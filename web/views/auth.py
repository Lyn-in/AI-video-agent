"""
登录与口令设置。

默认不开：单人本机用加登录纯属给自己添堵。
一旦在「系统 → 密钥」里设了口令，或者把工作台绑到非 localhost，
它才生效。
"""

from __future__ import annotations

from flask import (Blueprint, redirect, render_template, request, session,
                   url_for)

from web import security

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not security.has_password():
        return redirect(url_for("series.inbox"))
    if session.get("authed"):
        return redirect(url_for("series.inbox"))

    err = None
    if request.method == "POST":
        # 登录表单同样验 CSRF：token 在 GET 渲染这一页时就已经种进会话，
        # 顺带挡掉「诱导你登进攻击者账号」这类登录 CSRF。
        if security.check_password(request.form.get("password", "")):
            session["authed"] = True
            session.permanent = True
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") else
                            url_for("series.inbox"))
        err = "口令不对。"
    return render_template("login.html", err=err)


@bp.post("/logout")
def logout():
    session.pop("authed", None)
    return redirect(url_for("auth.login"))
