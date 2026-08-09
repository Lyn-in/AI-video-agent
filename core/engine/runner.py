"""
生成编排：跑一个流水线节点。

原先 web/app.py 有一份（_do_run 闭包），cli/avctl.py 有一份（cmd_run 正文），
两边各自拼装、各自落盘，改一处忘另一处就会出现「命令行跑出来的东西
工作台看不见」这类不报错的断层。

这里只负责领域动作并抛 RunError，不打印、不 sys.exit ——
怎么把失败讲给人听是界面的事。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core import contracts
from core.engine.prompt import build_prompt, resolve_genre_tags
from core.gateway.client import GatewayError, ModelGateway
from core.pipeline import node_for, scope_of
from core.skillkit.director import DIRECTOR_BLIND_SKILLS, Director
from core.store.artifacts import input_hash, make_envelope


class RunError(Exception):
    """带人话原因的执行失败。"""


@dataclass
class RunResult:
    artifact: dict
    path: Path
    provider: str
    model: str
    dry_run: bool = False
    genre_hit: list = field(default_factory=list)
    genre_miss: list = field(default_factory=list)


def resolve_director(skills_dir: Path, director_id: str | None,
                     pkg, *, strict: bool = False) -> Director | None:
    """
    取导演包。作家这类 director-blind 的 skill 直接返回 None ——
    故事在前，导演在后，作家不该被风格带跑。
    """
    if not director_id or pkg.id in DIRECTOR_BLIND_SKILLS:
        return None
    root = Path(skills_dir) / "directors" / director_id
    if not root.is_dir():
        raise RunError(f"找不到导演 {director_id}")
    d = Director.load(root)
    errs = d.validate()
    if errs and strict:
        raise RunError(f"导演 {d.id} 未通过规范校验：{errs[0]}")
    return d


def collect_upstream(store, astore, pkg, series_id: str,
                     episode_no: int | None) -> tuple[list[dict], list[str]]:
    """
    按 skill 声明的 input_contracts 自动收集上游产物，
    省掉手工指定文件。取每个上游契约在当前作用域下的最新一版。
    """
    inputs, ids = [], []
    for need in pkg.input_contracts:
        up = node_for(need)
        if not up:
            continue
        row = store.latest_artifact(*scope_of(up, series_id, episode_no), need)
        if row:
            a = astore.load(row["path"])
            inputs.append(a)
            ids.append(a["artifact_id"])
    return inputs, ids


def ensure_project(store, *, account: str, collection: str, series_id: str,
                   episode_no: int | None, director_id: str | None,
                   genre_tags: list[str] | None) -> None:
    """
    补建项目结构。不这么做的话，命令行跑完工作台首页看不到这个剧集 ——
    这类断层不报错，只会让人以为工作台坏了。
    """
    store.create_account(account, account)
    store.create_collection(collection, account, collection)
    with store.conn() as c:
        exists = c.execute("SELECT 1 FROM series WHERE id=?",
                           (series_id,)).fetchone()
    if not exists:
        store.create_series(series_id, collection, series_id,
                            director_id=director_id,
                            genre_tags=genre_tags or [])
    elif director_id:
        with store.conn() as c:
            c.execute("UPDATE series SET director_id=? WHERE id=?",
                      (director_id, series_id))
    if episode_no:
        # INSERT OR IGNORE：已存在的集不要被覆盖掉标题和状态。
        with store.conn() as c:
            c.execute("INSERT OR IGNORE INTO episodes"
                      "(id,series_id,episode_no,title) VALUES(?,?,?,?)",
                      (f"{series_id}-ep{episode_no}", series_id,
                       episode_no, f"第{episode_no}集"))


def run_node(*, pkg, store, astore, series_id: str,
             episode_no: int | None = None,
             account: str = "default", collection: str = "s1",
             brief: str = "", genre_tags: list[str] | None = None,
             director=None, inputs: list[dict] | None = None,
             input_ids: list[str] | None = None,
             gateway: ModelGateway | None = None,
             log_path: Path | None = None,
             dry_run: bool = False, force: bool = False,
             model_override: dict | None = None) -> RunResult:
    """
    跑一个节点：拼提示词 → 调模型 → 校验契约 → 落盘 → 登记。

    契约不过就不落盘。这一条是硬的：宁可什么都没有，
    也不要一份看着像模像样、下游读进去才炸的产物。
    """
    errs = pkg.validate()
    if errs and not force:
        raise RunError(f"{pkg.id} 未通过规范校验，拒绝执行：\n"
                       + "\n".join(f"    - {e}" for e in errs))

    if inputs is None:
        inputs, input_ids = collect_upstream(store, astore, pkg,
                                             series_id, episode_no)
    input_ids = input_ids or [a["artifact_id"] for a in inputs]

    missing = pkg.check_inputs([a["contract"] for a in inputs])
    if missing:
        lines = [f"    - {m}（{contracts.get(m).cn_name}），"
                 f"由 {contracts.get(m).producer} 产出" for m in missing]
        raise RunError(
            "缺少必需的上游产物，拒绝执行：\n" + "\n".join(lines) +
            "\n  缺上游硬跑的产物表面能通过校验，实际是在裸奔 ——"
            "\n  例如分镜没有角色板会自己发明人物长相，而引用完整性校验"
            "\n  恰好因为缺上下文被跳过，最该生效的时候失效。")

    tags = resolve_genre_tags(genre_tags, inputs)
    p = build_prompt(pkg, brief, inputs, tags, director)

    gw = gateway or ModelGateway(log_path=log_path, dry_run=dry_run)
    mc = {**pkg.model_config, **(model_override or {})}
    try:
        res = gw.call(system=p.system, user=p.user,
                      model_config=mc,
                      skill_id=pkg.id, tag=pkg.output_contract)
        payload = res.json_payload()
    except GatewayError as e:
        raise RunError(str(e)) from e

    citations = payload.pop("_references_cited", [])
    art = make_envelope(
        pkg.output_contract, payload,
        skill_id=pkg.id, skill_version=pkg.version,
        model=f"{res.provider}/{res.model}",
        director=director.id if director else None,
        inputs=input_ids, in_hash=input_hash(inputs),
        citations=citations)

    verrs = contracts.validate_artifact(
        art, contracts.get(pkg.output_contract),
        {a["contract"]: a["payload"] for a in inputs})
    if verrs:
        raise RunError("产出不符合契约，未落盘：\n"
                       + contracts.ValidationError(verrs).report())

    node = node_for(pkg.output_contract)
    ensure_project(store, account=account, collection=collection,
                   series_id=series_id,
                   episode_no=episode_no if node and node.level != "series" else None,
                   director_id=director.id if director else None,
                   genre_tags=tags)

    ep = episode_no if node and node.level != "series" else None
    path = astore.save(art, account=account, collection=collection,
                       series=series_id, episode_no=ep)
    scope_type, scope_id = (scope_of(node, series_id, episode_no)
                            if node else ("series", series_id))
    store.register_artifact(art, astore.rel(path), scope_type, scope_id,
                            art["lineage"]["input_hash"])

    return RunResult(artifact=art, path=path, provider=res.provider,
                     model=res.model, dry_run=res.dry_run,
                     genre_hit=p.genre_hit, genre_miss=p.genre_miss)
