"""
契约浏览器。

契约是平台的接口协议，早于所有 skill 存在，但界面上一直完全看不见 ——
只有 `avctl contract list / show` 能看。审核产物时要对照字段规格，
总不能让人去开终端。
"""

from __future__ import annotations

from flask import Blueprint, abort, render_template

from core import contracts

bp = Blueprint("assets", __name__)


@bp.route("/contracts")
def contracts_index():
    rows = [{"name": name, "c": c} for name, c in contracts.REGISTRY.items()]
    return render_template("contracts.html", rows=rows,
                           order=contracts.PIPELINE_ORDER)


@bp.route("/contracts/<name>")
def contract_detail(name):
    try:
        c = contracts.get(name)
    except KeyError:
        abort(404)
    return render_template("contract_detail.html", c=c, spec=c.describe())
