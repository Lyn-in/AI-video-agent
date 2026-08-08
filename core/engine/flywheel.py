"""
质量飞轮闭环。

    生成产物 → 审核门人工修改（diff 自动入 feedback）→ 分析修改模式
       ↑                                                    ↓
    skill 新版本 ← 人工批准合入 ← 生成 skill 修改建议

关键设计：**建议由模型生成，合入必须人工批准。**
全自动改 skill 是不可接受的 —— skill 是这个平台唯一的资产，
一次错误的自动合入会污染之后所有产出，而且很难回滚到"哪一版开始变坏的"。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


# ============================================================
# 一、feedback 解析与模式分析
# ============================================================

@dataclass
class FeedbackRecord:
    path: Path
    artifact_id: str
    skill: str
    skill_version: str
    director: str | None
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)

    @property
    def ts(self) -> str:
        m = re.match(r"(\d{8}-\d{6})", self.path.name)
        return m.group(1) if m else ""


def parse_feedback(path: Path) -> FeedbackRecord | None:
    """解析一份人工修改 diff。空文件或缺头部的直接跳过。"""
    text = path.read_text(encoding="utf-8")
    if not text.strip() or "# 人工修改记录" not in text:
        return None

    def grab(label):
        m = re.search(rf"- {label}: (.+)", text)
        return m.group(1).strip() if m else ""

    aid = ""
    m = re.search(r"# 人工修改记录：(.+)", text)
    if m:
        aid = m.group(1).strip()

    skill_raw = grab("skill")
    sm = re.match(r"(\S+)\s+v(\S+)", skill_raw)
    skill, ver = (sm.group(1), sm.group(2)) if sm else (skill_raw, "")
    director = grab("导演")
    if director in ("（无）", ""):
        director = None

    rec = FeedbackRecord(path=path, artifact_id=aid, skill=skill,
                         skill_version=ver, director=director)

    for line in text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            rec.added.append(line[1:].strip())
        elif line.startswith("-"):
            rec.removed.append(line[1:].strip())

    # 从改动行里抽字段名："field": value
    for line in rec.added + rec.removed:
        fm = re.match(r'"([^"]+)":', line)
        if fm:
            rec.fields.append(fm.group(1))
    return rec


def load_feedback(skill_root: Path) -> list[FeedbackRecord]:
    fdir = skill_root / "feedback"
    if not fdir.is_dir():
        return []
    out = []
    for f in sorted(fdir.glob("*.diff.md")):
        try:
            r = parse_feedback(f)
            if r:
                out.append(r)
        except Exception:                                    # noqa: BLE001
            continue
    return out


def analyze(records: list[FeedbackRecord]) -> dict:
    """
    修改模式统计。这是给人看的，也是拼进建议 prompt 的证据。

    注意：统计只能告诉你「哪个字段总被改」，告诉不了「为什么」。
    「为什么」需要模型读 diff 原文来判断，所以建议生成时要带原文。
    """
    if not records:
        return {"count": 0}
    fields = Counter()
    for r in records:
        fields.update(set(r.fields))       # 同一份 diff 内同字段只计一次
    versions = Counter(r.skill_version for r in records)
    directors = Counter(r.director or "（无导演）" for r in records)
    return {
        "count": len(records),
        "hot_fields": fields.most_common(10),
        "by_version": versions.most_common(),
        "by_director": directors.most_common(),
        "span": (records[0].ts, records[-1].ts),
    }


MIN_SAMPLES = 5   # 少于这个数量不建议生成改进 —— 样本太少会把偶然当规律


# ============================================================
# 二、建议生成
# ============================================================

SUGGEST_SYSTEM = """你是这个 AI 视频生产平台的 skill 维护者。

你的任务：读一批「人工修改记录」，找出模型产出与人工期望之间的**系统性差距**，
然后提出对 SKILL.md 的具体修改建议。

铁律：

1. **只提系统性问题。** 一次性的、偶然的修改不算。同一类问题至少出现两次才提。
2. **建议必须可执行。** 说清楚改哪一节、加哪一条、删哪一条、原文改成什么。
   不要提"要提高质量"这类无法执行的建议。
3. **优先加禁令和判断标准，而不是加知识。** 模型缺的很少是知识，
   缺的是"什么算过关"和"默认会往哪里错"。
4. **不要把 SKILL.md 写长。** 每条建议都要说明它替换或压缩了什么。
   如果只能靠加字解决，说明这条建议还不够精准。
5. **拿不准就说拿不准。** 证据不足时明确写出来，不要硬凑建议。

输出严格的 JSON，不要任何解释或围栏：

{
  "verdict": "有系统性问题" | "证据不足" | "暂无需要修改",
  "evidence_summary": "一段话：这批修改暴露了什么",
  "suggestions": [
    {
      "section": "要改的章节名",
      "kind": "add" | "replace" | "delete",
      "current": "现有原文（replace/delete 时必填，add 填空字符串）",
      "proposed": "建议的新文本",
      "why": "依据哪几次修改、解决什么问题",
      "confidence": "high" | "medium" | "low"
    }
  ],
  "not_suggested": ["看到但决定不提的问题，以及为什么不提"]
}"""


def build_suggest_prompt(skill_md: str, records: list[FeedbackRecord],
                         stats: dict, max_records: int = 20) -> str:
    parts = [f"# 当前 SKILL.md\n\n{skill_md}\n\n---\n\n# 修改统计\n"]
    parts.append(f"- 样本数：{stats['count']}")
    parts.append(f"- 高频被改字段：{stats.get('hot_fields')}")
    parts.append(f"- 按 skill 版本分布：{stats.get('by_version')}")
    parts.append(f"- 按导演分布：{stats.get('by_director')}")
    parts.append("\n注意：按导演分布很重要。"
                 "如果修改集中在某一个导演下，问题可能出在导演约束"
                 "或注入方式上，不该改 skill 本体。\n")
    parts.append("\n---\n\n# 人工修改记录原文\n")
    for r in records[-max_records:]:
        parts.append(f"\n## {r.path.name}（{r.skill} v{r.skill_version}"
                     f"，导演 {r.director or '无'}）\n")
        parts.append("```diff")
        for line in r.removed[:12]:
            parts.append(f"-{line}")
        for line in r.added[:12]:
            parts.append(f"+{line}")
        parts.append("```")
    return "\n".join(parts)


# ============================================================
# 三、建议存档与审批合入
# ============================================================

@dataclass
class Proposal:
    skill: str
    created_at: str
    based_on: int
    data: dict
    path: Path

    @property
    def suggestions(self) -> list[dict]:
        return self.data.get("suggestions", [])

    @property
    def status(self) -> str:
        return self.data.get("_status", "pending")


def save_proposal(skill_root: Path, skill_id: str, data: dict,
                  based_on: int) -> Proposal:
    pdir = skill_root / "proposals"
    pdir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    data = dict(data)
    data["_status"] = "pending"
    data["_based_on"] = based_on
    data["_created_at"] = ts
    p = pdir / f"{ts}.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return Proposal(skill=skill_id, created_at=ts, based_on=based_on,
                    data=data, path=p)


class SuggestError(Exception):
    """带人话原因的建议生成失败。界面负责怎么讲给人听。"""


def generate_proposal(pkg, *, gateway=None, log_path: Path | None = None,
                      dry_run: bool = False, force: bool = False):
    """
    读反馈 → 让模型给出 SKILL.md 的改进建议 → 存成待批提案。

    只生成，不合入。skill 是这个平台唯一的资产，一次错误的自动合入会污染
    之后所有产出，且很难回溯到「哪一版开始变坏」—— 所以合入必须人点头。

    原先只有命令行能跑这一步，界面却能审批它自己生成不了的建议，
    闭环是断的。
    """
    from core.gateway.client import GatewayError, ModelGateway

    recs = load_feedback(pkg.root)
    st = analyze(recs)
    if not st["count"]:
        raise SuggestError(
            f"{pkg.id} 还没有人工修改反馈。反馈来自审核门里的「保存修改」——"
            f"你改的每一处都会归集。")
    if st["count"] < MIN_SAMPLES and not force:
        raise SuggestError(
            f"反馈仅 {st['count']} 条，少于 {MIN_SAMPLES} 条门槛。"
            f"样本太少会把偶然当规律，生成的建议不可信。")

    gw = gateway or ModelGateway(log_path=log_path, dry_run=dry_run)
    try:
        res = gw.call(system=SUGGEST_SYSTEM,
                      user=build_suggest_prompt(pkg.skill_md, recs, st),
                      model_config=pkg.model_config,
                      skill_id=pkg.id, tag="proposal")
        data = res.json_payload()
    except GatewayError as e:
        raise SuggestError(str(e)) from e
    return save_proposal(pkg.root, pkg.id, data, st["count"]), data


def list_proposals(skill_root: Path, skill_id: str) -> list[Proposal]:
    pdir = skill_root / "proposals"
    if not pdir.is_dir():
        return []
    out = []
    for f in sorted(pdir.glob("*.json"), reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        out.append(Proposal(skill=skill_id, created_at=d.get("_created_at", ""),
                            based_on=d.get("_based_on", 0), data=d, path=f))
    return out


class ApplyError(Exception):
    pass


def apply_suggestion(skill_md: str, sug: dict) -> str:
    """
    把一条建议应用到 SKILL.md 文本上。

    replace/delete 必须精确匹配 current，匹配不到就报错 ——
    宁可让人手工处理，也不做模糊匹配。模糊匹配改错地方，
    人在 diff 里未必看得出来。
    """
    kind = sug.get("kind")
    cur = sug.get("current", "")
    new = sug.get("proposed", "")
    sec = sug.get("section", "")

    if kind == "replace":
        if not cur or cur not in skill_md:
            raise ApplyError(f"找不到要替换的原文（章节「{sec}」）。"
                             f"SKILL.md 可能已被改动，请手工处理。")
        if skill_md.count(cur) > 1:
            raise ApplyError(f"要替换的原文在文件中出现 "
                             f"{skill_md.count(cur)} 次，不唯一，拒绝自动替换。")
        return skill_md.replace(cur, new)

    if kind == "delete":
        if not cur or cur not in skill_md:
            raise ApplyError(f"找不到要删除的原文（章节「{sec}」）。")
        return skill_md.replace(cur, "")

    if kind == "add":
        pat = re.compile(rf"(^#{{1,4}}\s*{re.escape(sec)}\s*$)", re.M)
        m = pat.search(skill_md)
        if not m:
            raise ApplyError(f"找不到章节「{sec}」，无法插入。")
        # 插到该章节末尾（下一个同级标题之前）
        start = m.end()
        nxt = re.search(r"^#{1,4}\s", skill_md[start:], re.M)
        pos = start + (nxt.start() if nxt else len(skill_md) - start)
        return skill_md[:pos].rstrip() + "\n\n" + new + "\n\n" + skill_md[pos:]

    raise ApplyError(f"未知的建议类型 {kind!r}")


# ============================================================
# 四、reference 健康度治理
# ============================================================

RETIRE_MIN_CITED = 5      # 引用够这么多次才有资格判定"总被大改"
RETIRE_REVISE_RATIO = 0.6  # 引用后大改率超过这个值，建议降级
STALE_CITED = 0            # 从未被引用

def ref_governance(health_rows: list[dict]) -> dict:
    """
    references 治理建议。

    两类要清理：
    - 从未被引用：标签打错了，或者根本没用。留着只会拖慢检索精度。
    - 引用后总被大改：这条在误导模型，比没有更糟。
    """
    stale, misleading, healthy = [], [], []
    for r in health_rows:
        cited = r.get("cited_count", 0)
        revised = r.get("revised_count", 0)
        if cited <= STALE_CITED:
            stale.append(r)
        elif cited >= RETIRE_MIN_CITED and revised / cited >= RETIRE_REVISE_RATIO:
            misleading.append({**r, "ratio": round(revised / cited, 2)})
        else:
            healthy.append(r)
    return {"stale": stale, "misleading": misleading, "healthy": healthy}


# ============================================================
# 五、成片数据回流
# ============================================================

def record_performance(store, artifact_id: str, metrics: dict):
    """
    发布后的数据标注回产物。

    用途：识别哪些提示词模式产出了爆款。这条通路的价值要等样本
    攒够才显现，但采集必须从第一条片子就开始 —— 补采是补不回来的。
    """
    with store.conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS performance (
            artifact_id TEXT PRIMARY KEY,
            platform TEXT, published_at TEXT,
            views INTEGER, completion_rate REAL,
            likes INTEGER, comments INTEGER, shares INTEGER,
            note TEXT,
            updated_at TEXT DEFAULT (datetime('now','localtime')))""")
        c.execute("""INSERT OR REPLACE INTO performance
            (artifact_id, platform, published_at, views, completion_rate,
             likes, comments, shares, note, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
                  (artifact_id, metrics.get("platform"),
                   metrics.get("published_at"), metrics.get("views"),
                   metrics.get("completion_rate"), metrics.get("likes"),
                   metrics.get("comments"), metrics.get("shares"),
                   metrics.get("note")))
