"""
HTMX 片段端点。

约定：全部挂在 /f/ 下，只返回 HTML 片段，不返回整页。
片段模板与整页共用同一组 macro（components/），
保证「页面上的卡」和「轮询换回来的卡」永远是同一份标记。
"""

from __future__ import annotations

from flask import Blueprint, request

from web.views.series import render_node, render_stepper
from web.views.skills import render_suggest

bp = Blueprint("fragments", __name__, url_prefix="/f")


@bp.route("/suggest/<sid>")
def suggest(sid):
    return render_suggest(sid)


@bp.route("/node/<sid>/<contract>")
def node(sid, contract):
    return render_node(sid, contract, request.args.get("ep", type=int) or 1)


@bp.route("/stepper/<sid>")
def stepper(sid):
    return render_stepper(sid, request.args.get("ep", type=int) or 1,
                          request.args.get("step"))
