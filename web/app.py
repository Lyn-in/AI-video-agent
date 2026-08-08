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
from functools import lru_cache
from pathlib import Path

from flask import (Flask, abort, redirect, render_template, request, url_for)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import contracts                                    # noqa: E402
from core.pipeline import NODES, node_for, scope_of           # noqa: E402
from core.engine.gate import (                                # noqa: E402
    change_ratio, make_diff, MAJOR_REVISION_THRESHOLD,
)
from core.engine.flywheel import (                             # noqa: E402
    load_feedback, analyze, list_proposals, apply_suggestion, ApplyError,
    ref_governance, MIN_SAMPLES,
)
from core.skillkit.director import discover_directors          # noqa: E402
from core.engine.runner import (                               # noqa: E402
    RunError, resolve_director, run_node,
)
from core.skillkit.package import SkillError, SkillPackage, discover  # noqa: E402
from core.store.artifacts import ArtifactStore, to_markdown    # noqa: E402
from core.gateway.client import PROVIDERS                      # noqa: E402
from core.gateway import keystore                              # noqa: E402
from core.store.db import Store                                # noqa: E402

DB = ROOT / "config" / "platform.db"
SKILLS = ROOT / "skills"
PROJECTS = ROOT / "projects"

STATUS_CN = {
    "approved": "已通过",
    "revised": "已修改",
    "pending_review": "待审",
    "generating": "重生成中",
    None: "未开始",
}


# 正在跑的生成任务。key = (scope_type, scope_id, contract) —— 必须含集号，
# 否则同一部剧两集同时生成会互相顶掉状态。
# 内存态：进程重启就没了，重启后页面会显示节点还是原状态，重新点一次即可。
JOBS: dict[tuple, dict] = {}
JOBS_LOCK = threading.Lock()


def _job_key(node, series_id: str, episode_no: int | None) -> tuple:
    return (*scope_of(node, series_id, episode_no), node.contract)


@lru_cache(maxsize=None)
def _provider_of(skill_id: str) -> str:
    """
    读 skill 绑定的厂商。加缓存：流水线页每渲染一次要问 5 个节点，
    没有缓存就是每次翻页读 5 遍 manifest。
    manifest 的 model 段不能在界面上改，所以缓存不会读到脏值；
    手工改了 manifest 需要重启工作台。
    """
    try:
        return SkillPackage.load(SKILLS / skill_id).model_config.get(
            "provider", "anthropic")
    except SkillError:
        return "anthropic"


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
            art = store.latest_artifact(*scope_of(n, sid, ep), n.contract)
            cn = contracts.get(n.contract)
            st = art["status"] if art else None
            provider = _provider_of(n.skill)
            key_cfg = PROVIDERS.get(provider, {})
            has_key = bool(keystore.get_key(provider, key_cfg.get("key_env", "")))
            rows.append({"no": n.no, "contract": n.contract, "skill": n.skill,
                         "level": n.level, "level_cn": n.level_cn,
                         "cn_name": cn.cn_name, "artifact": art,
                         "status": st, "status_cn": STATUS_CN.get(st, st),
                         "provider": provider,
                         "provider_label": key_cfg.get("label", provider),
                         "has_key": has_key})
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
                j = JOBS.get(_job_key(node_for(r["contract"]), sid, ep))
                r["job"] = dict(j) if j else None
            busy = any(r["job"] and r["job"]["state"] == "running" for r in rows)

        directors = discover_directors(SKILLS)
        missing_providers = sorted({r["provider_label"] for r in rows
                                    if not r["has_key"]})
        return render_template("pipeline.html", s=s, eps=eps, ep=ep,
                               rows=rows, blocked=blocked,
                               directors=directors, busy=busy,
                               missing_providers=missing_providers)

    # ---------- 生成节点（界面直接跑，不用命令行） ----------
    @app.post("/series/<sid>/run/<contract>")
    def start_run(sid, contract):
        node = node_for(contract)
        if not node:
            abort(404)
        ep = request.form.get("ep", type=int) or 1
        key = _job_key(node, sid, ep)

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
        except RunError as e:
            # 预期内的失败（缺上游、契约不过、没密钥）：消息本身就是给人看的，
            # 不需要堆栈。
            app.logger.info("生成被拒 %s: %s", key, e)
            with JOBS_LOCK:
                JOBS[key] = {"state": "error", "msg": str(e)}
        except Exception as e:                                # noqa: BLE001
            # 意料之外的：留全量堆栈，否则线程里出的错会变成「没反应」。
            app.logger.error("生成失败 %s: %s", key, traceback.format_exc())
            with JOBS_LOCK:
                JOBS[key] = {"state": "error", "msg": f"内部错误：{e}"}

    def _do_run(sid, node, opts):
        """编排在 core.engine.runner —— 命令行走的是同一条路径。"""
        pkg = SkillPackage.load(SKILLS / node.skill)
        with store.conn() as c:
            srow = c.execute("SELECT * FROM series WHERE id=?",
                             (sid,)).fetchone()
            acc = srow["collection_id"] if srow else "s1"
            crow = c.execute("SELECT account_id FROM collections WHERE id=?",
                             (acc,)).fetchone()
        director_id = opts.get("director") or (srow["director_id"]
                                               if srow else None)
        run_node(
            pkg=pkg, store=store, astore=astore,
            series_id=sid, episode_no=opts.get("ep"),
            account=crow["account_id"] if crow else "default", collection=acc,
            brief=opts.get("brief", ""), genre_tags=opts.get("genre"),
            director=resolve_director(SKILLS, director_id, pkg),
            log_path=ROOT / "config" / "calls.jsonl",
        )

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

    # ---------- 密钥设置：多厂商 API Key 全在网页上填，不用命令行 ----------
    @app.route("/settings")
    def settings():
        st = keystore.status(PROVIDERS)
        rows = [{"id": pid, **cfg, **st[pid]} for pid, cfg in PROVIDERS.items()]
        return render_template("settings.html", rows=rows, nav="settings")

    @app.post("/settings/<provider>/save")
    def settings_save(provider):
        if provider not in PROVIDERS:
            abort(404)
        value = (request.form.get("key") or "").strip()
        if value:
            keystore.set_key(provider, value)
        return redirect(url_for("settings"))

    @app.post("/settings/<provider>/clear")
    def settings_clear(provider):
        if provider not in PROVIDERS:
            abort(404)
        keystore.clear_key(provider)
        return redirect(url_for("settings"))

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
