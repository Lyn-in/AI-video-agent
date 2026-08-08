"""
生成任务注册表。

key 必须含集号 —— 否则同一部剧两集同时生成会互相顶掉状态。
口径直接复用 core.pipeline.scope_of，和产物索引保持一致。

目前是内存态：进程重启就没了，重启后页面显示节点还是原状态，重新点一次即可。
独立成模块是为了 P2 接 HTMX 轮询时能整体换成 SQLite 持久化，
而不必再去动各个视图。
"""

from __future__ import annotations

import threading

from core.pipeline import scope_of

_JOBS: dict[tuple, dict] = {}
_LOCK = threading.Lock()


def key_for(node, series_id: str, episode_no: int | None) -> tuple:
    """流水线节点的任务 key。第一段是 kind，用来和别的慢任务分开。"""
    return ("node", *scope_of(node, series_id, episode_no), node.contract)


def key_of(kind: str, *parts) -> tuple:
    """通用慢任务的 key，比如 ('suggest', 'writer')。"""
    return (kind, *parts)


def get(key: tuple) -> dict | None:
    with _LOCK:
        j = _JOBS.get(key)
        return dict(j) if j else None


def is_running(key: tuple) -> bool:
    j = get(key)
    return bool(j and j["state"] == "running")


def claim(key: tuple) -> bool:
    """占位。已经在跑就返回 False，调用方据此拒绝重复提交。"""
    with _LOCK:
        if key in _JOBS and _JOBS[key]["state"] == "running":
            return False
        _JOBS[key] = {"state": "running", "msg": "正在生成…"}
        return True


def finish(key: tuple, msg: str = "生成完成，待审") -> None:
    with _LOCK:
        _JOBS[key] = {"state": "done", "msg": msg}


def fail(key: tuple, msg: str) -> None:
    with _LOCK:
        _JOBS[key] = {"state": "error", "msg": msg}


def failures(series_names: dict[str, str]) -> list[dict]:
    """
    失败的生成任务，供待办页汇总。
    失败只存在于任务表里 —— 产物压根没落盘（契约不过就不写），
    所以不查 artifacts 表是看不到这些的。
    """
    from core.contracts import get as get_contract
    from core.pipeline import node_for, parse_scope

    out = []
    with _LOCK:
        items = [(k, dict(v)) for k, v in _JOBS.items()]
    for key, job in items:
        if job["state"] != "error" or key[0] != "node":
            continue
        _, scope_type, scope_id, contract = key
        sid, ep = parse_scope(scope_type, scope_id)
        node = node_for(contract)
        out.append({
            "series_id": sid, "series_name": series_names.get(sid, sid),
            "ep": ep, "no": node.no if node else "",
            "cn_name": get_contract(contract).cn_name,
            "contract": contract, "msg": job["msg"],
        })
    return out


def clear() -> None:
    """测试用。"""
    with _LOCK:
        _JOBS.clear()
