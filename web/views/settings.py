"""密钥设置：多厂商 API Key 全在网页上填，不用命令行。"""

from __future__ import annotations

from flask import Blueprint, abort, redirect, render_template, request, url_for

from core.gateway import keystore
from core.gateway.client import PROVIDERS
from web import security

bp = Blueprint("settings", __name__, url_prefix="/settings")


@bp.route("")
def index():
    st = keystore.status(PROVIDERS)
    rows = [{"id": pid, **cfg, **st[pid]} for pid, cfg in PROVIDERS.items()]
    return render_template("settings.html", rows=rows,
                           has_password=security.has_password())


@bp.post("/password")
def set_password():
    """
    设口令。本机自用不需要 —— 设了反而每次双击都要登一次。
    要把工作台给别人访问（哪怕只是同一个局域网）才用得上。
    """
    pw = (request.form.get("password") or "").strip()
    if len(pw) < 6:
        abort(400)
    security.set_password(pw)
    return redirect(url_for("settings.index"))


@bp.post("/password/clear")
def clear_password():
    security.clear_password()
    return redirect(url_for("settings.index"))


@bp.post("/<provider>/save")
def save(provider):
    if provider not in PROVIDERS:
        abort(404)
    value = (request.form.get("key") or "").strip()
    if value:
        keystore.set_key(provider, value)
    return redirect(url_for("settings.index"))


@bp.post("/<provider>/clear")
def clear(provider):
    if provider not in PROVIDERS:
        abort(404)
    keystore.clear_key(provider)
    return redirect(url_for("settings.index"))
