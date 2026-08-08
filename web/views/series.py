"""剧集与流水线：总览、看板、新建、触发生成。"""

from __future__ import annotations

import json
import threading
import traceback

from flask import (Blueprint, abort, current_app, make_response, redirect,
                   render_template, request, url_for)

from core import contracts
from core.engine.runner import RunError, resolve_director, run_node
from core.gateway import keystore
from core.gateway.client import PROVIDERS
from core.pipeline import NODES, node_for, parse_scope, scope_of
from core.skillkit.director import discover_directors
from core.skillkit.package import SkillPackage
from web import jobs
from web.deps import (CALL_LOG, SKILLS, STATUS_CN, astore, provider_of,
                      render_error, slug, store)

bp = Blueprint("series", __name__)


def _all_series() -> list[dict]:
    with store.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT s.*, col.name AS collection_name, a.name AS account_name "
            "FROM series s "
            "LEFT JOIN collections col ON s.collection_id = col.id "
            "LEFT JOIN accounts a ON col.account_id = a.id "
            "ORDER BY s.created_at DESC").fetchall()]
        for s in rows:
            s["genre_tags"] = json.loads(s.get("genre_tags") or "[]")
            s["episodes"] = [dict(r) for r in c.execute(
                "SELECT * FROM episodes WHERE series_id=? ORDER BY episode_no",
                (s["id"],)).fetchall()]
    return rows


@bp.route("/")
def inbox():
    """
    待办。

    工作流里第一个问题不是「我有哪些剧集」，是「哪些东西在等我」——
    原先首页是「新建剧集 + 剧集列表」，这个问题没有落脚点，
    待审的节点散在各个流水线页里，得一个个点进去看。
    """
    names = {s["id"]: s["name"] for s in _all_series()}
    waiting, redo = [], []
    for row in store.list_artifacts():
        sid, ep = parse_scope(row["scope_type"], row["scope_id"])
        node = node_for(row["contract"])
        item = {
            "aid": row["id"], "series_id": sid, "series_name": names.get(sid, sid),
            "ep": ep, "no": node.no if node else "",
            "cn_name": contracts.get(row["contract"]).cn_name,
            "status": row["status"],
            "status_cn": STATUS_CN.get(row["status"], row["status"]),
            "updated_at": row["updated_at"],
        }
        if row["status"] in ("pending_review", "revised"):
            waiting.append(item)
        elif row["status"] == "generating":
            redo.append(item)

    waiting.sort(key=lambda x: x["updated_at"] or "", reverse=True)
    redo.sort(key=lambda x: x["updated_at"] or "", reverse=True)
    return render_template("inbox.html", waiting=waiting, redo=redo,
                           failed=jobs.failures(names),
                           has_series=bool(names))


@bp.route("/series")
def index():
    return render_template("series_list.html", series=_all_series())


def load_series(sid: str) -> tuple[dict, list[dict]]:
    with store.conn() as c:
        s = c.execute("SELECT * FROM series WHERE id=?", (sid,)).fetchone()
        if not s:
            abort(404)
        s = dict(s)
        s["genre_tags"] = json.loads(s.get("genre_tags") or "[]")
        eps = [dict(r) for r in c.execute(
            "SELECT * FROM episodes WHERE series_id=? ORDER BY episode_no",
            (sid,)).fetchall()]
    return s, eps


def node_rows(sid: str, ep: int | None) -> list[dict]:
    """
    五个节点的当前状态。整页渲染和 HTMX 片段都走这里 ——
    两边算出来的东西必须一模一样，否则轮询换回来的卡会和页面对不上。
    """
    rows, blocked = [], None
    for n in NODES:
        art = store.latest_artifact(*scope_of(n, sid, ep), n.contract)
        st = art["status"] if art else None
        provider = provider_of(n.skill)
        key_cfg = PROVIDERS.get(provider, {})
        rows.append({
            "no": n.no, "contract": n.contract, "skill": n.skill,
            "level": n.level, "level_cn": n.level_cn,
            "cn_name": contracts.get(n.contract).cn_name, "artifact": art,
            "status": st, "status_cn": STATUS_CN.get(st, st),
            "provider": provider,
            "provider_label": key_cfg.get("label", provider),
            "has_key": bool(keystore.get_key(provider,
                                             key_cfg.get("key_env", ""))),
            "job": jobs.get(jobs.key_for(n, sid, ep)),
        })
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
    return rows


@bp.route("/series/<sid>")
def pipeline(sid):
    s, eps = load_series(sid)
    ep = request.args.get("ep", type=int)
    if ep is None and eps:
        ep = eps[0]["episode_no"]
    rows = node_rows(sid, ep)
    blocked = next((r for r in rows if r.get("is_block")), None)

    # 默认停在卡点那一步 —— 打开页面就是「该干的那件事」，
    # 和待办首页同一个思路，只是范围收到这一部剧。
    want = request.args.get("step")
    current = (next((r for r in rows if r["contract"] == want), None)
               or blocked or rows[-1])
    return render_template(
        "pipeline.html", s=s, eps=eps, ep=ep, rows=rows, blocked=blocked,
        current=current, directors=discover_directors(SKILLS),
        missing_providers=sorted({r["provider_label"] for r in rows
                                  if not r["has_key"]}))


@bp.post("/series/new")
def series_new():
    name = (request.form.get("name") or "").strip()
    if not name:
        return render_error("请给剧集起个名字。"), 400
    sid = (request.form.get("sid") or "").strip() or slug(name)
    tags = [x.strip() for x in (request.form.get("genre") or "")
            .replace("，", ",").split(",") if x.strip()]
    store.create_account("default", "我的账号")
    store.create_collection("s1", "default", "第一季", 1)
    store.create_series(sid, "s1", name,
                        director_id=request.form.get("director") or None,
                        genre_tags=tags)
    store.create_episode(f"{sid}-ep1", sid, 1, "第一集")
    return redirect(url_for("series.pipeline", sid=sid, ep=1))


# ---------- 生成节点（界面直接跑，不用命令行） ----------

@bp.post("/series/<sid>/run/<contract>")
def start_run(sid, contract):
    node = node_for(contract)
    if not node:
        abort(404)
    ep = request.form.get("ep", type=int) or 1
    key = jobs.key_for(node, sid, ep)

    if jobs.claim(key):
        opts = {
            "brief": request.form.get("brief", ""),
            "genre": [x.strip() for x in
                      (request.form.get("genre") or "").replace("，", ",")
                      .split(",") if x.strip()],
            "director": request.form.get("director") or None,
            "ep": ep,
            "dry_run": bool(request.form.get("dry_run")),
        }
        threading.Thread(
            target=_run_job,
            args=(current_app._get_current_object(), key, sid, node, opts),
            daemon=True).start()

    # HTMX 就地换掉这张卡（它随即开始自轮询）；没有 HTMX 就退回整页跳转。
    if request.headers.get("HX-Request"):
        return render_node(sid, contract, ep)
    return redirect(url_for("series.pipeline", sid=sid, ep=ep))


def render_node(sid: str, contract: str, ep: int | None):
    """渲染单张节点卡。片段端点与 start_run 共用。"""
    s, _ = load_series(sid)
    row = next((r for r in node_rows(sid, ep) if r["contract"] == contract),
               None)
    if row is None:
        abort(404)
    html = render_template("fragments/node.html", r=row, s=s, ep=ep,
                           directors=discover_directors(SKILLS))
    resp = make_response(html)
    # 任务收尾的这一次（也是轮询的最后一次）通知上方的进度条刷新，
    # 否则卡片变了、进度条上的标记还停在「生成中」。
    if row["job"] and row["job"]["state"] in ("done", "error"):
        resp.headers["HX-Trigger"] = "nodeSettled"
    return resp


def render_stepper(sid: str, ep: int | None, step: str | None):
    s, _ = load_series(sid)
    rows = node_rows(sid, ep)
    blocked = next((r for r in rows if r.get("is_block")), None)
    current = (next((r for r in rows if r["contract"] == step), None)
               or blocked or rows[-1])
    return render_template("fragments/stepper.html", rows=rows, s=s, ep=ep,
                           current=current)


def _run_job(app, key, sid, node, opts):
    try:
        _do_run(sid, node, opts)
        jobs.finish(key)
    except RunError as e:
        # 预期内的失败（缺上游、契约不过、没密钥）：消息本身就是给人看的，
        # 不需要堆栈。
        app.logger.info("生成被拒 %s: %s", key, e)
        jobs.fail(key, str(e))
    except Exception as e:                                    # noqa: BLE001
        # 意料之外的：留全量堆栈，否则线程里出的错会变成「没反应」。
        app.logger.error("生成失败 %s: %s", key, traceback.format_exc())
        jobs.fail(key, f"内部错误：{e}")


def _do_run(sid, node, opts):
    """编排在 core.engine.runner —— 命令行走的是同一条路径。"""
    pkg = SkillPackage.load(SKILLS / node.skill)
    with store.conn() as c:
        srow = c.execute("SELECT * FROM series WHERE id=?", (sid,)).fetchone()
        acc = srow["collection_id"] if srow else "s1"
        crow = c.execute("SELECT account_id FROM collections WHERE id=?",
                         (acc,)).fetchone()
    director_id = opts.get("director") or (srow["director_id"] if srow else None)
    run_node(
        pkg=pkg, store=store, astore=astore,
        series_id=sid, episode_no=opts.get("ep"),
        account=crow["account_id"] if crow else "default", collection=acc,
        brief=opts.get("brief", ""), genre_tags=opts.get("genre"),
        director=resolve_director(SKILLS, director_id, pkg),
        log_path=CALL_LOG, dry_run=opts.get("dry_run", False),
    )
