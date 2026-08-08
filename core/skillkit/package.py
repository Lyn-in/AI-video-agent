"""
Skill 资产包：平台级 Skill 规范的代码执法。

规范要点（架构文档第二章）：
  skill包/
    ├── SKILL.md        必须含五个强制章节
    ├── manifest.json   元数据 + 输入输出契约 + 模型绑定
    ├── references/     私有学习库，支持分级
    ├── versions/       每次修改自动存档
    └── feedback/       人工修改 diff 归集（质量飞轮原料）

任何 skill —— 包括未来新增的 —— 不符合本规范，平台不接收。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..contracts import REGISTRY as CONTRACT_REGISTRY

# SKILL.md 的五个强制章节（架构文档 2.2）
REQUIRED_SECTIONS = [
    "角色定义",
    "职责边界",
    "方法论",
    "输入输出契约",
    "references调用方式",
]

SKILL_TYPES = ["worker", "director", "utility"]
TEAMS = ["content", "production", "direction", "supply"]

# reference 分级（架构文档 2.3）
REF_GRADES = ["S", "A", "N"]   # S级范本 / A级参考 / N=反面教材(negative)


class SkillError(Exception):
    pass


@dataclass
class RefItem:
    """references 目录里的一条笔记。"""
    ref_id: str
    path: Path
    grade: str = "A"
    purpose: str = "align_voice"      # align_voice | borrow_structure | avoid
    tags: dict = field(default_factory=dict)   # 四维标签
    title: str = ""

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")


@dataclass
class GenreModule:
    """
    题材知识模块。只写这个题材独有的判断标准和常见死法，
    不重复方法论层已有的内容。引擎按故事的 genre_tags 命中加载。

    为什么是模块不是独立 skill：分成多个 skill 会让方法论层出现多份拷贝，
    改一条质量红线要改多遍，漏改一处就开始漂移。
    """
    key: str
    path: Path
    aliases: list[str] = field(default_factory=list)
    title: str = ""

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def matches(self, tag: str) -> bool:
        t = tag.strip().lower()
        return t == self.key.lower() or t in [a.lower() for a in self.aliases]


@dataclass
class SkillPackage:
    root: Path
    manifest: dict
    skill_md: str
    refs: list[RefItem] = field(default_factory=list)
    genres: list[GenreModule] = field(default_factory=list)

    # ---------- 基本属性 ----------
    @property
    def id(self) -> str:
        return self.manifest["id"]

    @property
    def version(self) -> str:
        return self.manifest["version"]

    @property
    def output_contract(self) -> str | None:
        return self.manifest.get("output_contract")

    @property
    def input_contracts(self) -> list[str]:
        return self.manifest.get("input_contracts", [])

    @property
    def model_config(self) -> dict:
        return self.manifest.get("model", {})

    # ---------- 加载 ----------
    @classmethod
    def load(cls, root: str | Path) -> "SkillPackage":
        root = Path(root)
        if not root.is_dir():
            raise SkillError(f"skill 目录不存在: {root}")

        mf_path = root / "manifest.json"
        if not mf_path.exists():
            raise SkillError(f"缺少 manifest.json: {root}")
        try:
            manifest = json.loads(mf_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SkillError(f"manifest.json 解析失败: {e}")

        md_path = root / "SKILL.md"
        if not md_path.exists():
            raise SkillError(f"缺少 SKILL.md: {root}")
        skill_md = md_path.read_text(encoding="utf-8")

        pkg = cls(root=root, manifest=manifest, skill_md=skill_md)
        pkg.refs = pkg._load_refs()
        pkg.genres = pkg._load_genres()
        return pkg

    def _load_genres(self) -> list[GenreModule]:
        gdir = self.root / "genres"
        if not gdir.is_dir():
            return []
        index = {}
        ipath = gdir / "index.json"
        if ipath.exists():
            try:
                index = json.loads(ipath.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                index = {}
        mods = []
        for p in sorted(gdir.glob("*.md")):
            meta = index.get(p.name, {})
            mods.append(GenreModule(
                key=meta.get("key", p.stem),
                path=p,
                aliases=meta.get("aliases", []),
                title=meta.get("title", p.stem),
            ))
        return mods

    def select_genres(self, tags: list[str]) -> tuple[list[GenreModule], list[str]]:
        """按题材标签命中模块。返回 (命中的模块, 未覆盖的标签)。
        未覆盖的标签要显式告知用户 —— 让人知道这次是在无模块支撑下裸奔。"""
        hit, miss = [], []
        for t in tags or []:
            m = next((g for g in self.genres if g.matches(t)), None)
            if m and m not in hit:
                hit.append(m)
            elif not m:
                miss.append(t)
        return hit, miss

    def _load_refs(self) -> list[RefItem]:
        ref_dir = self.root / "references"
        if not ref_dir.is_dir():
            return []
        index_path = ref_dir / "index.json"
        index = {}
        if index_path.exists():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                index = {}

        items = []
        for p in sorted(ref_dir.glob("*.md")):
            meta = index.get(p.name, {})
            items.append(RefItem(
                ref_id=meta.get("ref_id", p.stem),
                path=p,
                grade=meta.get("grade", "A"),
                purpose=meta.get("purpose",
                                 self.manifest.get("references", {})
                                 .get("default_purpose", "align_voice")),
                tags=meta.get("tags", {}),
                title=meta.get("title", p.stem),
            ))
        return items

    # ---------- 校验 ----------
    def validate(self) -> list[str]:
        errs: list[str] = []
        m = self.manifest

        for key in ["id", "name", "type", "team", "version"]:
            if not m.get(key):
                errs.append(f"manifest.{key}: 缺失必填字段")

        if m.get("type") and m["type"] not in SKILL_TYPES:
            errs.append(f"manifest.type: 应为 {SKILL_TYPES} 之一，实际 {m['type']!r}")
        if m.get("team") and m["team"] not in TEAMS:
            errs.append(f"manifest.team: 应为 {TEAMS} 之一，实际 {m['team']!r}")

        if m.get("version") and not re.match(r"^\d+\.\d+\.\d+$", m["version"]):
            errs.append(f"manifest.version: 应为 x.y.z 格式，实际 {m['version']!r}")

        # 契约绑定检查：worker 型 skill 必须声明输出契约，且契约必须已注册
        if m.get("type") == "worker":
            oc = m.get("output_contract")
            if not oc:
                errs.append("manifest.output_contract: worker 型 skill 必须声明输出契约")
            elif oc not in CONTRACT_REGISTRY:
                errs.append(f"manifest.output_contract: 未知契约 {oc!r}")
            for ic in m.get("input_contracts", []):
                if ic not in CONTRACT_REGISTRY:
                    errs.append(f"manifest.input_contracts: 未知契约 {ic!r}")
            # required_inputs 必须是 input_contracts 的子集
            for rc in m.get("required_inputs", []):
                if rc not in m.get("input_contracts", []):
                    errs.append(
                        f"manifest.required_inputs: {rc!r} 不在 input_contracts 中")

        # 模型绑定
        mc = m.get("model", {})
        if not mc.get("provider"):
            errs.append("manifest.model.provider: 缺失（anthropic / deepseek）")
        if not mc.get("model"):
            errs.append("manifest.model.model: 缺失模型名")

        # SKILL.md 五章节强制检查
        headings = set(re.findall(r"^#{1,4}\s*(.+?)\s*$", self.skill_md, re.M))
        norm = {h.replace(" ", "").lower() for h in headings}
        for sec in REQUIRED_SECTIONS:
            if sec.replace(" ", "").lower() not in norm:
                errs.append(f"SKILL.md: 缺少强制章节「{sec}」")

        # 章节不能只有模板注释 —— 空壳包必须被拦住，否则规范形同虚设
        for sec in REQUIRED_SECTIONS:
            body = self._section(sec, strip_comments=True)
            if not body:
                errs.append(f"SKILL.md 章节「{sec}」: 尚未填写内容"
                            f"（模板注释不算内容）")

        # 职责边界章节必须写出禁区（含「不」字的否定表述）
        boundary = self._section("职责边界", strip_comments=True)
        if boundary and not re.search(r"不(写|管|做|负责|得|许|碰|涉)", boundary):
            errs.append("SKILL.md 职责边界: 未写出明确禁区，"
                        "必须写清楚「我不管什么」，否则 skill 会越权")

        # references 分级合法性
        for r in self.refs:
            if r.grade not in REF_GRADES:
                errs.append(f"references[{r.ref_id}].grade: 应为 {REF_GRADES} 之一")

        return errs

    @property
    def required_inputs(self) -> list[str]:
        """缺了就不许开工的上游契约。缺省视为全部 input_contracts 必需。

        为什么宁可严：缺上游的产物表面能跑通，实际是在裸奔。
        分镜没有角色板时会自己发明人物长相，而引用完整性校验恰好
        因为缺上下文被跳过 —— 最该生效的时候失效。
        """
        m = self.manifest
        if "required_inputs" in m:
            return m["required_inputs"]
        return list(m.get("input_contracts", []))

    def check_inputs(self, provided: list[str]) -> list[str]:
        """返回缺失的必需上游契约。"""
        return [c for c in self.required_inputs if c not in provided]

    def _section(self, name: str, strip_comments: bool = False) -> str:
        """抽取某个章节的正文。strip_comments=True 时剥离 HTML 注释与子标题，
        用于判断作者是否真的写了内容（模板注释不算内容）。"""
        pat = re.compile(
            rf"^#{{1,4}}\s*{re.escape(name)}\s*$(.*?)(?=^#{{1,4}}\s*(?!#)"
            rf"(?:{'|'.join(re.escape(s) for s in REQUIRED_SECTIONS)})\s*$|\Z)",
            re.M | re.S,
        )
        m = pat.search(self.skill_md)
        if not m:
            return ""
        body = m.group(1)
        if strip_comments:
            body = re.sub(r"<!--.*?-->", "", body, flags=re.S)   # 去注释
            body = re.sub(r"^#{1,6}\s*.*$", "", body, flags=re.M)  # 去子标题
            body = re.sub(r"^\s*[-*]\s*$", "", body, flags=re.M)   # 去空列表项
            body = re.sub(r"\*\*.*?\*\*", "", body)                # 去纯加粗小标题
        return body.strip()

    # ---------- 版本 ----------
    def snapshot(self, note: str = "") -> Path:
        """把当前 SKILL.md 存档为一个版本。在线编辑保存时调用。"""
        vdir = self.root / "versions"
        vdir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = vdir / f"v{self.version}-{ts}.md"
        shutil.copy2(self.root / "SKILL.md", dest)
        if note:
            (vdir / f"v{self.version}-{ts}.note.txt").write_text(
                note, encoding="utf-8")
        return dest

    def list_versions(self) -> list[Path]:
        vdir = self.root / "versions"
        return sorted(vdir.glob("*.md")) if vdir.is_dir() else []

    def record_feedback(self, artifact_id: str, diff_text: str) -> Path | None:
        """审核门里的人工修改 diff 归集到 feedback/，质量飞轮的进料口。

        空 diff 不落盘 —— 零字节文件会被统计成"有过一次修改"，
        既污染高频字段统计，也让人误以为改过而实际没改。
        """
        if not diff_text or not diff_text.strip():
            return None
        fdir = self.root / "feedback"
        fdir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = fdir / f"{ts}-{artifact_id}.diff.md"
        dest.write_text(diff_text, encoding="utf-8")
        return dest


def discover(skills_dir: str | Path) -> list[SkillPackage]:
    """扫描工种 skill 包。

    刻意排除 directors/ —— 导演是约束型文档，规范与工种 skill 完全不同
    （五章节 vs 擅长题材/叙事偏好/镜头语言/对白风格/禁忌清单）。
    用工种规范去校验导演包会报出一堆假错误，导演由
    director.discover_directors() 单独负责。
    """
    skills_dir = Path(skills_dir)
    found = []
    for mf in sorted(skills_dir.rglob("manifest.json")):
        if "directors" in mf.parts:
            continue
        try:
            pkg = SkillPackage.load(mf.parent)
        except SkillError:
            continue
        if pkg.manifest.get("type") == "director":
            continue
        found.append(pkg)
    return found


def scaffold(skills_dir: str | Path, skill_id: str, name: str,
             skill_type: str = "worker", team: str = "content",
             output_contract: str | None = None,
             input_contracts: list[str] | None = None) -> Path:
    """按规范生成一个空壳 skill 包。新建 skill 一律从这里出发。"""
    root = Path(skills_dir) / skill_id
    if root.exists():
        raise SkillError(f"skill 已存在: {root}")
    for sub in ["references", "versions", "feedback"]:
        (root / sub).mkdir(parents=True, exist_ok=True)

    manifest = {
        "id": skill_id,
        "name": name,
        "type": skill_type,
        "team": team,
        "version": "0.1.0",
        "input_contracts": input_contracts or [],
        "output_contract": output_contract,
        "genre_tags": [],
        "model": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "temperature": 1.0,
            "max_tokens": 8000,
        },
        "references": {
            "max_tokens": 20000,
            "default_purpose": "align_voice",
        },
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    (root / "SKILL.md").write_text(_SKILL_TEMPLATE.format(
        name=name, skill_id=skill_id,
        output_contract=output_contract or "（无）",
        input_contracts="、".join(input_contracts or []) or "（无，流水线起点）",
    ), encoding="utf-8")

    (root / "references" / "index.json").write_text(
        json.dumps({}, ensure_ascii=False, indent=2), encoding="utf-8")
    return root


_SKILL_TEMPLATE = """# {name}（{skill_id}）

## 角色定义

<!-- 我是谁，我在整个制作团队里的位置。一段话，不要写成岗位说明书。 -->

## 职责边界

**我负责**

<!-- 逐条列出 -->

**我不负责**

<!-- 必须写出明确禁区，例如：不写镜头、不管人物造型。
     校验器会检查本节是否出现否定表述，写不出禁区的 skill 一定会越权。 -->

## 方法论

<!-- 工作步骤、判断标准、质量红线。
     这里蒸馏的应该是「判断」和「纠偏」，不是教科书知识 ——
     模型已经知道三幕结构是什么，它不知道的是你认为什么样才算过关。 -->

### 质量红线

<!-- 触碰即打回的硬标准 -->

## 输入输出契约

- 输入：{input_contracts}
- 输出：{output_contract}

<!-- 输出必须严格符合契约字段规格，下游 skill 要机器化消费本产物。 -->

## references调用方式

<!-- 三种用法必须写明本 skill 默认采用哪种：
     - align_voice   对齐语感：只看语言节奏与分寸，不借情节
     - borrow_structure 借用结构：只借信息释放顺序，人物台词另起炉灶
     - avoid         规避雷区：反面教材，明确禁止此类处理

     硬护栏：禁止复用具体台词、人名、地名、独有设定，只能复用机制。
     产出时必须附引用申报（references_cited），说明调用了哪几条、各解决什么问题。 -->
"""
