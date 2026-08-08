"""
提示词拼装。

原先长在 cli/avctl.py 里叫 _build_prompt，web 层反过来 import 这个私有函数 ——
依赖方向是反的。提示词拼装是领域逻辑，不属于任何一个界面，
放这里让 web 和 cli 都做 core 的客户端。

四段式上下文：导演约束 + skill本体(+题材模块) + 输出格式规格 + references
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from core.contracts import get as get_contract
from core.skillkit.director import DIRECTOR_BLIND_SKILLS

REF_HEAD = {"S": "S级范本", "A": "A级参考", "N": "反面教材（禁止模仿）"}


@dataclass
class Prompt:
    system: str
    user: str
    # 题材匹配结果一并带出来：调用方要拿它做提示，不必再算一遍。
    genre_hit: list = field(default_factory=list)
    genre_miss: list = field(default_factory=list)


def resolve_genre_tags(genre_tags: list[str] | None,
                       inputs: list[dict]) -> list[str]:
    """题材标签：显式传入优先，否则从上游产物里读。"""
    tags = list(genre_tags or [])
    if not tags:
        for a in inputs:
            tags += a.get("payload", {}).get("genre_tags", [])
    return tags


def build_prompt(pkg, brief: str, inputs: list[dict],
                 genre_tags: list[str] | None = None,
                 director=None) -> Prompt:
    """
    导演约束放在最前面，因为它的优先级高于 skill 自身的方法论倾向。
    """
    c = get_contract(pkg.output_contract)
    tags = resolve_genre_tags(genre_tags, inputs)

    parts = []
    if director and pkg.id not in DIRECTOR_BLIND_SKILLS:
        parts.append(director.context_block())
        parts.append("\n\n---\n\n")
    parts.append(pkg.skill_md)

    hit, miss = pkg.select_genres(tags)
    if hit:
        parts.append("\n\n---\n\n# 题材模块（优先级高于方法论层通用表述）\n")
        for g in hit:
            parts.append(f"\n<!-- 模块 {g.key} -->\n")
            parts.append(g.read())
    if miss:
        parts.append(
            f"\n\n注意：题材标签 {miss} 尚无对应模块，本次在无题材支撑下作业。"
            f"请按通用方法论处理，并在产出中说明这一点。"
        )

    parts.append("\n\n---\n\n# 输出格式要求\n")
    parts.append(f"你必须只输出一个 JSON 对象，符合契约 {c.name}（{c.cn_name}）。")
    parts.append("不要输出任何解释、前言或 markdown 围栏。字段规格如下：\n")
    parts.append(c.describe())
    parts.append(
        "\n此外，在 JSON 顶层加一个 `_references_cited` 数组，逐条申报本次调用了"
        "哪些参考资料、用途（align_voice / borrow_structure / avoid）、"
        "以及各自解决了什么问题。没有引用则给空数组。"
    )

    if pkg.refs:
        parts.append("\n\n---\n\n# 参考资料\n")
        for r in pkg.refs:
            parts.append(f"\n## [{r.ref_id}] {r.title}"
                         f"（{REF_HEAD[r.grade]}，用途 {r.purpose}）\n")
            parts.append(r.read())
        parts.append(
            "\n\n硬护栏：禁止复用参考资料中的具体台词、人名、地名、独有设定，"
            "只能复用机制与模式。"
        )

    user_parts = []
    for a in inputs:
        cc = get_contract(a["contract"])
        user_parts.append(f"# 上游产物：{cc.cn_name}\n")
        user_parts.append(json.dumps(a["payload"], ensure_ascii=False, indent=2))
    if brief:
        user_parts.append(f"\n# 本次任务\n\n{brief}")

    return Prompt(system="\n".join(parts),
                  user="\n".join(user_parts) or "请开始。",
                  genre_hit=hit, genre_miss=miss)
