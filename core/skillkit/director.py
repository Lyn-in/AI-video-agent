"""
导演层。

架构上最关键的一点：**导演不是流水线上的一个节点。**
它是一个风格约束层，由引擎在调用每个工种 skill 时一并注入。

这样做的收益：同一个编剧 skill，在不同导演上下文下产出完全不同的剧本，
而 skill 本身保持通用。skill 不感知导演是谁，风格由导演层单独供给。

一个刻意的例外：**作家不吃导演上下文。**
因为导演是根据故事的题材来选的 —— 故事在前，导演在后。
让作家吃导演等于倒因为果。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# 导演 SKILL.md 的强制章节。与工种 skill 的五章节完全不同。
DIRECTOR_SECTIONS = [
    "擅长题材",
    "叙事偏好",
    "镜头语言",
    "对白风格",
    "禁忌清单",
]

# 不接受导演上下文的 skill。故事在前，导演在后。
DIRECTOR_BLIND_SKILLS = {"writer"}


@dataclass
class Director:
    id: str
    name: str
    root: Path
    manifest: dict
    doc: str

    @property
    def genre_tags(self) -> list[str]:
        return self.manifest.get("genre_tags", [])

    @property
    def one_line(self) -> str:
        return self.manifest.get("one_line", "")

    @classmethod
    def load(cls, root: str | Path) -> "Director":
        root = Path(root)
        mf = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        doc = (root / "SKILL.md").read_text(encoding="utf-8")
        return cls(id=mf["id"], name=mf.get("name", mf["id"]),
                   root=root, manifest=mf, doc=doc)

    def validate(self) -> list[str]:
        errs = []
        m = self.manifest
        if m.get("type") != "director":
            errs.append(f"manifest.type: 导演包应为 'director'，实际 {m.get('type')!r}")
        if not m.get("genre_tags"):
            errs.append("manifest.genre_tags: 缺失。导演匹配器靠它推荐，不能为空")
        if not m.get("one_line"):
            errs.append("manifest.one_line: 缺失一句话风格概括")

        headings = {h.replace(" ", "") for h in
                    re.findall(r"^#{1,4}\s*(.+?)\s*$", self.doc, re.M)}
        for sec in DIRECTOR_SECTIONS:
            if sec.replace(" ", "") not in headings:
                errs.append(f"SKILL.md: 缺少导演强制章节「{sec}」")

        # 禁忌清单必须真有内容 —— 说不出「我绝不这么拍」的导演没有风格
        taboo = self._section("禁忌清单")
        if taboo and len(taboo) < 40:
            errs.append("SKILL.md 禁忌清单: 内容过少。说不出"
                        "「我绝不这么拍」的导演等于没有风格，"
                        "注入下去也产生不了可分辨的差异")
        return errs

    def _section(self, name: str) -> str:
        pat = re.compile(
            rf"^#{{1,4}}\s*{re.escape(name)}\s*$(.*?)(?=^#{{1,4}}\s|\Z)",
            re.M | re.S)
        m = pat.search(self.doc)
        if not m:
            return ""
        body = re.sub(r"<!--.*?-->", "", m.group(1), flags=re.S)
        return body.strip()

    def context_block(self) -> str:
        """注入给工种 skill 的风格约束段。"""
        return (
            f"# 导演风格约束（本次由「{self.name}」执导）\n\n"
            f"以下约束的优先级**高于**你自己的方法论倾向。"
            f"当你的默认做法与本约束冲突时，服从本约束。\n"
            f"但它不能让你越界：约束只改变你「怎么做」，不扩大你「做什么」。\n"
            f"例如导演要求长镜头，编剧仍然不写镜头，"
            f"而是把该场的动作组织得连贯、不依赖切分。\n\n"
            f"{self.doc}\n"
        )

    def match_score(self, tags: list[str]) -> tuple[int, list[str]]:
        """与故事题材标签的匹配度。返回 (命中数, 命中的标签)。"""
        mine = {t.lower() for t in self.genre_tags}
        hit = [t for t in (tags or []) if t.strip().lower() in mine]
        return len(hit), hit


def discover_directors(skills_dir: str | Path) -> list[Director]:
    d = Path(skills_dir) / "directors"
    if not d.is_dir():
        return []
    out = []
    for mf in sorted(d.glob("*/manifest.json")):
        try:
            out.append(Director.load(mf.parent))
        except (KeyError, json.JSONDecodeError):
            continue
    return out


def recommend(directors: list[Director], tags: list[str]) -> list[dict]:
    """
    导演匹配器：按题材标签给推荐 + 理由。
    只推荐，不自动选定 —— 最终由人拍板。
    """
    rows = []
    for d in directors:
        n, hit = d.match_score(tags)
        rows.append({
            "id": d.id, "name": d.name, "score": n, "hit": hit,
            "one_line": d.one_line,
            "reason": (f"命中题材 {'、'.join(hit)}" if hit
                       else "与本次题材无直接匹配"),
        })
    rows.sort(key=lambda r: -r["score"])
    return rows


DIRECTOR_TEMPLATE = """# 导演：{name}（{did}）

<!-- 导演不是工种 skill，它不产出任何契约产物。
     它是一份风格约束文档，会被注入到编剧、选角、场务、分镜的调用里。

     写作要点：写「怎么拍」，不写「拍什么」。
     题材、故事、人物都不归你管，你只决定同一个故事被处理成什么样子。 -->

## 擅长题材

<!-- 供导演匹配器使用。写清楚为什么这几个题材适合你的处理方式。 -->

## 叙事偏好

<!-- 节奏、结构、视角习惯、信息释放的快慢。
     这一节会影响编剧的分场和信息设计。 -->

## 镜头语言

<!-- 景别偏好、机位习惯、镜头运动、单镜时长倾向、色彩倾向。
     这一节主要影响分镜，也会影响选角和场务的选型方向。 -->

## 对白风格

<!-- 台词密度、长短、留白程度、是否允许直说。
     这一节影响编剧。 -->

## 禁忌清单

<!-- 最重要的一节。写清楚「我绝不这么拍」。

     说不出禁忌的导演等于没有风格，注入下去产生不了可分辨的差异，
     M3 的盲测就会失败。校验器会检查本节是否有实质内容。

     禁忌要具体到可执行，不要写「不要低级」这种废话。 -->
"""


def scaffold_director(skills_dir: str | Path, did: str, name: str,
                      genre_tags: list[str] | None = None,
                      one_line: str = "") -> Path:
    root = Path(skills_dir) / "directors" / did
    if root.exists():
        raise FileExistsError(f"导演已存在: {root}")
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({
        "id": did, "name": name, "type": "director",
        "version": "0.1.0",
        "one_line": one_line,
        "genre_tags": genre_tags or [],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "SKILL.md").write_text(
        DIRECTOR_TEMPLATE.format(name=name, did=did), encoding="utf-8")
    return root
