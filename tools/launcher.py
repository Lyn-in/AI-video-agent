#!/usr/bin/env python3
"""
一键启动器。

双击 启动.command（Mac）或 启动.bat（Windows）会跑到这里。
它负责把所有零碎步骤自动做掉：装依赖、引导填密钥、建数据库、开浏览器、起服务。

设计原则：**每一步失败都要说人话**，告诉人该做什么，
而不是抛一个 traceback 让人不知所措。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 5001

BAR = "=" * 52


def say(msg=""):
    print(msg, flush=True)


def die(title: str, *lines: str):
    say()
    say(BAR)
    say(f"  {title}")
    say(BAR)
    for l in lines:
        say(f"  {l}")
    say()
    input("  按回车键关闭这个窗口。")
    sys.exit(1)


def step(n, total, text):
    say(f"[{n}/{total}] {text}")


# ---------- 1. Python 版本 ----------
def check_python():
    if sys.version_info < (3, 10):
        die("Python 版本太旧",
            f"当前是 {sys.version_info.major}.{sys.version_info.minor}，需要 3.10 以上。",
            "去 python.org 下载新版重装即可。",
            "Windows 安装时记得勾选 Add Python to PATH。")


# ---------- 2. 依赖 ----------
def ensure_flask():
    try:
        import flask  # noqa: F401
        return "已安装"
    except ImportError:
        pass
    say("      首次运行，正在安装网页所需组件（约半分钟）…")
    for args in (["--break-system-packages"], []):
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "flask", "-q", *args],
            capture_output=True, text=True)
        if r.returncode == 0:
            return "刚装好"
    die("组件安装失败",
        "可能是网络问题，或者电脑禁止了安装。",
        "可以手动在命令行里跑这一行再重试：",
        f"  {sys.executable} -m pip install flask --break-system-packages")


# ---------- 3. 密钥 ----------
# 密钥不在这个黑窗口里问——填在网页的「密钥设置」页，支持任意厂商，
# 改起来也不用重启程序。这里只做两件事：检查有没有配好、把老用户的
# config/secrets.env 一次性搬进新的密钥库。
def migrate_legacy_secrets_env():
    f = ROOT / "config" / "secrets.env"
    if not f.exists():
        return
    sys.path.insert(0, str(ROOT))
    from core.gateway import keystore
    from core.gateway.client import PROVIDERS
    env_to_provider = {cfg["key_env"]: p for p, cfg in PROVIDERS.items()}
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        provider = env_to_provider.get(k)
        if provider and v and "xxx" not in v and not keystore.get_key(provider, k):
            keystore.set_key(provider, v)


def has_any_key() -> bool:
    sys.path.insert(0, str(ROOT))
    from core.gateway import keystore
    from core.gateway.client import PROVIDERS
    return any(keystore.get_key(p, cfg["key_env"])
               for p, cfg in PROVIDERS.items())


# ---------- 4. 数据库 ----------
def ensure_db():
    sys.path.insert(0, str(ROOT))
    from core.store.db import Store
    Store(ROOT / "config" / "platform.db").init()
    for d in ("skills", "projects"):
        (ROOT / d).mkdir(exist_ok=True)


# ---------- 5. 端口 ----------
def pick_port(start=PORT) -> int:
    import socket
    for p in range(start, start + 12):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    die("找不到可用端口",
        "5001 到 5012 都被占用了。关掉一些程序再试。")


def main():
    os.chdir(ROOT)
    total = 5
    say()
    say(BAR)
    say("  AI 视频生产工作台")
    say(BAR)
    say()

    step(1, total, "检查运行环境…")
    check_python()
    say(f"      Python {sys.version_info.major}.{sys.version_info.minor} 可用")

    step(2, total, "检查网页组件…")
    say(f"      {ensure_flask()}")

    step(3, total, "检查 API 密钥…")
    migrate_legacy_secrets_env()
    has_key = has_any_key()
    say("      已配置" if has_key else "      还没配置，进网页后点「密钥设置」填")

    step(4, total, "准备数据…")
    ensure_db()
    say("      完成")

    step(5, total, "启动网页…")
    port = pick_port()
    url = f"http://127.0.0.1:{port}"

    def open_later():
        time.sleep(1.6)
        try:
            webbrowser.open(url)
        except Exception:                                     # noqa: BLE001
            pass
    threading.Thread(target=open_later, daemon=True).start()

    say()
    say(BAR)
    say(f"  网页地址: {url}")
    say("  浏览器会自动打开。没打开的话，手动复制上面这行地址。")
    say()
    if not has_key:
        say("  ⚠ 还没配置密钥，只能看不能生成。")
        say("     进网页后点右上角「密钥设置」，填一把任意厂商的 API Key 就行。")
        say()
    say("  ★ 这个窗口不要关，关了网页就打不开了。")
    say("  用完了在这个窗口按 Ctrl + C 结束。")
    say(BAR)
    say()

    from web.app import create_app
    try:
        create_app().run(host="127.0.0.1", port=port, debug=False)
    except KeyboardInterrupt:
        say("\n  已停止。做好的东西都保存着，下次双击启动即可。\n")
    except Exception as e:                                    # noqa: BLE001
        die("网页启动失败", str(e),
            "把这段报错发给开发者即可定位。")


if __name__ == "__main__":
    main()
