"""
契约基础设施：一套极轻量的 schema DSL。

设计取舍：
- 不引入 pydantic / jsonschema，零外部依赖，单人维护心智负担最低。
- 校验错误必须带完整字段路径（payload.scenes[2].lines[0].character_id），
  因为 M0 的验收标准就是「校验不通过要能明确报出是哪个字段缺失」。
- 只强校验核心字段，允许 extra 字段自由扩展 —— 对应规划里
  「契约字段定得太早太死」这条风险的应对。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


class ValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(f"契约校验失败，共 {len(errors)} 处问题")

    def report(self) -> str:
        lines = [f"契约校验失败（{len(self.errors)} 处）:"]
        lines += [f"  [{i}] {e}" for i, e in enumerate(self.errors, 1)]
        return "\n".join(lines)


# ---------- 字段类型 ----------

@dataclass
class Field:
    name: str
    type: str = "str"          # str | int | float | bool | list | dict | any
    required: bool = True
    desc: str = ""
    # 嵌套：type=dict 时用 fields；type=list 时用 item_fields（列表元素是 dict）
    fields: list["Field"] = field(default_factory=list)
    item_fields: list["Field"] = field(default_factory=list)
    item_type: str = "dict"    # list 元素类型：dict | str | int
    min_items: int = 0
    max_items: int | None = None
    choices: list[Any] | None = None
    # 自定义校验：接收值，返回错误信息列表（空列表=通过）
    check: Callable[[Any], list[str]] | None = None


_PY_TYPES = {
    "str": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
    "list": list,
    "dict": dict,
}


def _validate_fields(data: Any, fields: list[Field], path: str) -> list[str]:
    errs: list[str] = []
    if not isinstance(data, dict):
        return [f"{path}: 期望 object，实际是 {type(data).__name__}"]

    for f in fields:
        p = f"{path}.{f.name}" if path else f.name
        if f.name not in data or data[f.name] is None:
            if f.required:
                errs.append(f"{p}: 缺失必填字段（{f.desc or f.type}）")
            continue

        val = data[f.name]

        # 类型检查
        if f.type != "any":
            py = _PY_TYPES.get(f.type)
            if py and not isinstance(val, py):
                errs.append(f"{p}: 类型应为 {f.type}，实际是 {type(val).__name__}")
                continue

        # 空值检查（必填的字符串不能是空白）
        if f.required and f.type == "str" and not val.strip():
            errs.append(f"{p}: 必填字段不能为空字符串")
            continue

        # 枚举
        if f.choices is not None and val not in f.choices:
            errs.append(f"{p}: 取值应为 {f.choices} 之一，实际是 {val!r}")

        # 嵌套 object
        if f.type == "dict" and f.fields:
            errs += _validate_fields(val, f.fields, p)

        # 列表
        if f.type == "list":
            if len(val) < f.min_items:
                errs.append(f"{p}: 至少需要 {f.min_items} 项，实际 {len(val)} 项")
            if f.max_items is not None and len(val) > f.max_items:
                errs.append(f"{p}: 最多 {f.max_items} 项，实际 {len(val)} 项")
            for i, item in enumerate(val):
                ip = f"{p}[{i}]"
                if f.item_type == "dict":
                    if f.item_fields:
                        errs += _validate_fields(item, f.item_fields, ip)
                    elif not isinstance(item, dict):
                        errs.append(f"{ip}: 期望 object")
                else:
                    py = _PY_TYPES.get(f.item_type)
                    if py and not isinstance(item, py):
                        errs.append(f"{ip}: 元素类型应为 {f.item_type}")

        # 自定义校验
        if f.check:
            errs += [f"{p}: {m}" for m in f.check(val)]

    return errs


# ---------- 契约 ----------

@dataclass
class Contract:
    """一份产物契约。payload_fields 是契约特有字段，信封字段所有契约共用。"""
    name: str
    cn_name: str
    version: str
    producer: str                 # 生产者 skill 类型
    consumers: list[str]          # 下游消费者
    payload_fields: list[Field]
    # 跨契约校验：接收 (payload, context)，context 里是上游产物
    cross_check: Callable[[dict, dict], list[str]] | None = None

    def validate_payload(self, payload: dict) -> list[str]:
        return _validate_fields(payload, self.payload_fields, "payload")

    def describe(self) -> str:
        lines = [
            f"契约 {self.name}（{self.cn_name}） v{self.version}",
            f"  生产者: {self.producer}",
            f"  消费者: {', '.join(self.consumers)}",
            "  字段:",
        ]

        def walk(fs: list[Field], indent: int):
            for f in fs:
                mark = "*" if f.required else " "
                pad = " " * indent
                t = f.type
                if f.type == "list":
                    t = f"list[{f.item_type}]"
                lines.append(f"{pad}{mark} {f.name} ({t}) {f.desc}")
                walk(f.fields or f.item_fields, indent + 4)

        walk(self.payload_fields, 4)
        lines.append("  （* = 必填；未列出的字段允许自由扩展）")
        return "\n".join(lines)


# ---------- 信封 ----------

CITATION_PURPOSES = ["align_voice", "borrow_structure", "avoid"]

ENVELOPE_FIELDS = [
    Field("contract", "str", True, "契约名"),
    Field("contract_version", "str", True, "契约版本"),
    Field("artifact_id", "str", True, "产物唯一ID"),
    Field("status", "str", True, "节点状态", choices=[
        "generating", "pending_review", "revised", "approved"]),
    Field("produced_by", "dict", True, "生产血缘", fields=[
        Field("skill", "str", True, "skill id"),
        Field("skill_version", "str", True, "skill 版本"),
        Field("director", "str", False, "导演上下文 id"),
        Field("model", "str", True, "使用的模型"),
    ]),
    Field("lineage", "dict", True, "输入血缘", fields=[
        Field("inputs", "list", False, "上游产物ID", item_type="str"),
        Field("input_hash", "str", False, "输入指纹，用于断点续跑"),
    ]),
    # 引用申报：架构讨论里定的第四道关卡，强制存在（可以是空列表，但字段必须在）
    Field("references_cited", "list", True, "引用申报清单", item_fields=[
        Field("ref_id", "str", True, "reference 条目ID"),
        Field("purpose", "str", True, "用途", choices=CITATION_PURPOSES),
        Field("problem_solved", "str", True, "这条解决了什么问题"),
    ]),
    Field("payload", "dict", True, "契约主体"),
]


def validate_artifact(artifact: dict, contract: Contract,
                      context: dict | None = None) -> list[str]:
    """校验完整产物（信封 + payload + 跨契约引用完整性）。"""
    errs = _validate_fields(artifact, ENVELOPE_FIELDS, "")

    declared = artifact.get("contract")
    if declared and declared != contract.name:
        errs.append(f"contract: 信封声明为 {declared!r}，但按 {contract.name!r} 校验")

    payload = artifact.get("payload")
    if isinstance(payload, dict):
        errs += contract.validate_payload(payload)
        if contract.cross_check and context:
            errs += contract.cross_check(payload, context)

    return errs
