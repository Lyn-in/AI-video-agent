"""
密钥库：多厂商 API 配置的本地存储，供网页「密钥设置」页读写。

每个厂商存三项：密钥（key）、接口地址（base_url）、模型（model）。
密钥必填，地址和模型留空则用厂商默认值。

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
        data = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    _migrate_flat(data)
    return data


def _migrate_flat(data: dict) -> None:
    """旧格式 {"anthropic": "sk-..."} → 新格式 {"anthropic": {"key": "sk-..."}}"""
    for k, v in list(data.items()):
        if isinstance(v, str):
            data[k] = {"key": v}


def _write(data: dict) -> None:
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECRETS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(SECRETS_PATH, 0o600)
    except OSError:
        pass


def _entry(data: dict, provider: str) -> dict:
    return data.get(provider) if isinstance(data.get(provider), dict) else {}


def get_key(provider: str, key_env: str) -> str | None:
    """取某厂商的密钥。环境变量优先，其次页面上填的密钥库。"""
    return os.environ.get(key_env) or _entry(_read(), provider).get("key") or None


def get_provider_config(provider: str) -> dict:
    """取某厂商的全部用户配置（base_url, model）。不含密钥。"""
    entry = _entry(_read(), provider)
    return {k: v for k, v in entry.items() if k in ("base_url", "model") and v}


def status(providers: dict) -> dict:
    """{provider: {"configured": bool, "source": ..., "masked": ..., "base_url": ..., "model": ...}}"""
    stored = _read()
    out = {}
    for p, cfg in providers.items():
        entry = _entry(stored, p)
        env_val = os.environ.get(cfg["key_env"])
        file_val = entry.get("key")
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
            "base_url": entry.get("base_url", ""),
            "model": entry.get("model", ""),
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
    entry = _entry(data, provider) or {}
    entry["key"] = value
    data[provider] = entry
    _write(data)


def set_provider_config(provider: str, *, base_url: str = "",
                        model: str = "", key: str = "") -> None:
    """保存厂商的全部可配置项。空字符串表示恢复默认。"""
    data = _read()
    entry = _entry(data, provider) or {}
    if key.strip():
        entry["key"] = key.strip()
    if base_url.strip():
        entry["base_url"] = base_url.strip()
    else:
        entry.pop("base_url", None)
    if model.strip():
        entry["model"] = model.strip()
    else:
        entry.pop("model", None)
    data[provider] = entry
    _write(data)


def clear_key(provider: str) -> None:
    data = _read()
    entry = _entry(data, provider)
    if not entry:
        return
    entry.pop("key", None)
    if entry:
        data[provider] = entry
    else:
        data.pop(provider, None)
    _write(data)
