"""剧集与流水线：总览、看板、新建、触发生成。"""

from __future__ import annotations

import json
import threading
import traceback

from flask import (Blueprint, abort, current_app, redirect, render_template,
                   request, url_for)

from core import contracts
from core.engine.runner import RunError, resolve_director, run_node
from core.gateway import keystore
from core.gateway.client import PROVIDERS
from core.pipeline import NODES, node_for, scope_of
from core.skillkit.director import discover_directors
from core.skillkit.package import SkillPackage
from web import jobs
from web.deps import (CALL_LOG, SKILLS, STATUS_CN, astore, provider_of,
                      render_error, slug, store)

bp = Blueprint("series", __name__)


@bp.route("/")
def index():
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
    return render_template("index.html", series=rows)


@bp.route("/series/<sid>")
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

    busy = any(r["job"] and r["job"]["state"] == "running" for r in rows)
    return render_template(
        "pipeline.html", s=s, eps=eps, ep=ep, rows=rows, blocked=blocked,
        directors=discover_directors(SKILLS), busy=busy,
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

    if not jobs.claim(key):
        return redirect(url_for("series.pipeline", sid=sid, ep=ep))

    opts = {
        "brief": request.form.get("brief", ""),
        "genre": [x.strip() for x in
                  (request.form.get("genre") or "").replace("，", ",")
                  .split(",") if x.strip()],
        "director": request.form.get("director") or None,
        "ep": ep,
    }
    threading.Thread(target=_run_job,
                     args=(current_app._get_current_object(), key, sid, node,
                           opts),
                     daemon=True).start()
    return redirect(url_for("series.pipeline", sid=sid, ep=ep))


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
        log_path=CALL_LOG,
    )
