"""
审核门的结构化视图。

原先审阅是 <pre> 一坨 markdown，修改是一个手写 JSON 的 textarea ——
打错一个逗号就 400，整页跳到错误页，编辑内容全丢。
而底层其实有很好的材料被浪费着：validate_artifact 返回的是
payload.scenes[2].beats[0].text 这种精确路径，Contract.payload_fields
有完整字段规格，前端一个都没用上。

这里做三件事：
  1. 按契约字段把 payload 切成块，每块单独编辑 —— scenes 里打错字
     不该威胁到 title，报错也能落到具体那一块上。
  2. 把校验错误按字段路径归到对应的块。
  3. 抽出所有 {zh, en} 提示词对，做中英对照扫读 ——
     中英混排残句这类低级错误，校验器管不了（它只查结构），
     但人扫一眼就能发现。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# 长度超过这个就用多行框。短字符串用单行输入舒服得多。
_INLINE_MAX = 60


@dataclass
class Block:
    name: str
    label: str
    kind: str                 # line | text | json
    required: bool
    raw: str                  # 编辑框里的字符串
    errors: list[str] = field(default_factory=list)

    @property
    def rows(self) -> int:
        """按内容定高。固定 rows 会让三行的数组撑出一屏空白。"""
        n = self.raw.count("\n") + 2
        return max(3, min(n, 24))


def _kind_and_raw(f, value: Any) -> tuple[str, str]:
    if f.type in ("list", "dict"):
        return "json", json.dumps(value, ensure_ascii=False, indent=2)
    if value is None:
        return "line", ""
    s = str(value)
    if f.type == "str" and (len(s) > _INLINE_MAX or "\n" in s):
        return "text", s
    return "line", s


def build_blocks(contract, payload: dict,
                 submitted: dict[str, str] | None = None) -> list[Block]:
    """
    按契约字段切块。submitted 非空时用提交的原文回填 ——
    校验失败要把用户刚敲的东西原样留在框里，而不是回退到旧值。
    """
    blocks = []
    for f in contract.payload_fields:
        if submitted is not None and f.name in submitted:
            kind, _ = _kind_and_raw(f, payload.get(f.name))
            raw = submitted[f.name]
        else:
            kind, raw = _kind_and_raw(f, payload.get(f.name))
        blocks.append(Block(name=f.name, label=f.desc or f.name, kind=kind,
                            required=f.required, raw=raw))

    # 契约允许 extra 字段自由扩展，别把它们弄丢了。
    known = {f.name for f in contract.payload_fields}
    for k, v in payload.items():
        if k in known:
            continue
        raw = (json.dumps(v, ensure_ascii=False, indent=2)
               if isinstance(v, (list, dict)) else str(v))
        if submitted is not None and k in submitted:
            raw = submitted[k]
        blocks.append(Block(name=k, label=k + "（契约外字段）",
                            kind="json" if isinstance(v, (list, dict)) else "text",
                            required=False, raw=raw))
    return blocks


def parse_blocks(contract, blocks: list[Block],
                 submitted: dict[str, str]) -> tuple[dict, bool]:
    """
    从提交的表单重建 payload。JSON 解析失败的块就地标错，
    其余块照常解析 —— 一处写坏不该把整次编辑作废。
    返回 (payload, 是否有解析错误)。
    """
    spec = {f.name: f for f in contract.payload_fields}
    payload, bad = {}, False
    for b in blocks:
        raw = submitted.get(b.name)
        if raw is None:
            continue
        f = spec.get(b.name)
        if b.kind == "json":
            try:
                payload[b.name] = json.loads(raw) if raw.strip() else None
            except json.JSONDecodeError as e:
                b.errors.append(f"JSON 格式有误：{e}")
                bad = True
            continue
        raw = raw.strip()
        if f is not None and f.type in ("int", "float"):
            if raw == "":
                payload[b.name] = None
                continue
            try:
                payload[b.name] = int(raw) if f.type == "int" else float(raw)
            except ValueError:
                b.errors.append(f"应该是{'整数' if f.type == 'int' else '数字'}，"
                                f"填的是 {raw!r}")
                bad = True
            continue
        payload[b.name] = raw
    # 解析失败的字段不放进 payload，避免用半个 payload 去跑契约校验
    return {k: v for k, v in payload.items() if v is not None}, bad


_TOP = re.compile(r"^payload\.([A-Za-z_][A-Za-z0-9_]*)")


def attach_errors(blocks: list[Block], errors: list[str]) -> list[str]:
    """
    把校验错误按字段路径归到对应的块，返回归不到任何块的那些
    （信封层、跨契约引用完整性之类）。
    """
    by_name = {b.name: b for b in blocks}
    rest = []
    for e in errors:
        m = _TOP.match(e)
        if m and m.group(1) in by_name:
            by_name[m.group(1)].errors.append(e)
        else:
            rest.append(e)
    return rest


def prompt_pairs(payload: Any, path: str = "") -> list[dict]:
    """
    递归找出所有 {zh, en} 提示词对，供中英对照扫读。

    这些是最终要喂给出图/出片模型的东西，也是最容易出低级错误的地方
    （中英混排残句、译文漏了半句、两边说的不是一回事）。
    契约只校验结构，管不了这类问题 —— 只能靠人扫一眼。
    """
    out = []
    if isinstance(payload, dict):
        if isinstance(payload.get("zh"), str) and isinstance(payload.get("en"), str):
            out.append({"path": path or "(顶层)",
                        "zh": payload["zh"], "en": payload["en"]})
        else:
            for k, v in payload.items():
                out += prompt_pairs(v, f"{path}.{k}" if path else k)
    elif isinstance(payload, list):
        for i, v in enumerate(payload):
            out += prompt_pairs(v, f"{path}[{i}]")
    return out
