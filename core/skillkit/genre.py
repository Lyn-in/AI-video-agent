"""
题材模块脚手架。

加题材是高频操作，且要同时改作家和编剧两个 skill 的 genres/ 和 index.json，
手工做容易漏一处 —— 漏了的表现是「明明建了模块却不生效」，很难查。
所以做成命令。
"""

from __future__ import annotations

import json
from pathlib import Path

# 哪些 skill 吃题材维度。选角/场务/分镜不在此列 ——
# 它们的变异来源是导演，开题材维度会和导演层抢管辖权。
GENRE_AWARE_SKILLS = {
    "writer": "writer",          # 完整题材模块：结构与判断
    "playwright": "playwright",  # 薄模块：只管对白语感
}


def add_genre(skills_dir: str | Path, key: str, title: str,
              aliases: list[str] | None = None,
              only: list[str] | None = None) -> list[Path]:
    skills_dir = Path(skills_dir)
    aliases = aliases or []
    if title not in aliases:
        aliases = [title] + aliases

    targets = only or list(GENRE_AWARE_SKILLS)
    created = []

    for sid in targets:
        sroot = skills_dir / sid
        if not sroot.is_dir():
            continue
        gdir = sroot / "genres"
        gdir.mkdir(exist_ok=True)

        mdp = gdir / f"{key}.md"
        if not mdp.exists():
            tpl = WRITER_TEMPLATE if sid == "writer" else PLAYWRIGHT_TEMPLATE
            mdp.write_text(tpl.format(title=title), encoding="utf-8")
            created.append(mdp)

        # 登记别名
        ipath = gdir / "index.json"
        index = {}
        if ipath.exists():
            try:
                index = json.loads(ipath.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                index = {}
        index[f"{key}.md"] = {"key": key, "title": title, "aliases": aliases}
        ipath.write_text(json.dumps(index, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    return created


WRITER_TEMPLATE = """# 题材模块：{title}

> 只写本题材独有的判断标准和常见死法。方法论层已有的不要重复。
> 与方法论层冲突时，以本模块为准。
>
> 写作提示：这里蒸馏的是「判断」不是「知识」。模型已经知道教科书上的东西，
> 它不知道的是你认为什么才算过关。写完自问：这一条模型自己想得到吗？
> 想得到的删掉。

## 这个题材的核心难题

<!-- 一段话。这个题材最容易失败的地方在哪里，为什么。
     写不出核心难题，说明还没想清楚要不要单开这个模块。 -->

## 判断标准

<!-- 3-5 条。每条一个小标题 + 判断方法。
     必须可执行：给出自检的具体动作，不要只给原则。
     反例：「人物要立体」——没法执行。
     正例：「把笑点换给另一个人物执行，效果一样就是外挂的」——可执行。 -->

## 常见死法（中一条就重写）

<!-- 逐条列出。这一节往往比判断标准更值钱：
     纠偏知识是模型最缺的，因为它默认会往平庸的方向走。
     每条写清楚「症状」，让人一眼能对照产出检查。 -->

## 结构上的偏好

<!-- 这个题材的三幕配比与通用结构有何不同。
     必须给出短时长版本（180秒以内）的压缩方案 ——
     短剧装不下完整三幕，得说清楚砍哪里。 -->
"""

PLAYWRIGHT_TEMPLATE = """# 对白语感模块：{title}

> 只管对白语感，不管结构。结构的题材差异由作家层处理，这里不重复。
> 篇幅要薄，600 字上下即可。写长了会挤占上下文，稀释注意力。

## 这个题材的对白特征

<!-- 一两句话。这个题材的台词跟别的题材最大的不同是什么。 -->

## 具体标准

<!-- 4-5 条。每条一句判断 + 一句为什么。
     只写对白层面的：措辞、节奏、密度、留白、信息传递方式。
     不要写「这个题材的故事应该如何」——那是作家的活。 -->

## 常见死法

<!-- 5-6 条。台词层面的典型翻车，写成可对照检查的症状。 -->
"""
