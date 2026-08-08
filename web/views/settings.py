"""密钥设置：多厂商 API Key 全在网页上填，不用命令行。"""

from __future__ import annotations

from flask import Blueprint, abort, redirect, render_template, request, url_for

from core.gateway import keystore
from core.gateway.client import PROVIDERS

bp = Blueprint("settings", __name__, url_prefix="/settings")


@bp.route("")
def index():
    st = keystore.status(PROVIDERS)
    rows = [{"id": pid, **cfg, **st[pid]} for pid, cfg in PROVIDERS.items()]
    return render_template("settings.html", rows=rows)


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
