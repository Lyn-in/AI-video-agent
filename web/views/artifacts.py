"""审核门：读 → 判断 → 盖章 / 打回 / 直接改。"""

from __future__ import annotations

import json

from flask import (Blueprint, redirect, render_template, request, url_for)

from core import contracts
from core.engine.gate import (MAJOR_REVISION_THRESHOLD, build_retry_brief,
                              change_ratio, make_diff)
from core.pipeline import node_for, parse_scope
from core.skillkit.package import SkillError, SkillPackage
from core.store.artifacts import to_markdown
from web import review
from web.deps import (SKILLS, STATUS_CN, artifact_row, astore,
                      normalize_newlines, render_error, set_status,
                      store, write_artifact)

bp = Blueprint("artifacts", __name__, url_prefix="/artifact")


def _context(row: dict, art: dict) -> dict:
    """把产物放回它在流水线里的位置 —— 从待办点进来也知道自己在哪。"""
    sid, ep = parse_scope(row["scope_type"], row["scope_id"])
    node = node_for(art["contract"])
    with store.conn() as c:
        s = c.execute("SELECT name FROM series WHERE id=?", (sid,)).fetchone()
    return {"series_id": sid, "series_name": s["name"] if s else sid,
            "ep": ep, "no": node.no if node else "",
            "contract": art["contract"]}


def _page(aid: str, art: dict, row: dict, blocks, extra_errors=None,
          template="artifact.html", status_code=200):
    cn = contracts.get(art["contract"])
    with store.conn() as c:
        cits = [dict(r) for r in c.execute(
            "SELECT * FROM citations WHERE artifact_id=?", (aid,)).fetchall()]
        lin = [r["input_id"] for r in c.execute(
            "SELECT input_id FROM lineage WHERE artifact_id=?",
            (aid,)).fetchall()]
    html = render_template(
        template, row=row, art=art, cn=cn, aid=aid,
        ctx=_context(row, art), blocks=blocks,
        extra_errors=extra_errors or [],
        pairs=review.prompt_pairs(art["payload"]),
        md=to_markdown(art),
        raw=json.dumps(art["payload"], ensure_ascii=False, indent=2),
        citations=cits, lineage=lin,
        status_cn=STATUS_CN.get(art["status"], art["status"]))
    return (html, status_code) if status_code != 200 else html


@bp.route("/<aid>")
def artifact(aid):
    row = artifact_row(aid)
    art = astore.load(row["path"])
    cn = contracts.get(art["contract"])
    return _page(aid, art, row, review.build_blocks(cn, art["payload"]))


@bp.post("/<aid>/approve")
def approve(aid):
    set_status(aid, "approved")
    return redirect(url_for("artifacts.artifact", aid=aid))


@bp.post("/<aid>/save")
def save(aid):
    """
    分块保存。一处写坏只标那一块，其余编辑原样留着 ——
    原先是整坨 JSON 一个 textarea，打错个逗号就跳错误页，全丢。
    """
    row = artifact_row(aid)
    before = astore.load(row["path"])
    cn = contracts.get(before["contract"])

    submitted = {k[2:]: normalize_newlines(v)
                 for k, v in request.form.items() if k.startswith("f_")}
    blocks = review.build_blocks(cn, before["payload"], submitted)
    payload, bad = review.parse_blocks(cn, blocks, submitted)

    after = dict(before)
    after["payload"] = payload
    errs = [] if bad else contracts.validate_artifact(after, cn)
    extra = review.attach_errors(blocks, errs)

    if bad or errs:
        return _page(aid, before, row, blocks, extra_errors=extra,
                     status_code=400)

    after["status"] = "revised"
    write_artifact(row["path"], after)

    diff = make_diff(before, after, aid)
    if diff and row.get("skill_id"):
        try:
            SkillPackage.load(SKILLS / row["skill_id"]).record_feedback(aid, diff)
        except SkillError:
            # 反馈归集失败不该拖累保存本身 —— 产物已经写好了。
            pass
    store.set_status(aid, "revised")
    if change_ratio(before, after) >= MAJOR_REVISION_THRESHOLD:
        store.mark_revised(aid)
    return redirect(url_for("artifacts.artifact", aid=aid))


@bp.post("/<aid>/save_raw")
def save_raw(aid):
    """逃生舱：直接改整份 JSON。分块编辑处理不了的极端情况用它。"""
    row = artifact_row(aid)
    before = astore.load(row["path"])
    cn = contracts.get(before["contract"])
    try:
        payload = json.loads(normalize_newlines(
            request.form.get("payload", "")))
    except json.JSONDecodeError as e:
        return render_error(f"JSON 格式有误，无法保存：{e}"), 400

    after = dict(before)
    after["payload"] = payload
    errs = contracts.validate_artifact(after, cn)
    if errs:
        blocks = review.build_blocks(cn, before["payload"])
        extra = review.attach_errors(blocks, errs)
        return _page(aid, before, row, blocks, extra_errors=extra,
                     status_code=400)

    after["status"] = "revised"
    write_artifact(row["path"], after)
    diff = make_diff(before, after, aid)
    if diff and row.get("skill_id"):
        try:
            SkillPackage.load(SKILLS / row["skill_id"]).record_feedback(aid, diff)
        except SkillError:
            pass
    store.set_status(aid, "revised")
    if change_ratio(before, after) >= MAJOR_REVISION_THRESHOLD:
        store.mark_revised(aid)
    return redirect(url_for("artifacts.artifact", aid=aid))


@bp.post("/<aid>/reject")
def reject(aid):
    notes = (request.form.get("notes") or "").strip()
    if not notes:
        return render_error("打回需要写修改意见 —— "
                            "没有意见，重生成时模型不知道错在哪。"), 400
    row = artifact_row(aid)
    art = astore.load(row["path"])
    astore.resolve(row["path"]).with_suffix(".retry.md").write_text(
        build_retry_brief("", notes, art), encoding="utf-8")
    set_status(aid, "generating")
    return redirect(url_for("artifacts.artifact", aid=aid))
