"""Skill 库与质量飞轮：查看、在线改、审批合入建议。"""

from __future__ import annotations

import json
import threading
import traceback

from flask import (Blueprint, current_app, make_response, redirect,
                   render_template, request, url_for)

from core.engine.flywheel import (ApplyError, MIN_SAMPLES, SuggestError,
                                  analyze, apply_suggestion, generate_proposal,
                                  list_proposals, load_feedback,
                                  ref_governance)
from core.skillkit.package import SkillPackage, discover
from web import jobs
from web.deps import (CALL_LOG, SKILLS, form_text, load_skill,
                      render_error, store)

bp = Blueprint("skills", __name__, url_prefix="/skills")


@bp.route("")
def index():
    pkgs = []
    for p in discover(SKILLS):
        fb = p.root / "feedback"
        pkgs.append({"pkg": p, "errs": p.validate(),
                     "genres": [g.title for g in p.genres],
                     "refs": len(p.refs),
                     "feedback": len(list(fb.glob("*.md"))) if fb.is_dir() else 0})
    return render_template("skills.html", pkgs=pkgs)


@bp.route("/<sid>")
def detail(sid):
    p = load_skill(sid)
    recs = load_feedback(p.root)
    return render_template(
        "skill_detail.html", p=p, sid=sid, errs=p.validate(),
        versions=p.list_versions(),
        gov=ref_governance(store.ref_health(sid)),
        stats=analyze(recs), min_samples=MIN_SAMPLES,
        job=jobs.get(jobs.key_of("suggest", sid)),
        proposals=[x for x in list_proposals(p.root, sid)
                   if x.status == "pending"],
        feedback=[r.path.name for r in reversed(recs)][:12])


@bp.post("/<sid>/proposal/apply")
def proposal_apply(sid):
    p = load_skill(sid)
    props = [x for x in list_proposals(p.root, sid) if x.status == "pending"]
    if not props:
        return render_error("没有待批建议。"), 400
    prop = props[0]

    if request.form.get("reject"):
        prop.data["_status"] = "rejected"
        prop.path.write_text(json.dumps(prop.data, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        return redirect(url_for("skills.detail", sid=sid))

    # 表单值一律当不可信输入：直接 int() 的话，一个非数字的 pick
    # 就是一次未捕获的 ValueError，用户看到的是 500 而不是「选一条」。
    picks = [int(x) for x in request.form.getlist("pick") if x.isdigit()]
    if not picks:
        return render_error("至少选一条建议合入，或点驳回。"), 400
    if any(i < 1 or i > len(prop.suggestions) for i in picks):
        return render_error("选中的建议不存在，页面可能过期了，刷新再试。"), 400

    body = p.skill_md
    try:
        for i in picks:
            body = apply_suggestion(body, prop.suggestions[i - 1])
    except (ApplyError, IndexError) as e:
        return render_error(f"无法自动合入：{e}"), 400

    p.snapshot(note=f"合入建议 {prop.created_at} 第 {picks} 条")
    (p.root / "SKILL.md").write_text(body, encoding="utf-8")

    # skill 是这个平台唯一的资产，合入后必须复检规范，不合规立刻回滚。
    fresh = SkillPackage.load(p.root)
    errs = fresh.validate()
    if errs:
        vs = fresh.list_versions()
        if vs:
            (p.root / "SKILL.md").write_text(vs[-1].read_text(encoding="utf-8"),
                                             encoding="utf-8")
        return render_template(
            "errors.html", aid=None, eyebrow="已回滚",
            lead="合入后 SKILL.md 不再符合规范，已自动退回上一版。",
            errs=errs), 400

    prop.data["_status"] = "applied"
    prop.data["_applied"] = picks
    prop.path.write_text(json.dumps(prop.data, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    store.record_skill_version(sid, p.version, p.root / "SKILL.md",
                               f"合入建议 {prop.created_at}")
    return redirect(url_for("skills.detail", sid=sid))


@bp.post("/<sid>/suggest")
def suggest(sid):
    """
    让模型读反馈、给出 SKILL.md 的改进建议。

    这一步原先只有命令行有 —— 界面能审批它自己生成不了的建议，闭环是断的。
    模型调用要几十秒，所以扔后台线程 + 片段轮询，和流水线节点一个路子。
    """
    p = load_skill(sid)
    key = jobs.key_of("suggest", sid)
    if jobs.claim(key):
        threading.Thread(
            target=_suggest_job,
            args=(current_app._get_current_object(), key, p.root),
            daemon=True).start()
    if request.headers.get("HX-Request"):
        return render_suggest(sid)
    return redirect(url_for("skills.detail", sid=sid))


def _suggest_job(app, key, root):
    try:
        prop, data = generate_proposal(SkillPackage.load(root),
                                       log_path=CALL_LOG)
        jobs.finish(key, f"已生成 {len(data.get('suggestions', []))} 条建议，"
                         f"结论：{data.get('verdict', '')}")
    except SuggestError as e:
        app.logger.info("建议生成被拒 %s: %s", key, e)
        jobs.fail(key, str(e))
    except Exception as e:                                    # noqa: BLE001
        app.logger.error("建议生成失败 %s: %s", key, traceback.format_exc())
        jobs.fail(key, f"内部错误：{e}")


def render_suggest(sid: str):
    p = load_skill(sid)
    recs = load_feedback(p.root)
    html = render_template(
        "fragments/suggest.html", sid=sid, stats=analyze(recs),
        min_samples=MIN_SAMPLES, job=jobs.get(jobs.key_of("suggest", sid)),
        proposals=[x for x in list_proposals(p.root, sid)
                   if x.status == "pending"])
    resp = make_response(html)
    job = jobs.get(jobs.key_of("suggest", sid))
    if job and job["state"] in ("done", "error"):
        # 跑完通知整页刷新，好让新生成的提案出现在审批区
        resp.headers["HX-Trigger"] = "suggestSettled"
    return resp


@bp.post("/<sid>/save")
def save(sid):
    p = load_skill(sid)
    note = request.form.get("note", "")
    p.snapshot(note=note)
    (p.root / "SKILL.md").write_text(form_text(request.form, "body"),
                                     encoding="utf-8")
    store.record_skill_version(sid, p.version, p.root / "SKILL.md", note)
    return redirect(url_for("skills.detail", sid=sid))
