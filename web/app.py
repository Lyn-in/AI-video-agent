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

import sys
from pathlib import Path

from flask import Flask

# 直接 `python3 web/app.py` 起动时，sys.path 上只有 web/，import web.* 会失败。
# 经 avctl web / 启动器进来的路径本来就带着项目根，这里是兜底。
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from web.deps import store                                    # noqa: E402
from web.nav import ZONES, zone_of                            # noqa: E402
from web.views import (                                       # noqa: E402
    artifacts, assets, directors, fragments, series, settings, skills,
    system,
)

BLUEPRINTS = (series.bp, artifacts.bp, skills.bp, directors.bp, settings.bp,
              assets.bp, system.bp, fragments.bp)


def create_app() -> Flask:
    app = Flask(__name__)
    store.init()
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)

    @app.context_processor
    def inject_nav():
        # 导航结构给所有模板用，省得每个视图都往 render_template 里塞一遍。
        return {"zones": ZONES, "zone_of": zone_of}

    @app.template_filter("shortid")
    def shortid(v):
        return (v or "")[-6:]

    return app


if __name__ == "__main__":
    # debug 默认关闭：Werkzeug 调试器允许在页面上执行代码，
    # 而工作台目前没有任何鉴权，开着等于开后门。
    # 需要调试用 avctl web --debug。
    create_app().run(host="127.0.0.1", port=5001, debug=False)
