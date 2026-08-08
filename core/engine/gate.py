"""
审核门。流水线上每个节点产物都要过这一关。

三个动作：
  approve  通过
  revise   人工改过之后通过 —— 改动 diff 自动归集到对应 skill 的 feedback/
  reject   附修改意见打回，意见会拼进重生成的 prompt

第二个动作是质量飞轮的进料口：你在这里改的每一处，
都是在告诉平台「这个 skill 哪里还不够好」。
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path


def make_diff(before: dict, after: dict, artifact_id: str) -> str:
    """生成人工修改的 diff，落进 skill 的 feedback 目录。"""
    a = json.dumps(before.get("payload", {}), ensure_ascii=False,
                   indent=2, sort_keys=True).splitlines()
    b = json.dumps(after.get("payload", {}), ensure_ascii=False,
                   indent=2, sort_keys=True).splitlines()
    diff = list(difflib.unified_diff(a, b, "生成版", "人工修改版", lineterm=""))
    if not diff:
        return ""
    pb = after.get("produced_by", {})
    head = [
        f"# 人工修改记录：{artifact_id}",
        "",
        f"- skill: {pb.get('skill')} v{pb.get('skill_version')}",
        f"- 模型: {pb.get('model')}",
        f"- 导演: {pb.get('director') or '（无）'}",
        "",
        "## 改动",
        "",
        "```diff",
    ]
    return "\n".join(head + diff + ["```", ""])


def change_ratio(before: dict, after: dict) -> float:
    """改动比例。用于判断是否算「大改」，喂给 reference 健康度统计。"""
    a = json.dumps(before.get("payload", {}), ensure_ascii=False, sort_keys=True)
    b = json.dumps(after.get("payload", {}), ensure_ascii=False, sort_keys=True)
    if not a:
        return 1.0
    sm = difflib.SequenceMatcher(None, a, b)
    return round(1 - sm.ratio(), 4)


# 超过这个比例算大改，会记进 ref_health.revised_count
MAJOR_REVISION_THRESHOLD = 0.15


def build_retry_brief(original_brief: str, notes: str, rejected: dict) -> str:
    """打回重生成时，把修改意见和被否的产物一起拼进 prompt。"""
    parts = []
    if original_brief:
        parts.append(f"# 本次任务\n\n{original_brief}")
    parts.append(
        "\n# 上一版被打回\n\n"
        "以下是上一次的产出，人工审核后被否。请针对修改意见重写，"
        "不要在被否的版本上小修小补 —— 如果问题出在方向上，换方向。\n"
    )
    parts.append("## 被否的产出\n")
    parts.append("```json")
    parts.append(json.dumps(rejected.get("payload", {}),
                            ensure_ascii=False, indent=2))
    parts.append("```")
    parts.append(f"\n## 修改意见\n\n{notes}")
    return "\n".join(parts)
