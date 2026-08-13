"""密钥设置：多厂商 API 配置全在网页上填，不用命令行。"""

from __future__ import annotations

from flask import Blueprint, abort, redirect, render_template, request, url_for

from core.gateway import keystore
from core.gateway.client import PROVIDERS
from web import security
from web.deps import render_error

bp = Blueprint("settings", __name__, url_prefix="/settings")


@bp.route("")
def index():
    st = keystore.status(PROVIDERS)
    rows = [{"id": pid, **cfg, **st[pid]} for pid, cfg in PROVIDERS.items()]
    return render_template("settings.html", rows=rows,
                           has_password=security.has_password())


@bp.post("/password")
def set_password():
    pw = (request.form.get("password") or "").strip()
    if len(pw) < 6:
        # abort(400) 在这里等于「点了保存，页面报错，不说为什么」。
        # 长度要求是我们自己定的，就得自己讲清楚。
        return render_error("口令至少 6 位，换一个再试。"), 400
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
    keystore.set_provider_config(
        provider,
        base_url=(request.form.get("base_url") or "").strip(),
        model=(request.form.get("model") or "").strip(),
        key=(request.form.get("key") or "").strip(),
    )
    return redirect(url_for("settings.index"))


@bp.post("/<provider>/clear")
def clear(provider):
    if provider not in PROVIDERS:
        abort(404)
    keystore.clear_key(provider)
    return redirect(url_for("settings.index"))
