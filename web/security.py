"""
工作台的安全层：CSRF 防护 + 可选口令。

两件事的价值差得很远，分开说：

CSRF 是**本机自用也必须做**的。工作台跑在 127.0.0.1，但浏览器的同源策略
拦不住跨站表单提交 —— 你在别的标签页打开一个网页，那个网页就能悄悄往
你的 localhost:5001 发 POST：盖章通过、合入 skill 建议、清掉 API 密钥、
或者触发一串烧钱的模型调用。你什么都不会看见。
代价是零：用户感知不到，所以默认常开。

口令是**只有对外暴露才需要**的。单人本机用加登录纯属给自己添堵，
所以默认不开。但一旦把 --host 绑到非 localhost，它就从「建议」变成「强制」：
没设口令就不许启动。原先那里只打印一行警告然后照常暴露，
警告是拦不住事故的。

零外部依赖：token 用 secrets，口令用 hashlib.pbkdf2，都是标准库。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "config"
SECRET_FILE = CONFIG / "session.key"
AUTH_FILE = CONFIG / "auth.json"

CSRF_FIELD = "_csrf"
CSRF_HEADER = "X-CSRFToken"
_ITERATIONS = 240_000


def _chmod600(p: Path) -> None:
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def secret_key() -> bytes:
    """
    会话签名密钥。持久化而不是每次启动随机生成 ——
    否则一重启所有人的登录态和 CSRF token 全失效，双击启动的工具经不起这个。
    """
    CONFIG.mkdir(parents=True, exist_ok=True)
    if SECRET_FILE.exists():
        data = SECRET_FILE.read_bytes().strip()
        if len(data) >= 32:
            return data
    key = secrets.token_bytes(48)
    SECRET_FILE.write_bytes(key)
    _chmod600(SECRET_FILE)
    return key


# ---------- CSRF ----------

def issue_token(session) -> str:
    if not session.get(CSRF_FIELD):
        session[CSRF_FIELD] = secrets.token_urlsafe(32)
    return session[CSRF_FIELD]


def token_ok(session, form, headers) -> bool:
    want = session.get(CSRF_FIELD)
    got = form.get(CSRF_FIELD) or headers.get(CSRF_HEADER)
    # compare_digest 防时序侧信道；两边都得是非空字符串
    return bool(want and got and hmac.compare_digest(str(want), str(got)))


# ---------- 口令 ----------

def _read_auth() -> dict:
    if not AUTH_FILE.exists():
        return {}
    try:
        return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def has_password() -> bool:
    d = _read_auth()
    return bool(d.get("hash") and d.get("salt"))


def set_password(raw: str) -> None:
    salt = secrets.token_hex(16)
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps({
        "salt": salt,
        "hash": _derive(raw, salt),
        "iterations": _ITERATIONS,
    }, indent=2), encoding="utf-8")
    _chmod600(AUTH_FILE)


def clear_password() -> None:
    AUTH_FILE.unlink(missing_ok=True)


def _derive(raw: str, salt: str, iterations: int = _ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"),
                               salt.encode("utf-8"), iterations).hex()


def check_password(raw: str) -> bool:
    d = _read_auth()
    if not d.get("hash") or not d.get("salt"):
        return False
    got = _derive(raw, d["salt"], d.get("iterations", _ITERATIONS))
    return hmac.compare_digest(got, d["hash"])


def is_local(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")
