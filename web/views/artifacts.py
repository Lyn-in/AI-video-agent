"""审核门：读 → 判断 → 盖章 / 打回 / 直接改。"""

from __future__ import annotations

import json

from flask import (Blueprint, redirect, render_template, request, url_for)

from core import contracts
from core.engine.gate import (MAJOR_REVISION_THRESHOLD, build_retry_brief,
                              change_ratio, make_diff)
from core.skillkit.package import SkillError, SkillPackage
from core.store.artifacts import to_markdown
from web.deps import (SKILLS, STATUS_CN, artifact_row, astore, render_error,
                      set_status, store, write_artifact)

bp = Blueprint("artifacts", __name__, url_prefix="/artifact")


@bp.route("/<aid>")
def artifact(aid):
    row = artifact_row(aid)
    art = astore.load(row["path"])
    with store.conn() as c:
        cits = [dict(r) for r in c.execute(
            "SELECT * FROM citations WHERE artifact_id=?", (aid,)).fetchall()]
        lin = [r["input_id"] for r in c.execute(
            "SELECT input_id FROM lineage WHERE artifact_id=?",
            (aid,)).fetchall()]
    return render_template(
        "artifact.html", row=row, art=art,
        cn=contracts.get(art["contract"]), md=to_markdown(art),
        raw=json.dumps(art["payload"], ensure_ascii=False, indent=2),
        citations=cits, lineage=lin,
        status_cn=STATUS_CN.get(art["status"], art["status"]))


@bp.post("/<aid>/approve")
def approve(aid):
    set_status(aid, "approved")
    return redirect(url_for("artifacts.artifact", aid=aid))


@bp.post("/<aid>/save")
def save(aid):
    """人工改完 payload 保存。校验 + 归集 diff + 记大改。"""
    row = artifact_row(aid)
    before = astore.load(row["path"])
    try:
        payload = json.loads(request.form.get("payload", ""))
    except json.JSONDecodeError as e:
        return render_error(f"JSON 格式有误，无法保存：{e}"), 400

    after = dict(before)
    after["payload"] = payload
    errs = contracts.validate_artifact(after, contracts.get(after["contract"]))
    if errs:
        return render_template("errors.html", errs=errs, aid=aid), 400

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
