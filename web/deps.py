"""
工作台的共享依赖与小工具。

路由拆成 blueprint 之后不再有 create_app() 那个大闭包可以捎带 store/astore，
统一放这里。两者做成模块级单例是安全的：Store 每次调用现开连接
（WAL + busy_timeout 已配好），ArtifactStore 只持有一个根路径，
都没有跨线程可变状态。
"""

from __future__ import annotations

import hashlib
import json
import sys
from functools import lru_cache
from pathlib import Path

from flask import abort, render_template

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.skillkit.package import SkillError, SkillPackage   # noqa: E402
from core.store.artifacts import ArtifactStore, to_markdown  # noqa: E402
from core.store.db import Store                              # noqa: E402

DB = ROOT / "config" / "platform.db"
SKILLS = ROOT / "skills"
PROJECTS = ROOT / "projects"
CALL_LOG = ROOT / "config" / "calls.jsonl"

store = Store(DB)
astore = ArtifactStore(PROJECTS)

STATUS_CN = {
    "approved": "已通过",
    "revised": "已修改",
    "pending_review": "待审",
    "generating": "重生成中",
    None: "未开始",
}


@lru_cache(maxsize=None)
def provider_of(skill_id: str) -> str:
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


def load_skill(sid: str) -> SkillPackage:
    """按 id 加载 skill。路径必须落在 skills/ 之内 —— 防目录穿越。"""
    root = (SKILLS / sid).resolve()
    if not str(root).startswith(str(SKILLS.resolve()) + "/"):
        abort(404)
    if not root.is_dir():
        abort(404)
    return SkillPackage.load(root)


def render_error(msg: str):
    return render_template("errors.html", errs=[msg], aid=None)


def artifact_row(aid: str) -> dict:
    with store.conn() as c:
        row = c.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
    if not row:
        abort(404)
    return dict(row)


def write_artifact(rel_path: str, art: dict) -> None:
    """产物落两份：json 是真相源，md 是审核门里人读的那份。"""
    p = astore.resolve(rel_path)
    p.write_text(json.dumps(art, ensure_ascii=False, indent=2), encoding="utf-8")
    p.with_suffix(".md").write_text(to_markdown(art), encoding="utf-8")


def set_status(aid: str, status: str) -> None:
    row = artifact_row(aid)
    art = astore.load(row["path"])
    art["status"] = status
    write_artifact(row["path"], art)
    store.set_status(aid, status)


def slug(name: str) -> str:
    base = "".join(ch for ch in name if ch.isalnum() and ch.isascii())
    return (base or "s") + "-" + hashlib.md5(name.encode()).hexdigest()[:5]
