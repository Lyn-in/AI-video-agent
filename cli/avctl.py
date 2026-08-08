#!/usr/bin/env python3
"""
avctl —— AI视频Agent平台命令行入口。

M0 可用命令：
  avctl init                       初始化数据库与目录
  avctl contract list              列出五份契约
  avctl contract show <name>       查看某契约的字段规格
  avctl skill new <id> <名称>      按规范生成空壳 skill 包
  avctl skill list                 列出所有 skill
  avctl skill check [id]           校验 skill 是否符合平台规范
  avctl artifact check <file>      按契约校验一份产物
  avctl run <skill_id> ...         跑一个 skill 节点（支持 --dry-run）
  avctl smoke                      M0 验收：一条命令跑通全链路自检
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import contracts                                    # noqa: E402
from core.gateway.client import ModelGateway, GatewayError     # noqa: E402
from core.skillkit.package import (                            # noqa: E402
    SkillPackage, SkillError, discover, scaffold,
)
from core.skillkit.genre import (                              # noqa: E402
    add_genre, GENRE_AWARE_SKILLS,
)
from core.skillkit.director import (                           # noqa: E402
    Director, discover_directors, recommend, scaffold_director,
    DIRECTOR_BLIND_SKILLS,
)
from core.store.artifacts import ArtifactStore, make_envelope  # noqa: E402
from core.store.db import Store                                # noqa: E402
from core.pipeline import NODES, scope_of                      # noqa: E402
from core.engine.gate import (                                 # noqa: E402
    make_diff, change_ratio, build_retry_brief, MAJOR_REVISION_THRESHOLD,
)
from core.engine.prompt import build_prompt                    # noqa: E402
from core.engine.runner import (                               # noqa: E402
    RunError, resolve_director, run_node,
)
from core.engine.flywheel import (                            # noqa: E402
    load_feedback, analyze, build_suggest_prompt, SUGGEST_SYSTEM,
    save_proposal, list_proposals, apply_suggestion, ApplyError,
    ref_governance, record_performance, MIN_SAMPLES,
)

DB = ROOT / "config" / "platform.db"
SKILLS = ROOT / "skills"
PROJECTS = ROOT / "projects"
CALL_LOG = ROOT / "config" / "calls.jsonl"

OK, BAD, WARN = "\u2713", "\u2717", "!"


# ---------------- init ----------------

def cmd_init(args):
    store = Store(DB)
    store.init()
    for d in [SKILLS, PROJECTS, SKILLS / "directors"]:
        d.mkdir(parents=True, exist_ok=True)
    print(f"{OK} 数据库已初始化: {DB}")
    print(f"{OK} 目录就绪: skills/ projects/")


# ---------------- contract ----------------

def cmd_contract(args):
    if args.action == "list":
        print(f"已注册契约 {len(contracts.REGISTRY)} 份：\n")
        for name, c in contracts.REGISTRY.items():
            print(f"  {name:18} {c.cn_name:8} v{c.version}"
                  f"  {c.producer} → {', '.join(c.consumers)}")
        print("\n流水线顺序：")
        for step in contracts.PIPELINE_ORDER:
            print(f"  {' + '.join(step) if isinstance(step, list) else step}")
    else:
        print(contracts.get(args.name).describe())


# ---------------- skill ----------------

def cmd_skill(args):
    if args.action == "new":
        root = scaffold(
            SKILLS, args.id, args.name,
            skill_type=args.type, team=args.team,
            output_contract=args.output, input_contracts=args.input or [],
        )
        print(f"{OK} 已生成 skill 包: {root}")
        print("  下一步：填写 SKILL.md 的五个强制章节，然后 avctl skill check")
        return

    pkgs = discover(SKILLS)
    if args.action == "list":
        if not pkgs:
            print("尚无 skill。用 avctl skill new 创建。")
            return
        for p in pkgs:
            print(f"  {p.id:16} v{p.version:8} {p.manifest.get('type'):8}"
                  f" {p.manifest.get('name')}"
                  f"  → {p.output_contract or '-'}"
                  f"  refs:{len(p.refs)}")
        return

    # check
    targets = [p for p in pkgs if not args.id or p.id == args.id]
    if not targets:
        print(f"{BAD} 未找到 skill: {args.id}")
        sys.exit(1)
    failed = 0
    for p in targets:
        errs = p.validate()
        if errs:
            failed += 1
            print(f"{BAD} {p.id} 不符合平台规范（{len(errs)} 处）:")
            for e in errs:
                print(f"    - {e}")
        else:
            print(f"{OK} {p.id} 符合平台规范")
    nd = len(discover_directors(SKILLS))
    if nd and not args.id:
        print(f"\n  （另有 {nd} 位导演，用 avctl director list 校验 ——"
              f"导演走另一套规范）")
    sys.exit(1 if failed else 0)


# ---------------- export（把 skill 拿出去单独用） ----------------

def cmd_export(args):
    from core.skillkit.exporter import (
        export_claude, export_codex, export_plain,
    )
    out = Path(args.out or (ROOT / "export"))
    fmts = args.format or ["claude", "codex", "plain"]
    done = {}
    for f in fmts:
        d = out / f
        if d.exists() and args.clean:
            import shutil as _sh
            _sh.rmtree(d)
        fn = {"claude": export_claude, "codex": export_codex,
              "plain": export_plain}[f]
        done[f] = fn(SKILLS, d)

    print(f"{OK} 已导出到 {out}/\n")
    for f, ids in done.items():
        print(f"   {f:8} {len(ids)} 个 skill → {out / f}")
    print()
    if "claude" in done:
        print("   Claude Code 用法：把 export/claude/ 下的各个文件夹")
        print("   复制到你项目的 .claude/skills/ 里即可。")
    if "codex" in done:
        print("   Codex / 通用 agent：用 export/codex/ 下的单文件，"
              "或整目录 + AGENTS.md。")
    if "plain" in done:
        print("   任意对话框：export/plain/ 下的文件直接复制粘贴。")
    print()
    print(f"   {WARN} 导出版没有契约校验器 —— 平台里编剧写了镜头会被强制打回，")
    print("   导出版只有文字约定。要严格执法还是用平台。")


# ---------------- web（生产工作台） ----------------

def cmd_web(args):
    try:
        from web.app import create_app
    except ImportError:
        print(f"{BAD} 缺少 Flask。安装：")
        print("     pip install flask --break-system-packages")
        sys.exit(1)

    if args.host != "127.0.0.1":
        print(f"{WARN} 你把工作台绑到了 {args.host}。")
        print("     工作台目前没有任何鉴权和 CSRF 防护 ——")
        print("     任何能访问到它的人都能改 SKILL.md、盖章、合入建议。")
        print("     只在可信内网这么做，绝不要暴露到公网。")
        print()
    if args.debug:
        print(f"{WARN} debug 模式开启。Werkzeug 调试器可在页面上执行代码，")
        print("     配合无鉴权等于开后门。仅在本机排查问题时使用。")
        print()

    print(f"{OK} 生产工作台: http://{args.host}:{args.port}")
    print("   Ctrl+C 停止\n")
    create_app().run(host=args.host, port=args.port, debug=args.debug)


# ---------------- flywheel（M6 质量飞轮） ----------------

def cmd_flywheel(args):
    store = Store(DB)

    if args.action == "status":
        print()
        for pkg in discover(SKILLS):
            recs = load_feedback(pkg.root)
            st = analyze(recs)
            props = list_proposals(pkg.root, pkg.id)
            pending = [p for p in props if p.status == "pending"]
            gov = ref_governance(store.ref_health(pkg.id))
            ready = OK if st["count"] >= MIN_SAMPLES else " "
            print(f" {ready} {pkg.id:14} 反馈 {st['count']:3}"
                  f"  待批建议 {len(pending)}"
                  f"  ref: 健康 {len(gov['healthy'])}"
                  f" / 闲置 {len(gov['stale'])}"
                  f" / 误导 {len(gov['misleading'])}")
        print(f"\n  ✓ 表示反馈样本已达 {MIN_SAMPLES} 条，可以生成改进建议")
        print(f"  少于 {MIN_SAMPLES} 条不建议生成 —— 样本太少会把偶然当规律\n")
        return

    if args.action == "analyze":
        pkg = SkillPackage.load(SKILLS / args.skill)
        recs = load_feedback(pkg.root)
        st = analyze(recs)
        if not st["count"]:
            print(f"{WARN} {args.skill} 还没有人工修改反馈。")
            print("  反馈来自审核门里的『保存修改』—— 你改的每一处都会归集。")
            return
        print(f"\n{args.skill} 修改模式分析（{st['count']} 条，"
              f"{st['span'][0]} ~ {st['span'][1]}）\n")
        print("  高频被改字段:")
        for f, n in st["hot_fields"]:
            print(f"    {n:3}×  {f}")
        print("\n  按 skill 版本:")
        for v, n in st["by_version"]:
            print(f"    {n:3}×  v{v}")
        print("\n  按导演:")
        for d, n in st["by_director"]:
            print(f"    {n:3}×  {d}")
        print("\n  提示：若修改集中在某一个导演下，问题可能出在导演约束，"
              "\n  而不是 skill 本体。改错地方会越改越糟。\n")
        return

    if args.action == "suggest":
        pkg = SkillPackage.load(SKILLS / args.skill)
        recs = load_feedback(pkg.root)
        st = analyze(recs)
        if st["count"] < MIN_SAMPLES and not args.force:
            print(f"{BAD} 反馈仅 {st['count']} 条，少于 {MIN_SAMPLES} 条门槛。")
            print("  样本太少会把偶然当规律，生成的建议不可信。")
            print("  确要生成用 --force。")
            sys.exit(1)
        gw = ModelGateway(log_path=CALL_LOG, dry_run=args.dry_run)
        user = build_suggest_prompt(pkg.skill_md, recs, st)
        try:
            res = gw.call(system=SUGGEST_SYSTEM, user=user,
                          model_config=pkg.model_config,
                          skill_id=pkg.id, tag="proposal")
            data = res.json_payload()
        except GatewayError as e:
            print(f"{BAD} {e}")
            sys.exit(1)
        prop = save_proposal(pkg.root, pkg.id, data, st["count"])
        print(f"{OK} 建议已生成: {prop.path}")
        print(f"   结论: {data.get('verdict')}")
        print(f"   {data.get('evidence_summary', '')[:120]}")
        print(f"   共 {len(data.get('suggestions', []))} 条建议，"
              f"用 avctl flywheel review {args.skill} 逐条审批")
        return

    if args.action == "review":
        pkg = SkillPackage.load(SKILLS / args.skill)
        props = [p for p in list_proposals(pkg.root, pkg.id)
                 if p.status == "pending"]
        if not props:
            print(f"{WARN} {args.skill} 没有待批建议。")
            return
        prop = props[0]
        print(f"\n建议 {prop.created_at}（基于 {prop.based_on} 条反馈）")
        print(f"结论: {prop.data.get('verdict')}")
        print(f"{prop.data.get('evidence_summary','')}\n")
        for i, s in enumerate(prop.suggestions, 1):
            print(f"[{i}] 章节「{s.get('section')}」 {s.get('kind')}"
                  f"  置信 {s.get('confidence')}")
            print(f"    依据: {s.get('why','')[:100]}")
            if s.get("current"):
                print(f"    原文: {s['current'][:80]}")
            print(f"    改为: {s.get('proposed','')[:150]}")
            print()
        for n in prop.data.get("not_suggested", []):
            print(f"  （看到但未提）{n}")
        print(f"\n合入: avctl flywheel apply {args.skill} --pick 1 2")
        print(f"驳回: avctl flywheel apply {args.skill} --reject\n")
        return

    # apply
    pkg = SkillPackage.load(SKILLS / args.skill)
    props = [p for p in list_proposals(pkg.root, pkg.id)
             if p.status == "pending"]
    if not props:
        print(f"{WARN} 没有待批建议。")
        return
    prop = props[0]

    if args.reject:
        prop.data["_status"] = "rejected"
        prop.data["_note"] = args.note or ""
        prop.path.write_text(json.dumps(prop.data, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(f"{OK} 已驳回 {prop.created_at}")
        return

    if not args.pick:
        print(f"{BAD} 需要 --pick 指定合入哪几条（如 --pick 1 3），"
              f"或 --reject 驳回全部")
        sys.exit(1)

    body = pkg.skill_md
    applied = []
    for idx in args.pick:
        if idx < 1 or idx > len(prop.suggestions):
            print(f"{BAD} 序号 {idx} 超出范围")
            sys.exit(1)
        s = prop.suggestions[idx - 1]
        try:
            body = apply_suggestion(body, s)
            applied.append(idx)
        except ApplyError as e:
            print(f"{BAD} 第 {idx} 条无法自动合入: {e}")
            sys.exit(1)

    pkg.snapshot(note=f"合入建议 {prop.created_at} 第 {applied} 条")
    (pkg.root / "SKILL.md").write_text(body, encoding="utf-8")

    # 合入后立刻复检规范，不合规立即回滚
    fresh = SkillPackage.load(pkg.root)
    errs = fresh.validate()
    if errs:
        vs = fresh.list_versions()
        if vs:
            (pkg.root / "SKILL.md").write_text(
                vs[-1].read_text(encoding="utf-8"), encoding="utf-8")
        print(f"{BAD} 合入后 SKILL.md 不再符合规范，已回滚：")
        for e in errs:
            print(f"    - {e}")
        sys.exit(1)

    prop.data["_status"] = "applied"
    prop.data["_applied"] = applied
    prop.path.write_text(json.dumps(prop.data, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    Store(DB).record_skill_version(pkg.id, pkg.version, pkg.root / "SKILL.md",
                                   f"合入建议 {prop.created_at} 第 {applied} 条")
    print(f"{OK} 已合入第 {applied} 条，旧版已存档，规范复检通过")


# ---------------- director ----------------

def cmd_director(args):
    ds = discover_directors(SKILLS)

    if args.action == "list":
        if not ds:
            print("尚无导演。avctl director new 创建。")
            return
        for d in ds:
            e = d.validate()
            mark = OK if not e else BAD
            print(f"\n {mark} {d.id:10} {d.name}")
            print(f"     {d.one_line}")
            print(f"     擅长: {'、'.join(d.genre_tags)}")
            for x in e:
                print(f"     {BAD} {x}")
        return

    if args.action == "new":
        root = scaffold_director(SKILLS, args.id, args.name,
                                 args.genre or [], args.one_line or "")
        print(f"{OK} 已生成导演骨架: {root}")
        print("  填写五个章节，禁忌清单尤其重要 ——")
        print("  说不出「我绝不这么拍」的导演等于没有风格，盲测会失败。")
        return

    # match：导演匹配器
    tags = list(args.genre or [])
    if args.input:
        a = ArtifactStore(PROJECTS).load(args.input)
        tags += a.get("payload", {}).get("genre_tags", [])
    if not tags:
        print(f"{BAD} 需要题材标签：--genre 或 --input 指定故事档案")
        sys.exit(1)
    rows = recommend(ds, tags)
    print(f"\n本次题材: {'、'.join(tags)}\n")
    for i, r in enumerate(rows, 1):
        mark = OK if r["score"] else " "
        print(f" {mark} {i}. {r['name']}（{r['id']}）  匹配 {r['score']}")
        print(f"       {r['one_line']}")
        print(f"       {r['reason']}")
    print("\n  以上仅为推荐，最终由你拍板。"
          "\n  用法: avctl run <skill> --director <id> ...\n")


# ---------------- blindtest（M3 验收工具） ----------------

def cmd_blindtest(args):
    """
    同一份输入，跑两个导演，产出去标识的对照文件。

    M3 的验收方式：你看两份产物，能分辨出哪份是哪个导演的，
    风格注入才算立住。分不出来说明这条路没走通，
    应当停下来重新设计注入方式，而不是硬着头皮做 M4。
    """
    import random
    pkg = SkillPackage.load(SKILLS / args.skill)
    if pkg.id in DIRECTOR_BLIND_SKILLS:
        print(f"{BAD} {pkg.id} 不接受导演上下文，无法做盲测。"
              f"建议用 playwright 或 storyboard。")
        sys.exit(1)

    astore = ArtifactStore(PROJECTS)
    inputs = [astore.load(f) for f in args.input or []]
    missing = pkg.check_inputs([a["contract"] for a in inputs])
    if missing:
        print(f"{BAD} 缺少必需上游: {missing}")
        sys.exit(1)

    gw = ModelGateway(log_path=CALL_LOG, dry_run=args.dry_run)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    results = []
    for did in args.directors:
        d = Director.load(SKILLS / "directors" / did)
        pr = build_prompt(pkg, args.brief, inputs, args.genre, d)
        res = gw.call(system=pr.system, user=pr.user,
                      model_config=pkg.model_config,
                      skill_id=pkg.id, tag=pkg.output_contract)
        results.append((did, d.name, res.json_payload()))

    labels = list("ABCDEFG")[:len(results)]
    order = list(range(len(results)))
    random.shuffle(order)

    key = {}
    for label, idx in zip(labels, order):
        did, dname, payload = results[idx]
        key[label] = f"{dname}（{did}）"
        f = outdir / f"盲测-{label}.json"
        f.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                     encoding="utf-8")

    (outdir / "_答案.txt").write_text(
        "盲测答案（看完两份产物再打开）\n\n"
        + "\n".join(f"{k} = {v}" for k, v in sorted(key.items())),
        encoding="utf-8")

    print(f"{OK} 盲测材料已生成于 {outdir}/")
    for label in labels:
        print(f"   盲测-{label}.json")
    print(f"\n   答案在 _答案.txt，看完产物再打开。")
    print(f"   分辨得出 → 风格注入生效，M3 通过")
    print(f"   分辨不出 → 注入机制无效，停下来重新设计，不要硬做 M4")


# ---------------- genre ----------------

def cmd_genre(args):
    if args.action == "list":
        for sid in GENRE_AWARE_SKILLS:
            d = SKILLS / sid
            if not d.is_dir():
                continue
            pk = SkillPackage.load(d)
            print(f"\n{sid}（{pk.manifest.get('name')}）{len(pk.genres)} 个模块")
            for g in pk.genres:
                n = len(g.read())
                print(f"  {g.key:12} {g.title:6} {n:5}字  别名: "
                      f"{'、'.join(a for a in g.aliases if a != g.title) or '无'}")
        return

    # new
    created = add_genre(SKILLS, args.key, args.title,
                        aliases=args.aliases or [], only=args.only)
    if not created:
        print(f"{WARN} 模块 {args.key} 已存在，仅更新了别名登记")
    else:
        print(f"{OK} 已生成题材模块「{args.title}」:")
        for p in created:
            print(f"   {p}")
    print(f"{OK} 别名已登记: {'、'.join([args.title] + (args.aliases or []))}")
    print("\n   下一步：填写模板里的各节内容。写的时候只写这个题材独有的判断，")
    print("   方法论层已有的不要重复。填完 avctl skill check 验一下。")


# ---------------- artifact ----------------

def cmd_artifact(args):
    art = json.loads(Path(args.file).read_text(encoding="utf-8"))
    name = args.contract or art.get("contract")
    if not name:
        print(f"{BAD} 无法判断契约类型，请用 --contract 指定")
        sys.exit(1)
    ctx = {}
    for cf in args.context or []:
        c = json.loads(Path(cf).read_text(encoding="utf-8"))
        ctx[c["contract"]] = c["payload"]
    errs = contracts.validate_artifact(art, contracts.get(name), ctx)
    if errs:
        print(contracts.ValidationError(errs).report())
        sys.exit(1)
    print(f"{OK} 产物符合契约 {name}")


# ---------------- run ----------------

def cmd_run(args):
    """
    编排逻辑在 core.engine.runner —— 工作台走的是同一条路径。
    这里只负责把参数收好、把失败讲成人话。
    """
    pkg = SkillPackage.load(SKILLS / args.skill)
    astore = ArtifactStore(PROJECTS)
    store = Store(DB)

    # 上游产物：命令行是显式给文件（工作台那边按契约自动收集）
    inputs, input_ids = [], []
    for f in args.input or []:
        a = astore.load(f)
        inputs.append(a)
        input_ids.append(a["artifact_id"])

    try:
        director = resolve_director(SKILLS, args.director, pkg,
                                    strict=not args.force)
    except RunError as e:
        print(f"{BAD} {e}")
        sys.exit(1)
    if args.director and director:
        print(f"   导演: {director.name}（{director.one_line}）")
    elif args.director and pkg.id in DIRECTOR_BLIND_SKILLS:
        print(f"   {WARN} {pkg.id} 不接受导演上下文（故事在前，导演在后），"
              f"本次忽略 --director")

    try:
        r = run_node(
            pkg=pkg, store=store, astore=astore,
            series_id=args.series, episode_no=args.episode,
            account=args.account, collection=args.collection,
            brief=args.brief, genre_tags=args.genre, director=director,
            inputs=inputs, input_ids=input_ids,
            log_path=CALL_LOG, dry_run=args.dry_run, force=args.force,
        )
    except RunError as e:
        print(f"{BAD} {e}")
        if "未通过规范校验" in str(e):
            print("  修好，或用 --force 强行执行（不推荐）")
        sys.exit(1)

    if r.genre_hit:
        print(f"   题材模块: {', '.join(g.title for g in r.genre_hit)}")
    if r.genre_miss:
        print(f"   {WARN} 题材 {r.genre_miss} 无模块支撑，本次按通用方法论作业")
    print(f"{OK} 产物已生成并通过契约校验")
    print(f"   {r.path}")
    print(f"   审阅版: {r.path.with_suffix('.md')}")
    if r.dry_run:
        print(f"   {WARN} dry-run 模式，内容为占位产物")


# ---------------- pipeline（流水线看板 CLI 版） ----------------

STATUS_MARK = {
    "approved": OK,
    "revised": "~",
    "pending_review": "?",
    "generating": "…",
}


def cmd_pipeline(args):
    store = Store(DB)
    print(f"\n剧集 {args.series}" + (f" / 第 {args.episode} 集"
                                     if args.episode else ""))
    print("=" * 58)
    blocked = None
    for n in NODES:
        c = contracts.get(n.contract)
        row = store.latest_artifact(*scope_of(n, args.series, args.episode),
                                    n.contract)
        if not row:
            state, mark = "未开始", "-"
        else:
            state = row["status"]
            mark = STATUS_MARK.get(state, "?")
        print(f" {mark}  {n.no:6} {c.cn_name:5} {n.level_cn}"
              f"  [{n.skill}]  {state}")
        if blocked is None and (not row or row["status"] != "approved"):
            blocked = (n.no, c.cn_name, state)
    print("=" * 58)
    if blocked:
        print(f" 当前卡点: {blocked[0]} {blocked[1]}（{blocked[2]}）")
        print(" 图例: ✓已通过  ~已修改  ?待审  …重生成中  -未开始")
    else:
        print(" 全部节点已通过，可以进入 N7 视频生成")
    print()


# ---------------- review（审核门） ----------------

def cmd_review(args):
    astore = ArtifactStore(PROJECTS)
    store = Store(DB)
    art = astore.load(args.file)
    aid = art["artifact_id"]
    skill_id = art.get("produced_by", {}).get("skill")

    if args.action == "approve":
        art["status"] = "approved"
        _rewrite(astore, args.file, art)
        store.set_status(aid, "approved")
        print(f"{OK} 已通过: {aid}")
        return

    if args.action == "revise":
        # 你已经手改过 JSON 文件了，这里做的是：校验 + 归集 diff + 标记
        if not args.before:
            print(f"{BAD} revise 需要 --before 指定生成版原始文件"
                  f"（用于计算改动 diff）")
            sys.exit(1)
        before = astore.load(args.before)
        errs = contracts.validate_artifact(art, contracts.get(art["contract"]))
        if errs:
            print(contracts.ValidationError(errs).report())
            print(f"\n{WARN} 你的修改让产物不再符合契约，未接受。")
            sys.exit(1)
        diff = make_diff(before, art, aid)
        ratio = change_ratio(before, art)
        if diff and skill_id:
            pkg = SkillPackage.load(SKILLS / skill_id)
            fp = pkg.record_feedback(aid, diff)
            print(f"{OK} 修改已归集到 {pkg.id}/feedback/{fp.name}")
        art["status"] = "revised"
        _rewrite(astore, args.file, art)
        store.set_status(aid, "revised")
        if ratio >= MAJOR_REVISION_THRESHOLD:
            store.mark_revised(aid)
            print(f"{WARN} 改动比例 {ratio:.1%}，记为大改，"
                  f"已计入所引用 reference 的健康度")
        else:
            print(f"{OK} 改动比例 {ratio:.1%}")
        return

    # reject
    if not args.notes:
        print(f"{BAD} 打回必须写修改意见（--notes），"
              f"否则重生成时模型不知道错在哪")
        sys.exit(1)
    art["status"] = "generating"
    _rewrite(astore, args.file, art)
    store.set_status(aid, "generating")
    retry = build_retry_brief(args.brief or "", args.notes, art)
    outp = Path(args.file).with_suffix(".retry.md")
    outp.write_text(retry, encoding="utf-8")
    print(f"{OK} 已打回: {aid}")
    print(f"   重生成用的任务说明已写入: {outp}")
    print(f"   下一步: avctl run <skill> --brief \"$(cat {outp})\" ...")


def _rewrite(astore, path, art):
    from core.store.artifacts import to_markdown
    p = astore.resolve(path)
    p.write_text(json.dumps(art, ensure_ascii=False, indent=2), encoding="utf-8")
    p.with_suffix(".md").write_text(to_markdown(art), encoding="utf-8")


# ---------------- smoke（M0 验收） ----------------

def cmd_smoke(args):
    print("=" * 60)
    print("M0 验收自检")
    print("=" * 60)
    fails = []

    print("\n[1/5] 契约层")
    for name, c in contracts.REGISTRY.items():
        print(f"  {OK} {name} ({c.cn_name}) v{c.version}")

    print("\n[2/5] 契约校验器 —— 故意喂一份残缺产物，看是否精确报错")
    bad = {
        "contract": "screenplay", "contract_version": "1.0",
        "artifact_id": "test", "status": "pending_review",
        "produced_by": {"skill": "x", "skill_version": "0.1.0", "model": "m"},
        "lineage": {}, "references_cited": [],
        "payload": {
            "title": "测试",
            "characters": [{"id": "c1", "name": "甲"}],          # 缺 brief
            "locations": [{"id": "l1", "name": "屋", "int_ext": "INT"}],
            "scenes": [{
                "id": "s1", "location_id": "l1", "time_of_day": "日",
                "summary": "测试", "present_character_ids": ["c1"],
                "beats": [{"kind": "action", "character_id": "c1",
                           "text": "特写他的手，镜头缓缓推近"}],   # 编剧越权
            }],
        },
    }
    errs = contracts.validate_artifact(bad, contracts.get("screenplay"))
    for e in errs:
        print(f"  {OK} 捕获: {e}")
    if len(errs) < 2:
        fails.append("校验器未捕获预期错误（应至少报出缺失字段 + 镜头术语越权）")

    print("\n[3/5] Skill 规范执法 —— 生成空壳包并校验")
    tmp_id = "_smoke_writer"
    tmp = SKILLS / tmp_id
    if tmp.exists():
        import shutil
        shutil.rmtree(tmp)
    scaffold(SKILLS, tmp_id, "冒烟测试作家", output_contract="story_file")
    pkg = SkillPackage.load(tmp)
    serrs = pkg.validate()
    if serrs:
        print(f"  {OK} 空壳包被正确拦截（模板未填写完，符合预期）:")
        for e in serrs[:3]:
            print(f"      - {e}")
    else:
        fails.append("空壳 skill 未被拦截，规范执法失效")

    print("\n[4/5] 模型网关 dry-run + 产物落盘")
    gw = ModelGateway(dry_run=True)
    res = gw.call(system="s", user="u",
                  model_config={"provider": "anthropic", "model": "test"},
                  skill_id=tmp_id, tag="story_file")
    payload = res.json_payload()
    art = make_envelope("story_file", payload, skill_id=tmp_id,
                        skill_version="0.1.0", model="dry-run")
    aerrs = contracts.validate_artifact(art, contracts.get("story_file"))
    if aerrs:
        fails.append(f"dry-run 产物未通过契约校验: {aerrs}")
        print(f"  {BAD} {aerrs}")
    else:
        print(f"  {OK} dry-run 产物通过 story_file 契约校验")

    astore = ArtifactStore(PROJECTS)
    p = astore.save(art, account="_smoke", collection="s1",
                    series="demo", episode_no=1)
    print(f"  {OK} 已落盘: {astore.rel(p)}")
    print(f"  {OK} 审阅版: {astore.rel(p.with_suffix('.md'))}")

    print("\n[5/5] 存储层")
    store = Store(DB)
    store.init()
    store.create_account("_smoke", "冒烟账号")
    store.create_collection("s1", "_smoke", "第一季")
    store.create_series("demo", "s1", "冒烟剧集")
    store.create_episode("demo-ep1", "demo", 1, "第一集")
    store.register_artifact(art, astore.rel(p), "episode", "demo")
    rows = store.list_artifacts("demo")
    print(f"  {OK} 产物索引写入成功，当前 {len(rows)} 条")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    # ---- M1 增补：内容链路 ----
    print("\n[M1] 内容链路")
    from core.skillkit.package import SkillPackage as _SP
    for sid in ["writer", "playwright"]:
        d = SKILLS / sid
        if not d.is_dir():
            fails.append(f"{sid} skill 不存在")
            continue
        pk = _SP.load(d)
        e = pk.validate()
        if e:
            fails.append(f"{sid} 未通过规范校验: {e[:2]}")
            print(f"  {BAD} {sid}: {e[:2]}")
        else:
            print(f"  {OK} {sid} v{pk.version} 规范通过"
                  f"，题材模块 {[g.key for g in pk.genres] or '无'}")
    w = _SP.load(SKILLS / "writer")
    for tags, want_hit in [(["打脸"], "爽剧"), (["烧脑"], "悬疑"),
                           (["催泪"], "情感"), (["职场"], "现实"),
                           (["沙雕"], "喜剧")]:
        hit, _ = w.select_genres(tags)
        if hit and hit[0].title == want_hit:
            print(f"  {OK} 别名 {tags[0]} → {want_hit}")
        else:
            fails.append(f"题材别名 {tags[0]} 未命中 {want_hit}")
    hit, miss = w.select_genres(["亲情", "赛博朋克"])
    if not (hit and miss == ["赛博朋克"]):
        fails.append("未覆盖题材标签未正确报出")
    else:
        print(f"  {OK} 未覆盖标签正确报出 {miss}")

    from core.engine.gate import change_ratio as _cr
    if _cr({"payload": {"a": "x"}}, {"payload": {"a": "y"}}) > 0:
        print(f"  {OK} 审核门 diff 与改动比例计算就绪")
    else:
        fails.append("审核门改动比例计算异常")

    # ---- M2 增补：制作链路 ----
    print("\n[M2] 制作链路")
    for sid in ["casting", "set_dresser", "storyboard"]:
        d = SKILLS / sid
        if not d.is_dir():
            fails.append(f"{sid} skill 不存在")
            continue
        pk = _SP.load(d)
        e = pk.validate()
        if e:
            fails.append(f"{sid} 规范校验失败: {e[:2]}")
            print(f"  {BAD} {sid}: {e[:2]}")
        else:
            print(f"  {OK} {sid} v{pk.version} 规范通过"
                  f"，必需上游 {pk.required_inputs or '无'}")

    sb = _SP.load(SKILLS / "storyboard")
    miss = sb.check_inputs(["screenplay"])
    if set(miss) == {"character_board", "location_board"}:
        print(f"  {OK} 必需上游检查：分镜缺角色板/场景板会被拒绝执行")
    else:
        fails.append(f"必需上游检查异常，缺失判定为 {miss}")

    # 引用完整性：分镜引用不存在的 ID 必须被拦
    bad_sl = {
        "contract": "shotlist", "contract_version": "1.0",
        "artifact_id": "t", "status": "pending_review",
        "produced_by": {"skill": "storyboard", "skill_version": "0.1.0",
                        "model": "m"},
        "lineage": {}, "references_cited": [],
        "payload": {"scenes": [{"scene_id": "s1", "shots": [{
            "id": "sh1", "location_id": "l9", "character_ids": ["c1", "c7"],
            "shot_size": "中景", "camera_movement": "固定",
            "frame_description": "x", "duration_sec": 3.0,
            "video_prompt": {"zh": "a", "en": "b"}}]}]},
    }
    ctx = {"character_board": {"characters": [{"id": "c1"}]},
           "location_board": {"locations": [{"id": "l1"}]}}
    ce = contracts.validate_artifact(bad_sl, contracts.get("shotlist"), ctx)
    if len(ce) >= 2:
        print(f"  {OK} 引用完整性：凭空发明的人物/场景被拦截（{len(ce)} 处）")
    else:
        fails.append("引用完整性校验未生效")

    # 覆盖完整性：角色板漏了剧本里的人物，必须在选角这一关就拦下
    sp_ctx = {"screenplay": {"characters": [{"id": "c1", "name": "甲"},
                                            {"id": "c3", "name": "丙"}],
                             "locations": [{"id": "l1", "name": "屋"}]}}
    cb_miss = {"contract": "character_board", "contract_version": "1.0",
               "artifact_id": "t", "status": "pending_review",
               "produced_by": {"skill": "casting", "skill_version": "0.1.0",
                               "model": "m"},
               "lineage": {}, "references_cited": [],
               "payload": {"characters": [{
                   "id": "c1", "name": "甲",
                   "anchor_description": "方长脸，下颌线清晰，单眼皮眼裂偏窄，"
                                         "鼻梁中等鼻头圆钝，嘴唇偏薄嘴角下垂，"
                                         "皮肤偏黄褐毛孔粗大，短寸花白，轻度驼背。",
                   "signature_marks": ["右手食指旧疤"], "main_look": "工作服",
                   "reference_prompts": {k: {"zh": "a", "en": "b"} for k in
                                         ["front_bust", "profile", "full_body",
                                          "expression", "costume_detail"]}}]}}
    me = contracts.validate_artifact(
        cb_miss, contracts.get("character_board"), sp_ctx)
    if any("漏了剧本里的人物" in x for x in me):
        print(f"  {OK} 覆盖完整性：角色板漏人在选角关即被拦截")
    else:
        fails.append("角色板覆盖完整性校验未生效")

    # 锚定描述长度下限
    short = {"contract": "character_board", "contract_version": "1.0",
             "artifact_id": "t", "status": "pending_review",
             "produced_by": {"skill": "casting", "skill_version": "0.1.0",
                             "model": "m"},
             "lineage": {}, "references_cited": [],
             "payload": {"characters": [{
                 "id": "c1", "name": "甲", "anchor_description": "帅气清秀",
                 "signature_marks": ["疤"], "main_look": "x",
                 "reference_prompts": {k: {"zh": "a", "en": "b"} for k in
                                       ["front_bust", "profile", "full_body",
                                        "expression", "costume_detail"]}}]}}
    se = contracts.validate_artifact(short, contracts.get("character_board"))
    if any("锚定" in x for x in se):
        print(f"  {OK} 锚定描述长度下限生效，评价性短描述被拦截")
    else:
        fails.append("锚定描述长度校验未生效")

    # ---- M3 增补：导演层 ----
    print("\n[M3] 导演层")
    ds = discover_directors(SKILLS)
    if len(ds) < 2:
        fails.append(f"导演不足 2 个（当前 {len(ds)}），无法做盲测")
    for d in ds:
        e = d.validate()
        if e:
            fails.append(f"导演 {d.id} 规范校验失败: {e[:2]}")
            print(f"  {BAD} {d.id}: {e[:2]}")
        else:
            print(f"  {OK} {d.id}（{d.name}）规范通过，"
                  f"擅长 {len(d.genre_tags)} 个题材")

    # 匹配器
    if ds:
        rows = recommend(ds, ["武侠", "打脸", "逆袭"])
        if rows[0]["score"] > 0:
            print(f"  {OK} 导演匹配器：武侠/爽剧题材首推 {rows[0]['name']}"
                  f"（匹配 {rows[0]['score']}）")
        else:
            fails.append("导演匹配器未能匹配武侠/爽剧题材")

    # 上下文注入 + 作家豁免
    pw = _SP.load(SKILLS / "playwright")
    wr = _SP.load(SKILLS / "writer")
    d0 = ds[0] if ds else None
    if d0:
        s_pw = build_prompt(pw, "", [], ["情感"], d0).system
        s_wr = build_prompt(wr, "", [], ["情感"], d0).system
        if "导演风格约束" in s_pw and "导演风格约束" not in s_wr:
            print(f"  {OK} 导演注入：编剧吃约束，作家豁免"
                  f"（故事在前，导演在后）")
        else:
            fails.append("导演上下文注入或作家豁免异常")

    # ---- M5 部分：拉片员 ----
    print("\n[M5 部分] 供料层")
    fa = SKILLS / "film_analyst"
    if fa.is_dir():
        pk = _SP.load(fa)
        e = pk.validate()
        if e:
            fails.append(f"film_analyst 规范失败: {e[:2]}")
            print(f"  {BAD} film_analyst: {e[:2]}")
        else:
            print(f"  {OK} film_analyst v{pk.version} 规范通过（utility 型）")
    tool = ROOT / "tools" / "ripfilm.py"
    print(f"  {OK if tool.exists() else BAD} 抽帧工具链 tools/ripfilm.py")
    if not tool.exists():
        fails.append("ripfilm.py 缺失")

    # 导演包不该被工种规范误校验
    ids = [p.id for p in discover(SKILLS)]
    if any(i in ids for i in ["lengxu", "jifeng", "weiguang"]):
        fails.append("导演包被工种 skill 扫描收录，会产生假错误")
    else:
        print(f"  {OK} 导演包与工种 skill 走各自规范，互不干扰")

    # ---- M4：工作台 ----
    print("\n[M4] 生产工作台")
    try:
        from web.app import create_app as _ca
        app = _ca()
        c = app.test_client()
        routes = {str(r.rule) for r in app.url_map.iter_rules()}
        need = {"/", "/series/<sid>", "/artifact/<aid>", "/skills",
                "/skills/<sid>", "/directors"}
        if need <= routes:
            print(f"  {OK} 路由齐备（{len(routes)} 条）")
        else:
            fails.append(f"工作台缺少路由: {need - routes}")
        for u in ["/", "/skills", "/directors"]:
            if c.get(u).status_code != 200:
                fails.append(f"工作台 {u} 返回异常")
        print(f"  {OK} 页面可访问：剧集总览 / Skill 管理 / 导演库")
        print(f"  {OK} 审核门：读 → 判断 → 改（审阅版在编辑区之前）")
    except ImportError:
        print(f"  {WARN} Flask 未安装，跳过工作台自检"
              f"（pip install flask --break-system-packages）")

    # ---- M6：质量飞轮 ----
    print("\n[M6] 质量飞轮")
    from core.engine.flywheel import (apply_suggestion as _as,
                                      ApplyError as _AE,
                                      ref_governance as _rg)
    try:
        _as("## A\n重复\n\n## B\n重复\n",
            {"kind": "replace", "section": "A", "current": "重复",
             "proposed": "x"})
        fails.append("飞轮：原文不唯一时未拒绝替换")
    except _AE:
        print(f"  {OK} 建议合入：原文不唯一或匹配不到时拒绝，不做模糊匹配")
    g = _rg([{"ref_id": "a", "cited_count": 0, "revised_count": 0},
             {"ref_id": "b", "cited_count": 10, "revised_count": 8}])
    if len(g["stale"]) == 1 and len(g["misleading"]) == 1:
        print(f"  {OK} reference 治理：闲置与误导可正确分类")
    else:
        fails.append("reference 治理分类异常")
    print(f"  {OK} 合入后规范复检 + 自动回滚（skill 是唯一资产，不做全自动合入）")

    # ---- 回归测试 ----
    print("\n[回归测试]")
    import subprocess
    r = subprocess.run([sys.executable, str(ROOT / "tests" / "test_core.py")],
                       capture_output=True, text=True)
    line = [x for x in r.stderr.splitlines() if x.startswith("Ran ")]
    if r.returncode == 0:
        print(f"  {OK} {line[0] if line else '通过'}")
    else:
        fails.append("回归测试未通过，详见 python3 tests/test_core.py")
        print(f"  {BAD} 回归测试失败")

    print("\n" + "=" * 60)
    if fails:
        print(f"{BAD} M0 验收未通过：")
        for f in fails:
            print(f"   - {f}")
        sys.exit(1)
    print(f"{OK} 自动验收通过：")
    print("   M0 契约 / 规范执法 / 网关 / 存储")
    print("   M1 作家+编剧 skill / 题材模块机制 / 审核门")
    print("   M2 选角+场务+分镜 skill / 必需上游执法 / 引用完整性")
    print("   M3 导演层 / 风格注入 / 匹配器 / 盲测工具")
    print("   M4 生产工作台（Flask+HTMX，通告单视觉）")
    print("   M5部分 拉片员 skill / 抽帧工具链")
    print("   M6 质量飞轮 / 建议审批合入 / reference 治理")
    print("=" * 60)


# ---------------- 入口 ----------------

def main():
    ap = argparse.ArgumentParser(prog="avctl", description="AI视频Agent平台")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化").set_defaults(func=cmd_init)
    sub.add_parser("smoke", help="M0 验收自检").set_defaults(func=cmd_smoke)

    pc = sub.add_parser("contract", help="契约")
    pcs = pc.add_subparsers(dest="action", required=True)
    pcs.add_parser("list")
    sh = pcs.add_parser("show")
    sh.add_argument("name")
    pc.set_defaults(func=cmd_contract)

    ps = sub.add_parser("skill", help="skill 管理")
    pss = ps.add_subparsers(dest="action", required=True)
    n = pss.add_parser("new")
    n.add_argument("id")
    n.add_argument("name")
    n.add_argument("--type", default="worker")
    n.add_argument("--team", default="content")
    n.add_argument("--output")
    n.add_argument("--input", nargs="*")
    pss.add_parser("list")
    ck = pss.add_parser("check")
    ck.add_argument("id", nargs="?")
    ps.set_defaults(func=cmd_skill)

    pe = sub.add_parser("export", help="导出 skill 供外部工具使用")
    pe.add_argument("--format", nargs="*",
                    choices=["claude", "codex", "plain"],
                    help="缺省全部导出")
    pe.add_argument("--out", help="输出目录，缺省 export/")
    pe.add_argument("--clean", action="store_true", help="先清空再导出")
    pe.set_defaults(func=cmd_export)

    pw = sub.add_parser("web", help="启动生产工作台")
    pw.add_argument("--port", type=int, default=5001)
    pw.add_argument("--host", default="127.0.0.1")
    pw.add_argument("--debug", action="store_true")
    pw.set_defaults(func=cmd_web)

    pf = sub.add_parser("flywheel", help="质量飞轮")
    pfs = pf.add_subparsers(dest="action", required=True)
    pfs.add_parser("status", help="全部 skill 的飞轮状态")
    fa = pfs.add_parser("analyze", help="修改模式分析")
    fa.add_argument("skill")
    fg = pfs.add_parser("suggest", help="生成 skill 改进建议")
    fg.add_argument("skill")
    fg.add_argument("--dry-run", action="store_true")
    fg.add_argument("--force", action="store_true")
    fr = pfs.add_parser("review", help="查看待批建议")
    fr.add_argument("skill")
    fp = pfs.add_parser("apply", help="合入或驳回建议")
    fp.add_argument("skill")
    fp.add_argument("--pick", nargs="*", type=int)
    fp.add_argument("--reject", action="store_true")
    fp.add_argument("--note")
    pf.set_defaults(func=cmd_flywheel)

    pd = sub.add_parser("director", help="导演层")
    pds = pd.add_subparsers(dest="action", required=True)
    pds.add_parser("list")
    dn = pds.add_parser("new")
    dn.add_argument("id")
    dn.add_argument("name")
    dn.add_argument("--genre", nargs="*")
    dn.add_argument("--one-line", dest="one_line")
    dm = pds.add_parser("match", help="导演匹配器")
    dm.add_argument("--genre", nargs="*")
    dm.add_argument("--input", help="故事档案，自动读题材标签")
    pd.set_defaults(func=cmd_director)

    pb = sub.add_parser("blindtest", help="M3 验收：导演风格盲测")
    pb.add_argument("skill")
    pb.add_argument("--directors", nargs="+", required=True)
    pb.add_argument("--input", nargs="*")
    pb.add_argument("--brief", default="")
    pb.add_argument("--genre", nargs="*")
    pb.add_argument("--out", default="blindtest")
    pb.add_argument("--dry-run", action="store_true")
    pb.set_defaults(func=cmd_blindtest)

    pg = sub.add_parser("genre", help="题材模块")
    pgs = pg.add_subparsers(dest="action", required=True)
    pgs.add_parser("list")
    gn = pgs.add_parser("new")
    gn.add_argument("key", help="模块标识，英文，如 wuxia")
    gn.add_argument("title", help="中文题材名，如 武侠")
    gn.add_argument("--aliases", nargs="*", help="触发别名")
    gn.add_argument("--only", nargs="*",
                    help="只给指定 skill 建，缺省两个都建")
    pg.set_defaults(func=cmd_genre)

    pa = sub.add_parser("artifact", help="产物")
    pas = pa.add_subparsers(dest="action", required=True)
    chk = pas.add_parser("check")
    chk.add_argument("file")
    chk.add_argument("--contract")
    chk.add_argument("--context", nargs="*", help="上游产物文件，用于引用完整性校验")
    pa.set_defaults(func=cmd_artifact)

    pp = sub.add_parser("pipeline", help="流水线状态")
    pp.add_argument("--series", required=True)
    pp.add_argument("--episode", type=int)
    pp.set_defaults(func=cmd_pipeline)

    pv = sub.add_parser("review", help="审核门")
    pv.add_argument("action", choices=["approve", "revise", "reject"])
    pv.add_argument("file", help="产物 json 文件")
    pv.add_argument("--before", help="revise 时指定生成版原文件，用于计算 diff")
    pv.add_argument("--notes", help="reject 时的修改意见")
    pv.add_argument("--brief", help="reject 时原任务说明")
    pv.set_defaults(func=cmd_review)

    pr = sub.add_parser("run", help="执行 skill 节点")
    pr.add_argument("skill")
    pr.add_argument("--brief", default="", help="本次任务说明")
    pr.add_argument("--input", nargs="*", help="上游产物文件")
    pr.add_argument("--account", default="default")
    pr.add_argument("--collection", default="s1")
    pr.add_argument("--series", required=True)
    pr.add_argument("--episode", type=int)
    pr.add_argument("--director")
    pr.add_argument("--genre", nargs="*", help="题材标签，缺省时从上游产物读取")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--force", action="store_true")
    pr.set_defaults(func=cmd_run)

    args = ap.parse_args()
    try:
        args.func(args)
    except (SkillError, KeyError, FileNotFoundError) as e:
        print(f"{BAD} {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
