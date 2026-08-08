"""
产物文件系统层。

目录约定 = 内容组织结构（架构文档 5.1）：
  projects/<account>/<collection>/<series>/
      _series/                     剧集级产物：角色板、场景板（全剧共享，锁死一致性）
      ep01/                        集级产物：故事档案、剧本、分镜表
      assets/                      回传的设计图与视频片段

每个产物落两份：
  xxx.json  机器消费版（下游 skill 读它）
  xxx.md    人工审阅版（审核门里你读它、改它）
JSON 是真相源；Markdown 供阅读，改完可用 md->json 回写（M4 界面接管后自动化）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path

# 契约 → 归属层级。角色板/场景板挂剧集，其余挂集。
SERIES_LEVEL = {"character_board", "location_board"}


def new_artifact_id(contract: str) -> str:
    return f"{contract}-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"


def input_hash(inputs: list[dict]) -> str:
    """输入指纹：上游没变就不必重跑（断点续跑）。"""
    h = hashlib.sha256()
    for art in inputs:
        h.update(json.dumps(art.get("payload", {}), ensure_ascii=False,
                            sort_keys=True).encode("utf-8"))
    return h.hexdigest()[:16]


def make_envelope(contract: str, payload: dict, *, skill_id: str,
                  skill_version: str, model: str, director: str | None = None,
                  inputs: list[str] | None = None, in_hash: str = "",
                  citations: list[dict] | None = None,
                  status: str = "pending_review") -> dict:
    from ..contracts import get as get_contract
    return {
        "contract": contract,
        "contract_version": get_contract(contract).version,
        "artifact_id": new_artifact_id(contract),
        "status": status,
        "produced_by": {
            "skill": skill_id,
            "skill_version": skill_version,
            "director": director,
            "model": model,
        },
        "lineage": {"inputs": inputs or [], "input_hash": in_hash},
        "references_cited": citations or [],
        "payload": payload,
    }


class ArtifactStore:
    def __init__(self, projects_dir: str | Path):
        self.root = Path(projects_dir)

    def dir_for(self, account: str, collection: str, series: str,
                contract: str, episode_no: int | None = None) -> Path:
        base = self.root / account / collection / series
        if contract in SERIES_LEVEL:
            return base / "_series"
        if episode_no is None:
            return base / "_series"
        return base / f"ep{episode_no:02d}"

    def save(self, artifact: dict, *, account: str, collection: str,
             series: str, episode_no: int | None = None) -> Path:
        contract = artifact["contract"]
        d = self.dir_for(account, collection, series, contract, episode_no)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{contract}.json"
        self.atomic_write(p, json.dumps(artifact, ensure_ascii=False, indent=2))
        self.atomic_write(d / f"{contract}.md", to_markdown(artifact))
        return p

    def resolve(self, path: str | Path) -> Path:
        """统一的路径解析：绝对路径 / 相对 cwd / 相对 projects/ 都能吃。

        安全边界：解析结果必须落在 projects/ 之内。
        路径可能来自数据库、命令行参数、未来的 HTTP 参数 ——
        任何一处被污染，都不该让它读到 /etc/passwd 或 config/secrets.env。
        """
        p = Path(path)
        root = self.root.resolve()
        cands = [p] if p.is_absolute() else [p, self.root / p]
        for cand in cands:
            if not cand.exists():
                continue
            real = cand.resolve()
            if real != root and root not in real.parents:
                raise PermissionError(
                    f"拒绝访问 projects/ 之外的路径: {path}")
            return cand
        raise FileNotFoundError(
            f"找不到产物文件: {path}"
            + ("" if p.is_absolute() else f"（已尝试 {p} 与 {self.root / p}）"))

    @staticmethod
    def atomic_write(path: Path, text: str):
        """原子写入：先写临时文件再 rename。

        直接 write_text 在写到一半崩溃时会留下截断的 JSON，
        而产物是这个平台的核心资产，损坏一份就得重跑整个节点。
        """
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def load(self, path: str | Path) -> dict:
        return json.loads(self.resolve(path).read_text(encoding="utf-8"))

    def rel(self, path: Path) -> str:
        try:
            return str(Path(path).relative_to(self.root))
        except ValueError:
            return str(path)


# ---------- 人工审阅版渲染 ----------

def to_markdown(art: dict) -> str:
    from ..contracts import get as get_contract
    c = get_contract(art["contract"])
    pb = art.get("produced_by", {})
    lines = [
        f"# {c.cn_name}",
        "",
        f"- 产物ID: `{art['artifact_id']}`",
        f"- 状态: **{art['status']}**",
        f"- 生产: {pb.get('skill')} v{pb.get('skill_version')}"
        f" / 模型 {pb.get('model')}"
        + (f" / 导演 {pb.get('director')}" if pb.get("director") else ""),
    ]
    cits = art.get("references_cited", [])
    if cits:
        lines += ["", "## 引用申报", ""]
        for x in cits:
            lines.append(f"- `{x['ref_id']}`（{x['purpose']}）"
                         f"— {x['problem_solved']}")
    else:
        lines += ["", "## 引用申报", "", "- 本次未引用任何 reference"]

    lines += ["", "---", "", _render(art.get("payload", {}), 2)]
    return "\n".join(lines)


def _render(obj, level: int) -> str:
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                out.append(f"\n{'#' * min(level, 6)} {k}\n")
                out.append(_render(v, level + 1))
            else:
                out.append(f"- **{k}**: {v}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, (dict, list)):
                out.append(f"\n{'#' * min(level, 6)} [{i}]\n")
                out.append(_render(v, level + 1))
            else:
                out.append(f"- {v}")
    else:
        out.append(str(obj))
    return "\n".join(out)
