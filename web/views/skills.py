"""Skill 库与质量飞轮：查看、在线改、审批合入建议。"""

from __future__ import annotations

import json

from flask import (Blueprint, redirect, render_template, request, url_for)

from core.engine.flywheel import (ApplyError, MIN_SAMPLES, analyze,
                                  apply_suggestion, list_proposals,
                                  load_feedback, ref_governance)
from core.skillkit.director import discover_directors
from core.skillkit.package import SkillPackage, discover
from web.deps import SKILLS, load_skill, render_error, store

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
    return render_template("skills.html", pkgs=pkgs,
                           directors=discover_directors(SKILLS))


@bp.route("/<sid>")
def detail(sid):
    p = load_skill(sid)
    recs = load_feedback(p.root)
    return render_template(
        "skill_detail.html", p=p, errs=p.validate(),
        versions=p.list_versions(),
        gov=ref_governance(store.ref_health(sid)),
        stats=analyze(recs), min_samples=MIN_SAMPLES,
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

    picks = [int(x) for x in request.form.getlist("pick")]
    if not picks:
        return render_error("至少选一条建议合入，或点驳回。"), 400

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
            "errors.html", aid=None,
            errs=["合入后 SKILL.md 不再符合规范，已自动回滚："] + errs), 400

    prop.data["_status"] = "applied"
    prop.data["_applied"] = picks
    prop.path.write_text(json.dumps(prop.data, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    store.record_skill_version(sid, p.version, p.root / "SKILL.md",
                               f"合入建议 {prop.created_at}")
    return redirect(url_for("skills.detail", sid=sid))


@bp.post("/<sid>/save")
def save(sid):
    p = load_skill(sid)
    note = request.form.get("note", "")
    p.snapshot(note=note)
    (p.root / "SKILL.md").write_text(request.form.get("body", ""),
                                     encoding="utf-8")
    store.record_skill_version(sid, p.version, p.root / "SKILL.md", note)
    return redirect(url_for("skills.detail", sid=sid))
