"""导演库。导演是注入层，不是流水线节点。"""

from __future__ import annotations

from flask import (Blueprint, abort, redirect, render_template, request,
                   url_for)

from core.skillkit.director import (DIRECTOR_SECTIONS, Director,
                                    discover_directors, recommend)
from web.deps import SKILLS, form_text

bp = Blueprint("directors", __name__, url_prefix="/directors")


def load_director(did: str) -> Director:
    """按 id 加载导演。路径必须落在 skills/directors/ 之内 —— 防目录穿越。"""
    base = (SKILLS / "directors").resolve()
    root = (base / did).resolve()
    if not str(root).startswith(str(base) + "/") or not root.is_dir():
        abort(404)
    return Director.load(root)


@bp.route("")
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


@bp.route("/<did>")
def detail(did):
    """
    导演详情。原先导演库只有卡片，点不进去也改不了 ——
    skill 能在界面上改 SKILL.md，导演却只能去翻文件，这是不对称的。
    """
    d = load_director(did)
    return render_template("director_detail.html", d=d, errs=d.validate(),
                           sections=DIRECTOR_SECTIONS)


@bp.post("/<did>/save")
def save(did):
    d = load_director(did)
    (d.root / "SKILL.md").write_text(form_text(request.form, "body"),
                                    encoding="utf-8")
    return redirect(url_for("directors.detail", did=did))
