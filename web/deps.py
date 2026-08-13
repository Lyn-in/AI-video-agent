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


def normalize_newlines(text: str) -> str:
    """
    把 textarea 提交回来的 CRLF 收回 LF。

    浏览器按 HTML 规范提交 <textarea> 时一律用 CRLF，于是在网页上点一次
    「保存」——哪怕一个字没改——整个文件的每一行都会被判定为改过：
    SKILL.md 的版本存档全是满屏 diff，产物 JSON 里的多行字段（锚定描述之类）
    还会被塞进 \\r。这类污染不报错，只是慢慢把版本历史和飞轮统计弄脏。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def form_text(form, key: str, default: str = "") -> str:
    return normalize_newlines(form.get(key, default) or default)


def render_error(msg: str, *, eyebrow: str = "没能完成",
                 lead: str = "这次操作没有生效。", aid: str | None = None):
    return render_template("errors.html", errs=[msg], aid=aid,
                           eyebrow=eyebrow, lead=lead)


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
    """
    剧集 id。会出现在 URL 和 projects/ 的目录名里，所以只用 ASCII。

    中文名整串都不是 ASCII，逐字符过滤后 base 必然为空 —— 于是每一部剧
    都叫 s-xxxxx，列表和目录树里彼此分不开。这里改成先转拼音首字母
    （没有依赖，用一张声母区间表），实在转不出来才退回 s。

    哈希从 5 位加到 8 位：5 位十六进制约 100 万种，按生日问题算，
    一千部剧就有约 4‰ 的撞号概率，而撞号在这里是 INSERT OR REPLACE
    直接盖掉别人的剧集，不报错。
    """
    base = "".join(ch for ch in name if ch.isalnum() and ch.isascii())
    if not base:
        base = "".join(_initial(ch) for ch in name)[:12]
    return (base.lower() or "s") + "-" + \
        hashlib.md5(name.encode("utf-8")).hexdigest()[:8]


# 汉语拼音声母的 GB2312 码位区间，按首字母分段。
# 只用来生成可读的 id 片段，不追求准确 —— 多音字、生僻字落到哪都无所谓，
# 后面还跟着哈希保证唯一。
_PINYIN_BOUNDS = (
    (0xB0A1, "a"), (0xB0C5, "b"), (0xB2C1, "c"), (0xB4EE, "d"),
    (0xB6EA, "e"), (0xB7A2, "f"), (0xB8C1, "g"), (0xB9FE, "h"),
    (0xBBF7, "j"), (0xBFA6, "k"), (0xC0AC, "l"), (0xC2E8, "m"),
    (0xC4C3, "n"), (0xC5B6, "o"), (0xC5BE, "p"), (0xC6DA, "q"),
    (0xC8BB, "r"), (0xC8F6, "s"), (0xCBFA, "t"), (0xCDDA, "w"),
    (0xCEF4, "x"), (0xD1B9, "y"), (0xD4D1, "z"), (0xD7FA, ""),
)


def _initial(ch: str) -> str:
    """取一个字的拼音首字母；非汉字里只留 ASCII 字母数字。"""
    if ch.isalnum() and ch.isascii():
        return ch
    try:
        code = int.from_bytes(ch.encode("gb2312"), "big")
    except (UnicodeEncodeError, LookupError):
        return ""
    prev = ""
    for bound, letter in _PINYIN_BOUNDS:
        if code < bound:
            return prev
        prev = letter
    return ""
