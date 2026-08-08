"""导演库。导演是注入层，不是流水线节点。"""

from __future__ import annotations

from flask import Blueprint, render_template

from core.skillkit.director import discover_directors
from web.deps import SKILLS

bp = Blueprint("directors", __name__)


@bp.route("/directors")
def index():
    ds = [{"d": d, "errs": d.validate()} for d in discover_directors(SKILLS)]
    return render_template("directors.html", ds=ds)
