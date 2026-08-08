"""
Skill 导出器。

平台真正的资产是 skill —— 那套方法论、判断标准、禁令、题材模块、导演约束。
平台只是把它们编排起来的壳子。所以 skill 必须能单独拿出来用。

导出的难点在于：**平台运行时会往上下文里注入四样东西**
（导演约束、题材模块、契约字段规格、references）。
脱离平台之后没有引擎做这件事，所以导出的 skill 必须自带这些，
否则拿出去就是一份残缺的提示词。

三种目标格式：
  claude   Claude Code 的 .claude/skills/ 结构（带 YAML frontmatter）
  codex    单文件 AGENTS.md 风格，适合 Codex / 通用 agent
  plain    纯 Markdown，粘到任何对话框里都能用
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from ..contracts import get as get_contract
from .director import DIRECTOR_BLIND_SKILLS, discover_directors
from .package import SkillPackage, discover

# 只有真正消费导演上下文的工种才随包附带导演约束。
# 作家不吃（故事在前导演在后）；拉片员不在流水线上，也不吃。
def _takes_director(pkg: SkillPackage) -> bool:
    return (pkg.id not in DIRECTOR_BLIND_SKILLS
            and pkg.manifest.get("team") in ("content", "production"))

# 每个 skill 的触发描述。Claude Code 靠 description 判断何时启用，
# 写不好就永远触发不了，所以这里逐个手写而不是从 SKILL.md 里截。
TRIGGERS = {
    "writer": "把一个念头、素材或主题变成完整的故事档案（logline、三幕梗概、"
              "情感内核、人物功能）。当用户说「想个故事」「做选题」「写个短剧创意」"
              "「基于这个素材想个片子」时使用。这是影视创作流水线的第一步。",
    "playwright": "把故事档案展开成可拍摄的分场剧本（场次、动作、对白）。"
                  "当用户说「写剧本」「把这个故事写成剧本」「展开成剧本」时使用。"
                  "严格不写镜头、光线、转场——那是分镜的活。",
    "casting": "为剧本里每个角色写视觉锚定描述和参考图提示词，解决 AI 生成的"
               "跨镜头一致性问题。当用户说「做角色卡」「固定角色长相」"
               "「生成角色参考图提示词」「角色一致性」时使用。",
    "set_dresser": "为剧本里每个场景写空间锚定描述和场景图提示词，保证跨镜头"
                   "布局不漂移。当用户说「做场景设计」「场景一致性」"
                   "「生成场景图提示词」时使用。",
    "storyboard": "把剧本拆成镜头，产出可直接投喂视频模型的提示词。"
                  "当用户说「做分镜」「分镜表」「拆镜头」「生成视频提示词」时使用。"
                  "必须先有角色板和场景板。",
    "film_analyst": "把一部影片拆成可复用的机制笔记，按消费者分流成编剧向、"
                    "选角向、分镜向三份。当用户说「拉片」「拉片笔记」"
                    "「分析这部片的手法」时使用。",
}

HEADER_NOTE = """
> **本文件是从「AI视频Agent平台」导出的独立版本。**
> 平台运行时会自动注入题材模块、导演约束和输出格式规格；
> 脱离平台后这些已内嵌在本文件中，所以可以单独使用。
"""


def _frontmatter(name: str, desc: str) -> str:
    d = desc.replace("\n", " ").strip()
    return f"---\nname: {name}\ndescription: {d}\n---\n\n"


def _contract_spec_block(contract_name: str | None) -> str:
    if not contract_name:
        return ""
    c = get_contract(contract_name)
    return (f"\n\n---\n\n## 输出格式（必须严格遵守）\n\n"
            f"输出一个 JSON 对象，符合 {c.cn_name} 规格：\n\n"
            f"```\n{c.describe()}\n```\n\n"
            f"（标 * 的是必填字段；未列出的字段可自由扩展。）\n\n"
            f"另外在 JSON 顶层加 `_references_cited` 数组，"
            f"申报本次参考了哪些资料、用途、解决了什么问题；没有则给空数组。\n")


def _genre_block(pkg: SkillPackage, embed: bool) -> str:
    if not pkg.genres:
        return ""
    lines = ["\n\n---\n\n## 题材模块\n",
             "本 skill 带有题材模块。**开始工作前，先判断本次题材，"
             "然后加载对应模块**——模块里的判断标准优先级高于上面的通用方法论。\n",
             "\n可用模块：\n"]
    for g in pkg.genres:
        alias = "、".join(a for a in g.aliases if a != g.title)
        lines.append(f"- **{g.title}**（{g.key}）"
                     f"{f'，也叫 {alias}' if alias else ''}"
                     f" → `genres/{g.key}.md`")
    lines.append("\n如果本次题材没有对应模块，按通用方法论处理，"
                 "并在产出中说明本次缺少题材支撑。\n")
    if embed:
        lines.append("\n<details>\n<summary>全部模块内容（点开查看）</summary>\n")
        for g in pkg.genres:
            lines.append(f"\n### 【题材模块】{g.title}\n\n{g.read()}\n")
        lines.append("\n</details>\n")
    return "\n".join(lines)


def _director_block(directors, embed: bool) -> str:
    if not directors:
        return ""
    lines = ["\n\n---\n\n## 导演风格（可选）\n",
             "如果用户指定了导演，加载对应的风格约束，"
             "其优先级**高于**本 skill 自身的方法论倾向。\n",
             "但它只改变「怎么做」，不扩大「做什么」——"
             "例如导演要求长镜头，编剧仍然不写镜头，"
             "而是把动作组织得连贯、不依赖切分。\n\n可用导演：\n"]
    for d in directors:
        lines.append(f"- **{d.name}**（{d.id}）：{d.one_line} "
                     f"→ `directors/{d.id}.md`")
    if embed:
        lines.append("\n<details>\n<summary>全部导演约束（点开查看）</summary>\n")
        for d in directors:
            lines.append(f"\n### 【导演】{d.name}\n\n{d.doc}\n")
        lines.append("\n</details>\n")
    return "\n".join(lines)


def _pipeline_note(pkg: SkillPackage) -> str:
    if not pkg.input_contracts:
        return ""
    names = "、".join(get_contract(c).cn_name for c in pkg.required_inputs)
    return (f"\n\n---\n\n## 上游依赖\n\n"
            f"本 skill 需要这些输入：**{names}**。\n\n"
            f"缺了不要硬做——缺角色板时会自己发明人物长相，"
            f"缺场景板时会自己发明场景，一致性从第一个镜头就崩。"
            f"应当先产出上游，或者明确告诉用户缺什么。\n")


# ============================================================
# 三种导出格式
# ============================================================

def export_claude(skills_dir: Path, out: Path) -> list[str]:
    """Claude Code 格式：.claude/skills/<name>/SKILL.md + 附属文件。"""
    out.mkdir(parents=True, exist_ok=True)
    directors = discover_directors(skills_dir)
    made = []

    for pkg in discover(skills_dir):
        d = out / pkg.id
        d.mkdir(parents=True, exist_ok=True)

        body = [_frontmatter(pkg.id, TRIGGERS.get(pkg.id, pkg.manifest.get("name", pkg.id))),
                HEADER_NOTE, pkg.skill_md,
                _pipeline_note(pkg),
                _genre_block(pkg, embed=False),
                _director_block(directors if _takes_director(pkg) else [],
                                embed=False),
                _contract_spec_block(pkg.output_contract)]
        (d / "SKILL.md").write_text("\n".join(body), encoding="utf-8")

        if pkg.genres:
            gd = d / "genres"
            gd.mkdir(exist_ok=True)
            for g in pkg.genres:
                shutil.copy2(g.path, gd / f"{g.key}.md")

        if directors and _takes_director(pkg):
            dd = d / "directors"
            dd.mkdir(exist_ok=True)
            for x in directors:
                (dd / f"{x.id}.md").write_text(x.doc, encoding="utf-8")

        made.append(pkg.id)

    (out / "README.md").write_text(_CLAUDE_README, encoding="utf-8")
    return made


def export_codex(skills_dir: Path, out: Path) -> list[str]:
    """Codex / 通用 agent：每个 skill 一个自包含单文件，全部内容内嵌。"""
    out.mkdir(parents=True, exist_ok=True)
    directors = discover_directors(skills_dir)
    made = []
    for pkg in discover(skills_dir):
        body = [f"# {pkg.manifest.get('name', pkg.id)}（{pkg.id}）\n",
                f"**何时使用**：{TRIGGERS.get(pkg.id, '')}\n",
                HEADER_NOTE, pkg.skill_md,
                _pipeline_note(pkg),
                _genre_block(pkg, embed=True),
                _director_block(directors if _takes_director(pkg) else [],
                                embed=True),
                _contract_spec_block(pkg.output_contract)]
        (out / f"{pkg.id}.md").write_text("\n".join(body), encoding="utf-8")
        made.append(pkg.id)
    (out / "AGENTS.md").write_text(_agents_md(skills_dir), encoding="utf-8")
    return made


def export_plain(skills_dir: Path, out: Path) -> list[str]:
    """纯 Markdown：粘到任何对话框都能用，不带任何格式约定。"""
    out.mkdir(parents=True, exist_ok=True)
    directors = discover_directors(skills_dir)
    made = []
    for pkg in discover(skills_dir):
        body = [f"# {pkg.manifest.get('name', pkg.id)}\n", pkg.skill_md,
                _genre_block(pkg, embed=True),
                _director_block(directors if _takes_director(pkg) else [],
                                embed=True),
                _contract_spec_block(pkg.output_contract)]
        (out / f"{pkg.id}.md").write_text("\n".join(body), encoding="utf-8")
        made.append(pkg.id)
    for d in directors:
        (out / f"导演-{d.id}.md").write_text(
            f"# 导演：{d.name}\n\n{d.doc}", encoding="utf-8")
    return made


def _agents_md(skills_dir: Path) -> str:
    pkgs = discover(skills_dir)
    lines = ["# AI 影视生产 · Agent 说明\n",
             "本目录是一套影视内容生产的方法论集合，"
             "从「AI视频Agent平台」导出。\n",
             "## 工种\n"]
    for p in pkgs:
        lines.append(f"- **{p.manifest.get('name')}**（`{p.id}.md`）："
                     f"{TRIGGERS.get(p.id, '').split('。')[0]}。")
    lines.append("\n## 流水线顺序\n")
    lines.append("```\n主题 → 故事档案 → 剧本 → 角色板 + 场景板 → 分镜表 → 出图出片\n```\n")
    lines.append("每一步的产出都应交人过目再进下一步。"
                 "上游没定稿就往下做，返工成本极高。\n")
    lines.append("\n## 三条贯穿全流程的纪律\n")
    lines.append("1. **职责边界**：编剧不写镜头，选角不管场景，分镜不发明人物长相。"
                 "越界会让下游失去调整空间。\n")
    lines.append("2. **锚定复用**：角色和场景的外观描述写一次，"
                 "下游一律引用原文，不要理解后自己重写——"
                 "每次重写都会漂移，几十个镜头就是几十张脸。\n")
    lines.append("3. **风格来自导演**：镜头语言、节奏、对白密度由导演约束决定，"
                 "不由题材决定。两边同时下指令会打架。\n")
    return "\n".join(lines)


_CLAUDE_README = """# 影视生产 Skills（Claude Code 版）

从「AI视频Agent平台」导出的独立 skill 集合。

## 怎么装

把本目录下的各个 skill 文件夹，复制到你项目的 `.claude/skills/` 下面：

```
你的项目/
└── .claude/
    └── skills/
        ├── writer/
        │   ├── SKILL.md
        │   └── genres/
        ├── playwright/
        ├── casting/
        ├── set_dresser/
        ├── storyboard/
        └── film_analyst/
```

装好后在 Claude Code 里直接说「帮我想个短剧故事」「把这个故事写成剧本」，
对应的 skill 会自动启用。

## 跟平台版的区别

- 平台版由引擎自动注入题材模块和导演约束；这里改成 SKILL.md 里写明，
  由模型自己按题材加载 `genres/` 下的对应文件、按指定导演加载 `directors/` 下的文件。
- 平台版有契约校验器强制拦截越权（比如编剧写了镜头会被打回）；
  这里只有文字约定，没有强制。要严格执法还是得用平台。
- 平台版有审核门、版本管理、质量飞轮；这里没有。

## 流水线

```
主题 → 故事档案 → 剧本 → 角色板 + 场景板 → 分镜表 → 出图出片
```

角色板和场景板必须先于分镜完成，否则分镜会自己发明人物和场景。
"""
