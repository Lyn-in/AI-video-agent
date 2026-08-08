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
def ensure_key() -> str:
    cfg = ROOT / "config"
    cfg.mkdir(exist_ok=True)
    f = cfg / "secrets.env"

    key = ""
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip().removeprefix("export ").strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")

    if key and key.startswith("sk-") and "xxx" not in key:
        return key

    say()
    say("  还没有填 API 密钥。")
    say("  去 console.anthropic.com 创建一把，复制过来粘贴在下面。")
    say("  （粘贴后按回车。密钥只存在你自己电脑上。）")
    say()
    try:
        entered = input("  密钥: ").strip()
    except (EOFError, KeyboardInterrupt):
        entered = ""

    if not entered:
        say()
        say("  没填也可以先进去看看界面，但不能生成内容。")
        say("  以后想填，重新双击启动，或者直接编辑 config/secrets.env。")
        say()
        return ""

    if not entered.startswith("sk-"):
        say("  ⚠ 这串东西不像密钥（正常应该以 sk- 开头），先按你填的存下了。")

    f.write_text(f'export ANTHROPIC_API_KEY={entered}\n'
                 f'# export DEEPSEEK_API_KEY=\n', encoding="utf-8")
    try:
        os.chmod(f, 0o600)      # 只有自己能读
    except OSError:
        pass
    say("  ✓ 已保存到 config/secrets.env，下次不用再填。")
    return entered


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
    key = ensure_key()
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
        say("      已就绪")

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
    if not key:
        say("  ⚠ 没有密钥，只能看不能生成。")
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
