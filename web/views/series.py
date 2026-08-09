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


def _providers_info() -> list[dict]:
    """所有厂商的 id、名称、是否有密钥——给模型选择下拉用。"""
    return [
        {"id": pid, "label": cfg["label"],
         "default_model": cfg["default_model"],
         "has_key": bool(keystore.get_key(pid, cfg["key_env"]))}
        for pid, cfg in PROVIDERS.items()
    ]


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
    return render_template("series_list.html", series=_all_series(),
                           directors=discover_directors(SKILLS))


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

    want = request.args.get("step")
    current = (next((r for r in rows if r["contract"] == want), None)
               or blocked or rows[-1])
    return render_template(
        "pipeline.html", s=s, eps=eps, ep=ep, rows=rows, blocked=blocked,
        current=current, directors=discover_directors(SKILLS),
        providers=_providers_info(),
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


@bp.post("/series/<sid>/edit")
def edit_series(sid):
    s, _ = load_series(sid)
    name = (request.form.get("name") or "").strip()
    genre = [x.strip() for x in (request.form.get("genre") or "")
             .replace("，", ",").split(",") if x.strip()]
    director = request.form.get("director") or None
    with store.conn() as c:
        if name:
            c.execute("UPDATE series SET name=? WHERE id=?", (name, sid))
        c.execute("UPDATE series SET genre_tags=?, director_id=? WHERE id=?",
                  (json.dumps(genre, ensure_ascii=False), director, sid))
    return redirect(url_for("series.pipeline", sid=sid))


@bp.post("/series/<sid>/add-episode")
def add_episode(sid):
    s, eps = load_series(sid)
    next_no = max(e["episode_no"] for e in eps) + 1 if eps else 1
    title = (request.form.get("title") or "").strip() or f"第{next_no}集"
    store.create_episode(f"{sid}-ep{next_no}", sid, next_no, title)
    return redirect(url_for("series.pipeline", sid=sid, ep=next_no))


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
            "provider": request.form.get("provider") or None,
            "ep": ep,
            "dry_run": bool(request.form.get("dry_run")),
        }
        threading.Thread(
            target=_run_job,
            args=(current_app._get_current_object(), key, sid, node, opts),
            daemon=True).start()

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
                           directors=discover_directors(SKILLS),
                           providers=_providers_info())
    resp = make_response(html)
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
        app.logger.info("生成被拒 %s: %s", key, e)
        jobs.fail(key, str(e))
    except Exception as e:                                    # noqa: BLE001
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

    model_override = {}
    if opts.get("provider"):
        pid = opts["provider"]
        model_override["provider"] = pid
        if pid in PROVIDERS:
            model_override["model"] = PROVIDERS[pid]["default_model"]

    run_node(
        pkg=pkg, store=store, astore=astore,
        series_id=sid, episode_no=opts.get("ep"),
        account=crow["account_id"] if crow else "default", collection=acc,
        brief=opts.get("brief", ""), genre_tags=opts.get("genre"),
        director=resolve_director(SKILLS, director_id, pkg),
        log_path=CALL_LOG, dry_run=opts.get("dry_run", False),
        model_override=model_override,
    )
