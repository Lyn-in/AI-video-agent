"""
密钥库：多厂商 API Key 的本地存储，供网页「密钥设置」页读写。

设计取舍：
- 存 config/secrets.json（gitignore），而不是逼用户在命令行 export 环境变量
  或编辑 .env 文件 —— 工作台是给不懂命令行的人用的，密钥必须能在页面上填、改、清。
- 环境变量仍然优先生效：CI / 云端部署这类场景习惯用环境变量注入密钥，
  页面填的值只在没有环境变量时兜底，两种用法互不冲突。
- 明文落盘是已知取舍（见「商业化就绪度审查.md」），靠文件权限 0600 + gitignore
  兜底，多租户场景需要换成真正的密钥管理服务，这里不做。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SECRETS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "secrets.json"


def _read() -> dict:
    if not SECRETS_PATH.exists():
        return {}
    try:
        return json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write(data: dict) -> None:
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECRETS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(SECRETS_PATH, 0o600)      # 只有自己能读
    except OSError:
        pass


def get_key(provider: str, key_env: str) -> str | None:
    """取某厂商的密钥。环境变量优先，其次页面上填的密钥库。"""
    return os.environ.get(key_env) or _read().get(provider) or None


def status(providers: dict) -> dict:
    """{provider: {"configured": bool, "source": "env"|"file"|"none", "masked": str}}"""
    stored = _read()
    out = {}
    for p, cfg in providers.items():
        env_val = os.environ.get(cfg["key_env"])
        file_val = stored.get(p)
        if env_val:
            src, val = "env", env_val
        elif file_val:
            src, val = "file", file_val
        else:
            src, val = "none", None
        out[p] = {
            "configured": bool(val),
            "source": src,
            "masked": _mask(val) if val else "",
        }
    return out


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}{'•' * 8}{value[-4:]}"


def set_key(provider: str, value: str) -> None:
    value = value.strip()
    if not value:
        return
    data = _read()
    data[provider] = value
    _write(data)


def clear_key(provider: str) -> None:
    data = _read()
    if data.pop(provider, None) is not None:
        _write(data)
