"""
系统区：导出、自检、调用与成本。

这些原先只有命令行有。导出尤其别扭 —— 零基础使用文档里，
它是唯一一处还要求用户打开终端敲命令的步骤。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

from flask import Blueprint, redirect, render_template, request, url_for

from core.skillkit.exporter import export_claude, export_codex, export_plain
from web.deps import CALL_LOG, ROOT, SKILLS

bp = Blueprint("system", __name__, url_prefix="/system")

EXPORT_DIR = ROOT / "export"
FORMATS = {
    "claude": ("Claude Code", export_claude,
               "把各个文件夹复制到你项目的 .claude/skills/ 下即可。"),
    "codex": ("Codex / 通用 agent", export_codex,
              "每个 skill 是自包含单文件，另有 AGENTS.md 说明整条流水线。"),
    "plain": ("纯 Markdown", export_plain,
              "复制粘贴到任何 AI 对话框里都能用。"),
}


@bp.route("/export")
def export_page():
    made = {}
    for k in FORMATS:
        d = EXPORT_DIR / k
        made[k] = sorted(p.name for p in d.iterdir()) if d.is_dir() else []
    return render_template("export.html", formats=FORMATS, made=made,
                           out=EXPORT_DIR)


@bp.post("/export")
def do_export():
    fmts = request.form.getlist("fmt") or list(FORMATS)
    for k in fmts:
        d = EXPORT_DIR / k
        if d.exists():
            shutil.rmtree(d)
        FORMATS[k][1](SKILLS, d)
    return redirect(url_for("system.export_page"))


@bp.route("/check")
def check_page():
    return render_template("check.html", output=None)


@bp.post("/check")
def run_check():
    """
    跑一遍 avctl smoke。走子进程而不是直接调函数 ——
    自检会往库里写 _smoke 数据、还会跑一整套测试，
    塞在工作台进程里跑容易互相干扰。

    timeout 收到 120 秒并接住超时：原先是 300 秒，而开发服务器是单线程的，
    自检期间整个工作台都打不开 —— 用户以为把它点死了，去关窗口重启。
    自检正常十几秒就完，跑到两分钟本身就说明有问题，该报出来而不是继续等。
    """
    cmd = [sys.executable, str(ROOT / "cli" / "avctl.py"), "smoke"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as e:
        partial = (e.stdout or "") + (e.stderr or "")
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        return render_template(
            "check.html", ok=False,
            output=partial + "\n\n[自检超过 120 秒没有结束，已中止。]"), 200
    return render_template("check.html",
                           output=(r.stdout or "") + (r.stderr or ""),
                           ok=r.returncode == 0)


RECENT = 60          # 明细表只展示最近这些条


@bp.route("/cost")
def cost_page():
    """
    调用与成本。calls.jsonl 一直在记 token 和耗时，但从来没有视图。
    这里只做可见，不做限额 —— 成本护栏是另一件事。

    逐行读而不是 read_text().splitlines()：这个文件只增不减，
    每次调模型加一行。整份读进内存意味着用得越久这一页越慢，
    最后在某次点击时把工作台撑爆 —— 而它只是个只读的统计页。
    """
    rows, totals = [], {"calls": 0, "in": 0, "out": 0, "sec": 0.0}
    by_skill = {}
    if CALL_LOG.exists():
        with CALL_LOG.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(r, dict):
                    continue
                totals["calls"] += 1
                totals["in"] += r.get("in_tokens", 0) or 0
                totals["out"] += r.get("out_tokens", 0) or 0
                totals["sec"] += r.get("elapsed", 0) or 0
                s = by_skill.setdefault(r.get("skill") or "-",
                                        {"calls": 0, "in": 0, "out": 0})
                s["calls"] += 1
                s["in"] += r.get("in_tokens", 0) or 0
                s["out"] += r.get("out_tokens", 0) or 0
                # 只留住要展示的那一小段，读过的行随即丢掉。
                rows.append(r)
                if len(rows) > RECENT:
                    del rows[0]
    return render_template("cost.html", rows=rows[::-1], totals=totals,
                           by_skill=sorted(by_skill.items(),
                                           key=lambda kv: -kv[1]["in"]),
                           log=CALL_LOG)

