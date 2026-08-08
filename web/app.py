"""
生产工作台。

设计取舍：Flask + Jinja2 + HTMX，不引入前端构建链。
审核门这类交互 HTMX 完全够用，省掉一整套 React 工具链的心智负担。

视觉方向：通告单 / 场记板。
审核门在真实剧组里就是盖章，所以通过的节点盖一枚印章 —— 这是签名元素。
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from pathlib import Path

from flask import (Flask, abort, redirect, render_template, request, url_for)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import contracts                                    # noqa: E402
from core.engine.gate import (                                # noqa: E402
    change_ratio, make_diff, MAJOR_REVISION_THRESHOLD,
)
from core.engine.flywheel import (                             # noqa: E402
    load_feedback, analyze, list_proposals, apply_suggestion, ApplyError,
    ref_governance, MIN_SAMPLES,
)
from core.skillkit.director import (                           # noqa: E402
    Director, discover_directors, DIRECTOR_BLIND_SKILLS,
)
from core.skillkit.package import SkillPackage, discover       # noqa: E402
from core.store.artifacts import ArtifactStore, to_markdown    # noqa: E402
from core.gateway.client import GatewayError, ModelGateway     # noqa: E402
from core.store.artifacts import input_hash, make_envelope     # noqa: E402
from core.store.db import Store                                # noqa: E402

DB = ROOT / "config" / "platform.db"
SKILLS = ROOT / "skills"
PROJECTS = ROOT / "projects"

# 流水线节点定义。N1-N6 是真实的顺序，编号在这里承载信息，不是装饰。
NODES = [
    {"no": "N2", "contract": "story_file", "skill": "writer", "level": "episode"},
    {"no": "N3", "contract": "screenplay", "skill": "playwright", "level": "episode"},
    {"no": "N4", "contract": "character_board", "skill": "casting", "level": "series"},
    {"no": "N5", "contract": "location_board", "skill": "set_dresser", "level": "series"},
    {"no": "N6", "contract": "shotlist", "skill": "storyboard", "level": "episode"},
]

STATUS_CN = {
    "approved": "已通过",
    "revised": "已修改",
    "pending_review": "待审",
    "generating": "重生成中",
    None: "未开始",
}


# 正在跑的生成任务。key = (series, contract)
# 内存态：进程重启就没了，重启后页面会显示节点还是原状态，重新点一次即可。
JOBS: dict[tuple, dict] = {}
JOBS_LOCK = threading.Lock()


def create_app() -> Flask:
    app = Flask(__name__)
    store = Store(DB)
    store.init()
    astore = ArtifactStore(PROJECTS)

    # ---------- 总览 ----------
    @app.route("/")
    def index():
        with store.conn() as c:
            series = [dict(r) for r in c.execute(
                "SELECT s.*, col.name AS collection_name, a.name AS account_name "
                "FROM series s "
                "LEFT JOIN collections col ON s.collection_id = col.id "
                "LEFT JOIN accounts a ON col.account_id = a.id "
                "ORDER BY s.created_at DESC").fetchall()]
            for s in series:
                s["genre_tags"] = json.loads(s.get("genre_tags") or "[]")
                s["episodes"] = [dict(r) for r in c.execute(
                    "SELECT * FROM episodes WHERE series_id=? "
                    "ORDER BY episode_no", (s["id"],)).fetchall()]
        return render_template("index.html", series=series)

    # ---------- 流水线看板 ----------
    @app.route("/series/<sid>")
    def pipeline(sid):
        ep = request.args.get("ep", type=int)
        with store.conn() as c:
            s = c.execute("SELECT * FROM series WHERE id=?", (sid,)).fetchone()
            if not s:
                abort(404)
            s = dict(s)
            s["genre_tags"] = json.loads(s.get("genre_tags") or "[]")
            eps = [dict(r) for r in c.execute(
                "SELECT * FROM episodes WHERE series_id=? ORDER BY episode_no",
                (sid,)).fetchall()]
        if ep is None and eps:
            ep = eps[0]["episode_no"]

        rows, blocked = [], None
        for n in NODES:
            art = store.latest_artifact(
                "series" if n["level"] == "series" else "episode",
                sid, n["contract"])
            cn = contracts.get(n["contract"])
            st = art["status"] if art else None
            rows.append({**n, "cn_name": cn.cn_name, "artifact": art,
                         "status": st, "status_cn": STATUS_CN.get(st, st)})
            if blocked is None and st != "approved":
                blocked = rows[-1]
                rows[-1]["is_block"] = True
        # 卡点之后的节点不是「被审核卡住」，只是还没轮到。
        # 视觉上必须区分，否则一眼看过去五个节点都在报警。
        seen_block = False
        for r in rows:
            r["waiting"] = seen_block
            if r.get("is_block"):
                seen_block = True

        with JOBS_LOCK:
            for r in rows:
                j = JOBS.get((sid, r["contract"]))
                r["job"] = dict(j) if j else None
            busy = any(r["job"] and r["job"]["state"] == "running" for r in rows)

        directors = discover_directors(SKILLS)
        return render_template("pipeline.html", s=s, eps=eps, ep=ep,
                               rows=rows, blocked=blocked,
                               directors=directors, busy=busy,
                               has_key=bool(__import__("os").environ.get(
                                   "ANTHROPIC_API_KEY")))

    # ---------- 生成节点（界面直接跑，不用命令行） ----------
    @app.post("/series/<sid>/run/<contract>")
    def run_node(sid, contract):
        node = next((n for n in NODES if n["contract"] == contract), None)
        if not node:
            abort(404)
        ep = request.form.get("ep", type=int) or 1
        key = (sid, contract)

        with JOBS_LOCK:
            if key in JOBS and JOBS[key]["state"] == "running":
                return redirect(url_for("pipeline", sid=sid, ep=ep))
            JOBS[key] = {"state": "running", "msg": "正在生成…"}

        opts = {
            "brief": request.form.get("brief", ""),
            "genre": [x.strip() for x in
                      (request.form.get("genre") or "").replace("，", ",")
                      .split(",") if x.strip()],
            "director": request.form.get("director") or None,
            "ep": ep,
        }
        threading.Thread(target=_run_job, args=(key, sid, node, opts),
                         daemon=True).start()
        return redirect(url_for("pipeline", sid=sid, ep=ep))

    def _run_job(key, sid, node, opts):
        try:
            _do_run(sid, node, opts)
            with JOBS_LOCK:
                JOBS[key] = {"state": "done", "msg": "生成完成，待审"}
        except Exception as e:                                # noqa: BLE001
            app.logger.error("生成失败 %s: %s", key, traceback.format_exc())
            with JOBS_LOCK:
                JOBS[key] = {"state": "error", "msg": str(e)}

    def _do_run(sid, node, opts):
        from cli.avctl import _build_prompt
        pkg = SkillPackage.load(SKILLS / node["skill"])
        errs = pkg.validate()
        if errs:
            raise RuntimeError(f"{pkg.id} 不符合平台规范：{errs[0]}")

        with store.conn() as c:
            srow = c.execute("SELECT * FROM series WHERE id=?", (sid,)).fetchone()
        director_id = opts.get("director") or (srow["director_id"] if srow else None)

        # 自动收集上游产物：不用你手工指定文件
        inputs, ids = [], []
        for need in pkg.input_contracts:
            up = next((n for n in NODES if n["contract"] == need), None)
            row = store.latest_artifact(
                "series" if up and up["level"] == "series" else "episode",
                sid, need)
            if row:
                a = astore.load(row["path"])
                inputs.append(a)
                ids.append(a["artifact_id"])
        missing = pkg.check_inputs([a["contract"] for a in inputs])
        if missing:
            names = "、".join(contracts.get(m).cn_name for m in missing)
            raise RuntimeError(f"缺少上游：{names}。请先生成并通过它们。")

        tags = list(opts.get("genre") or [])
        if not tags:
            for a in inputs:
                tags += a.get("payload", {}).get("genre_tags", [])

        director = None
        if director_id and pkg.id not in DIRECTOR_BLIND_SKILLS:
            droot = SKILLS / "directors" / director_id
            if droot.is_dir():
                director = Director.load(droot)

        system, user = _build_prompt(pkg, opts.get("brief", ""),
                                     inputs, tags, director)
        gw = ModelGateway(log_path=ROOT / "config" / "calls.jsonl")
        res = gw.call(system=system, user=user,
                      model_config=pkg.model_config,
                      skill_id=pkg.id, tag=pkg.output_contract)
        payload = res.json_payload()
        cits = payload.pop("_references_cited", [])
        art = make_envelope(pkg.output_contract, payload,
                            skill_id=pkg.id, skill_version=pkg.version,
                            model=f"{res.provider}/{res.model}",
                            director=director.id if director else None,
                            inputs=ids, in_hash=input_hash(inputs),
                            citations=cits)
        verrs = contracts.validate_artifact(
            art, contracts.get(pkg.output_contract),
            {a["contract"]: a["payload"] for a in inputs})
        if verrs:
            raise RuntimeError("产出不符合契约，未保存：" + "；".join(verrs[:3]))

        ep = opts.get("ep") if node["level"] != "series" else None
        acc = srow["collection_id"] if srow else "s1"
        with store.conn() as c:
            crow = c.execute("SELECT account_id FROM collections WHERE id=?",
                             (acc,)).fetchone()
        account = crow["account_id"] if crow else "default"
        p = astore.save(art, account=account, collection=acc,
                        series=sid, episode_no=ep)
        store.register_artifact(art, astore.rel(p),
                                "series" if node["level"] == "series" else "episode",
                                sid, art["lineage"]["input_hash"])

    # ---------- 新建剧集 ----------
    @app.post("/series/new")
    def series_new():
        name = (request.form.get("name") or "").strip()
        if not name:
            return render_error("请给剧集起个名字。"), 400
        sid = (request.form.get("sid") or "").strip() or _slug(name)
        tags = [x.strip() for x in (request.form.get("genre") or "")
                .replace("，", ",").split(",") if x.strip()]
        store.create_account("default", "我的账号")
        store.create_collection("s1", "default", "第一季", 1)
        store.create_series(sid, "s1", name,
                            director_id=request.form.get("director") or None,
                            genre_tags=tags)
        store.create_episode(f"{sid}-ep1", sid, 1, "第一集")
        return redirect(url_for("pipeline", sid=sid, ep=1))

    def _slug(name: str) -> str:
        import hashlib
        base = "".join(ch for ch in name if ch.isalnum() and ch.isascii())
        return (base or "s") + "-" + hashlib.md5(
            name.encode()).hexdigest()[:5]

    # ---------- 产物 ----------
    @app.route("/artifact/<aid>")
    def artifact(aid):
        with store.conn() as c:
            row = c.execute("SELECT * FROM artifacts WHERE id=?",
                            (aid,)).fetchone()
        if not row:
            abort(404)
        row = dict(row)
        art = astore.load(row["path"])
        cn = contracts.get(art["contract"])
        with store.conn() as c:
            cits = [dict(r) for r in c.execute(
                "SELECT * FROM citations WHERE artifact_id=?", (aid,)).fetchall()]
            lin = [r["input_id"] for r in c.execute(
                "SELECT input_id FROM lineage WHERE artifact_id=?",
                (aid,)).fetchall()]
        return render_template(
            "artifact.html", row=row, art=art, cn=cn,
            md=to_markdown(art), raw=json.dumps(art["payload"],
                                                ensure_ascii=False, indent=2),
            citations=cits, lineage=lin,
            status_cn=STATUS_CN.get(art["status"], art["status"]))

    @app.post("/artifact/<aid>/approve")
    def approve(aid):
        _set(aid, "approved")
        return redirect(url_for("artifact", aid=aid))

    @app.post("/artifact/<aid>/save")
    def save(aid):
        """人工改完 payload 保存。校验 + 归集 diff + 记大改。"""
        with store.conn() as c:
            row = dict(c.execute("SELECT * FROM artifacts WHERE id=?",
                                 (aid,)).fetchone())
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
        p = astore.resolve(row["path"])
        p.write_text(json.dumps(after, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        p.with_suffix(".md").write_text(to_markdown(after), encoding="utf-8")

        diff = make_diff(before, after, aid)
        ratio = change_ratio(before, after)
        if diff and row.get("skill_id"):
            try:
                SkillPackage.load(SKILLS / row["skill_id"]).record_feedback(aid, diff)
            except Exception:                                   # noqa: BLE001
                pass
        store.set_status(aid, "revised")
        if ratio >= MAJOR_REVISION_THRESHOLD:
            store.mark_revised(aid)
        return redirect(url_for("artifact", aid=aid))

    @app.post("/artifact/<aid>/reject")
    def reject(aid):
        notes = (request.form.get("notes") or "").strip()
        if not notes:
            return render_error("打回需要写修改意见 —— "
                                "没有意见，重生成时模型不知道错在哪。"), 400
        with store.conn() as c:
            row = dict(c.execute("SELECT * FROM artifacts WHERE id=?",
                                 (aid,)).fetchone())
        art = astore.load(row["path"])
        from core.engine.gate import build_retry_brief
        brief = build_retry_brief("", notes, art)
        p = astore.resolve(row["path"])
        p.with_suffix(".retry.md").write_text(brief, encoding="utf-8")
        _set(aid, "generating")
        return redirect(url_for("artifact", aid=aid))

    def _set(aid, status):
        with store.conn() as c:
            row = c.execute("SELECT path FROM artifacts WHERE id=?",
                            (aid,)).fetchone()
        if not row:
            abort(404)
        art = astore.load(row["path"])
        art["status"] = status
        p = astore.resolve(row["path"])
        p.write_text(json.dumps(art, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        p.with_suffix(".md").write_text(to_markdown(art), encoding="utf-8")
        store.set_status(aid, status)

    def _load_skill(sid: str) -> SkillPackage:
        """按 id 加载 skill。路径必须落在 skills/ 之内 —— 防目录穿越。"""
        root = (SKILLS / sid).resolve()
        if not str(root).startswith(str(SKILLS.resolve()) + "/"):
            abort(404)
        if not root.is_dir():
            abort(404)
        return SkillPackage.load(root)

    def render_error(msg):
        return render_template("errors.html", errs=[msg], aid=None)

    # ---------- Skill ----------
    @app.route("/skills")
    def skills():
        pkgs = []
        for p in discover(SKILLS):
            pkgs.append({"pkg": p, "errs": p.validate(),
                         "genres": [g.title for g in p.genres],
                         "refs": len(p.refs),
                         "feedback": len(list((p.root / "feedback").glob("*.md")))
                         if (p.root / "feedback").is_dir() else 0})
        return render_template("skills.html", pkgs=pkgs,
                               directors=discover_directors(SKILLS))

    @app.route("/skills/<sid>")
    def skill_detail(sid):
        p = _load_skill(sid)
        recs = load_feedback(p.root)
        return render_template(
            "skill_detail.html", p=p, errs=p.validate(),
            versions=p.list_versions(),
            gov=ref_governance(store.ref_health(sid)),
            stats=analyze(recs), min_samples=MIN_SAMPLES,
            proposals=[x for x in list_proposals(p.root, sid)
                       if x.status == "pending"],
            feedback=[r.path.name for r in reversed(recs)][:12])

    @app.post("/skills/<sid>/proposal/apply")
    def proposal_apply(sid):
        p = _load_skill(sid)
        props = [x for x in list_proposals(p.root, sid) if x.status == "pending"]
        if not props:
            return render_error("没有待批建议。"), 400
        prop = props[0]

        if request.form.get("reject"):
            prop.data["_status"] = "rejected"
            prop.path.write_text(json.dumps(prop.data, ensure_ascii=False,
                                            indent=2), encoding="utf-8")
            return redirect(url_for("skill_detail", sid=sid))

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

        fresh = SkillPackage.load(p.root)
        errs = fresh.validate()
        if errs:
            vs = fresh.list_versions()
            if vs:
                (p.root / "SKILL.md").write_text(
                    vs[-1].read_text(encoding="utf-8"), encoding="utf-8")
            return render_template(
                "errors.html", aid=None,
                errs=["合入后 SKILL.md 不再符合规范，已自动回滚："] + errs), 400

        prop.data["_status"] = "applied"
        prop.data["_applied"] = picks
        prop.path.write_text(json.dumps(prop.data, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        store.record_skill_version(sid, p.version, p.root / "SKILL.md",
                                   f"合入建议 {prop.created_at}")
        return redirect(url_for("skill_detail", sid=sid))

    @app.post("/skills/<sid>/save")
    def skill_save(sid):
        p = _load_skill(sid)
        p.snapshot(note=request.form.get("note", ""))
        (p.root / "SKILL.md").write_text(request.form.get("body", ""),
                                         encoding="utf-8")
        store.record_skill_version(sid, p.version,
                                   p.root / "SKILL.md",
                                   request.form.get("note", ""))
        return redirect(url_for("skill_detail", sid=sid))

    # ---------- 导演库 ----------
    @app.route("/directors")
    def directors():
        ds = [{"d": d, "errs": d.validate()}
              for d in discover_directors(SKILLS)]
        return render_template("directors.html", ds=ds)

    @app.template_filter("shortid")
    def shortid(v):
        return (v or "")[-6:]

    return app


if __name__ == "__main__":
    # debug 默认关闭：Werkzeug 调试器允许在页面上执行代码，
    # 而工作台目前没有任何鉴权，开着等于开后门。
    # 需要调试用 avctl web --debug。
    create_app().run(host="127.0.0.1", port=5001, debug=False)
