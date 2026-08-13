"""
生成任务注册表。

key 必须含集号 —— 否则同一部剧两集同时生成会互相顶掉状态。
口径直接复用 core.pipeline.scope_of，和产物索引保持一致。

存在 SQLite 里而不是进程内字典：任务要跑几十秒到几分钟，期间用户可能
关掉窗口、启动器可能被重启。内存态下这些情况一律表现为「页面回到未开始」，
用户看不出刚才那次到底跑没跑、token 花没花，只能再点一次 —— 于是花两遍钱。
落库之后重启至少能看见「进程重启，任务中断」这个事实。

并发安全靠 SQLite 自己：claim() 是一条带条件的 UPSERT，
不需要再叠一层进程内的锁（叠了也没用，命令行是另一个进程）。
"""

from __future__ import annotations

import json

from core.pipeline import scope_of
from web.deps import store


def key_for(node, series_id: str, episode_no: int | None) -> tuple:
    """流水线节点的任务 key。第一段是 kind，用来和别的慢任务分开。"""
    return ("node", *scope_of(node, series_id, episode_no), node.contract)


def key_of(kind: str, *parts) -> tuple:
    """通用慢任务的 key，比如 ('suggest', 'writer')。"""
    return (kind, *parts)


def _enc(key: tuple) -> str:
    return json.dumps([str(x) for x in key], ensure_ascii=False)


def _dec(s: str) -> tuple:
    return tuple(json.loads(s))


def get(key: tuple) -> dict | None:
    with store.conn() as c:
        r = c.execute("SELECT state, msg FROM jobs WHERE key=?",
                      (_enc(key),)).fetchone()
    return {"state": r["state"], "msg": r["msg"]} if r else None


def is_running(key: tuple) -> bool:
    j = get(key)
    return bool(j and j["state"] == "running")


def claim(key: tuple) -> bool:
    """
    占位。已经在跑就返回 False，调用方据此拒绝重复提交。

    判定和写入必须是同一条语句 —— 先 SELECT 再 INSERT 的话，
    两次快速点击会双双看到「没在跑」，然后起两个线程跑同一个节点，
    两份产物抢着往同一个路径落盘。
    """
    with store.conn() as c:
        cur = c.execute(
            """INSERT INTO jobs(key, kind, state, msg, started_at, ended_at)
               VALUES(?,?,'running',?,datetime('now','localtime'),NULL)
               ON CONFLICT(key) DO UPDATE SET
                   state='running', msg=excluded.msg,
                   started_at=excluded.started_at, ended_at=NULL
               WHERE jobs.state <> 'running'""",
            (_enc(key), str(key[0]), "正在生成…"))
        return cur.rowcount > 0


def _settle(key: tuple, state: str, msg: str) -> None:
    with store.conn() as c:
        c.execute(
            """INSERT INTO jobs(key, kind, state, msg, ended_at)
               VALUES(?,?,?,?,datetime('now','localtime'))
               ON CONFLICT(key) DO UPDATE SET
                   state=excluded.state, msg=excluded.msg,
                   ended_at=excluded.ended_at""",
            (_enc(key), str(key[0]), state, msg))


def finish(key: tuple, msg: str = "生成完成，待审") -> None:
    _settle(key, "done", msg)


def fail(key: tuple, msg: str) -> None:
    _settle(key, "error", msg)


def sweep_stale() -> int:
    """
    把上次进程留下的 running 标成中断。工作台启动时跑一次。

    不这么做的话，重启后前端会对着一个永远不会完成的任务轮询下去 ——
    卡片永远显示「生成中」，没有任何线程在推进它。

    放在 create_app() 而不是 Store.init()：命令行也会调 init()，
    在工作台正跑着任务时敲一条 avctl 命令不该把它判死。
    """
    with store.conn() as c:
        cur = c.execute(
            "UPDATE jobs SET state='error', msg='进程重启，任务中断。"
            "产物没有落盘，重新点一次即可。',"
            " ended_at=datetime('now','localtime') WHERE state='running'")
        return cur.rowcount


def failures(series_names: dict[str, str]) -> list[dict]:
    """
    失败的生成任务，供待办页汇总。
    失败只存在于任务表里 —— 产物压根没落盘（契约不过就不写），
    所以不查 artifacts 表是看不到这些的。
    """
    from core.contracts import get as get_contract
    from core.pipeline import node_for, parse_scope

    out = []
    with store.conn() as c:
        rows = c.execute("SELECT key, msg FROM jobs"
                         " WHERE state='error' AND kind='node'"
                         " ORDER BY ended_at DESC").fetchall()
    for row in rows:
        key = _dec(row["key"])
        if len(key) != 4:
            continue
        _, scope_type, scope_id, contract = key
        node = node_for(contract)
        if not node:
            continue
        sid, ep = parse_scope(scope_type, scope_id)
        out.append({
            "series_id": sid, "series_name": series_names.get(sid, sid),
            "ep": ep, "no": node.no,
            "cn_name": get_contract(contract).cn_name,
            "contract": contract, "msg": row["msg"],
        })
    return out


def clear() -> None:
    """测试用。"""
    with store.conn() as c:
        c.execute("DELETE FROM jobs")
