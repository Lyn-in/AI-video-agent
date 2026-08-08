"""导演库。导演是注入层，不是流水线节点。"""

from __future__ import annotations

from flask import Blueprint, render_template, request

from core.skillkit.director import discover_directors, recommend
from web.deps import SKILLS

bp = Blueprint("directors", __name__)


@bp.route("/directors")
def index():
    """
    导演库 + 匹配器。

    recommend() 一直存在但界面上够不着 —— 只有 avctl director match 能跑，
    界面里就一个干巴巴的下拉。匹配器只给排序和理由，选谁仍然是人定的。
    """
    ds = discover_directors(SKILLS)
    tags = [t.strip() for t in
            (request.args.get("genre") or "").replace("，", ",").split(",")
            if t.strip()]
    return render_template(
        "directors.html",
        ds=[{"d": d, "errs": d.validate()} for d in ds],
        tags=tags,
        matches=recommend(ds, tags) if (tags and ds) else [])
