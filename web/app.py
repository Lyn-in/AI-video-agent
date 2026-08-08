"""
生产工作台。

设计取舍：Flask + Jinja2，不引入前端构建链。

视觉方向：通告单 / 场记板。
审核门在真实剧组里就是盖章，所以通过的节点盖一枚印章 —— 这是签名元素。

这个模块只做装配：注册 blueprint 与模板 filter。
路由在 web/views/ 下按领域分文件，共享依赖在 web/deps.py，
生成编排在 core/engine/runner.py（命令行走同一条路径）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from flask import (Flask, redirect, render_template, request, session,
                   url_for)
from markupsafe import Markup   # Flask 3 起不再从 flask 转出

# 直接 `python3 web/app.py` 起动时，sys.path 上只有 web/，import web.* 会失败。
# 经 avctl web / 启动器进来的路径本来就带着项目根，这里是兜底。
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from web import security                                      # noqa: E402
from web.deps import store                                    # noqa: E402
from web.nav import ZONES, zone_of                            # noqa: E402
from web.views import (                                       # noqa: E402
    artifacts, assets, directors, fragments, series, settings, skills,
    system,
)

BLUEPRINTS = (series.bp, artifacts.bp, skills.bp, directors.bp, settings.bp,
              assets.bp, system.bp, fragments.bp)

# 登录页自己不能要求登录，否则没人进得来。
OPEN_ENDPOINTS = {"auth.login", "static"}


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = security.secret_key()
    app.url_map.strict_slashes = False
    app.config.update(SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax")
    store.init()

    from web.views import auth
    app.register_blueprint(auth.bp)
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)

    @app.before_request
    def guard():
        # 1) 登录闸：只在设了口令时生效。本机自用不设口令 = 零摩擦。
        if security.has_password() and not session.get("authed"):
            if request.endpoint not in OPEN_ENDPOINTS:
                return redirect(url_for("auth.login", next=request.full_path))
        # 2) CSRF：所有写操作都验。默认常开，用户感知不到。
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if not security.token_ok(session, request.form, request.headers):
                return render_template("csrf.html"), 400

    # csrf_input 必须是 Jinja 全局而不是 context processor：
    # {% import %} 进来的宏拿不到 context，而节点卡正是个宏 ——
    # 做成 context processor 的话，卡片里的表单会静默丢掉 token。
    def csrf_token() -> str:
        return security.issue_token(session)

    def csrf_input() -> Markup:
        return Markup(f'<input type="hidden" name="{security.CSRF_FIELD}"'
                      f' value="{csrf_token()}">')

    try:
        _build = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_ROOT), stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:                                            # noqa: BLE001
        _build = "dev"
    app.jinja_env.globals.update(csrf_input=csrf_input, csrf_token=csrf_token,
                                 build_version=_build)

    @app.context_processor
    def inject_nav():
        return {"zones": ZONES, "zone_of": zone_of,
                "locked": security.has_password()}

    @app.template_filter("shortid")
    def shortid(v):
        return (v or "")[-6:]

    return app


if __name__ == "__main__":
    # debug 默认关闭：Werkzeug 调试器允许在页面上执行代码，
    # 配合可能未设的口令等于开后门。需要调试用 avctl web --debug。
    create_app().run(host="127.0.0.1", port=5001, debug=False)
