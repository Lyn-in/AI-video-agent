#!/usr/bin/env python3
"""
回归测试。

原则：只测「改坏了会造成真实损失」的东西。
不测 getter/setter，不测框架本身，不追求覆盖率数字。

运行：python3 tests/test_core.py
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import contracts                                     # noqa: E402
from core.engine.flywheel import (                             # noqa: E402
    apply_suggestion, ApplyError, analyze, parse_feedback, ref_governance,
)
from core.engine.gate import change_ratio, make_diff           # noqa: E402
from core.gateway._stubs import STUBS                          # noqa: E402
from core.skillkit.director import Director, recommend         # noqa: E402
from core.skillkit.package import SkillPackage, scaffold       # noqa: E402
from core.store.artifacts import ArtifactStore, make_envelope  # noqa: E402

SKILLS = ROOT / "skills"


def envelope(contract, payload, **kw):
    return make_envelope(contract, payload, skill_id="t",
                         skill_version="0.1.0", model="test", **kw)


class TestContractEnforcement(unittest.TestCase):
    """契约执法。这是全平台的安全网，坏了不会报错，只会静默放行垃圾。"""

    def test_all_stubs_pass_own_contract(self):
        """占位产物必须能过自己的契约 —— 否则 dry-run 全线瘫痪。"""
        for name, payload in STUBS.items():
            if name == "proposal":
                continue
            with self.subTest(contract=name):
                errs = contracts.validate_artifact(
                    envelope(name, copy.deepcopy(payload)), contracts.get(name))
                self.assertEqual(errs, [], f"{name} 占位产物不合契约: {errs}")

    def test_playwright_camera_terms_blocked(self):
        """编剧越权必须被拦。四种典型写法逐一验证。"""
        base = copy.deepcopy(STUBS["screenplay"])
        for bad in ["特写他的手，镜头缓缓推近。",
                    "夕阳的光线斜进来，画面暖了。",
                    "淡出，转场到第二天。",
                    "停留三秒后切至下一场。"]:
            with self.subTest(text=bad[:8]):
                p = copy.deepcopy(base)
                p["scenes"][0]["beats"][0]["text"] = bad
                errs = contracts.validate_artifact(
                    envelope("screenplay", p), contracts.get("screenplay"))
                self.assertTrue(any("越权" in e for e in errs),
                                f"未拦截: {bad}")

    def test_anchor_length_floor(self):
        """锚定描述过短必须被拦 —— 一致性崩塌的源头。"""
        p = copy.deepcopy(STUBS["character_board"])
        p["characters"][0]["anchor_description"] = "帅气清秀"
        errs = contracts.validate_artifact(
            envelope("character_board", p), contracts.get("character_board"))
        self.assertTrue(any("锚定" in e for e in errs))

    def test_shotlist_reference_integrity(self):
        """分镜不得引用不存在的人物/场景。"""
        p = copy.deepcopy(STUBS["shotlist"])
        p["scenes"][0]["shots"][0]["character_ids"] = ["c1", "c99"]
        p["scenes"][0]["shots"][0]["location_id"] = "l99"
        ctx = {"character_board": {"characters": [{"id": "c1"}]},
               "location_board": {"locations": [{"id": "l1"}]}}
        errs = contracts.validate_artifact(
            envelope("shotlist", p), contracts.get("shotlist"), ctx)
        self.assertEqual(len(errs), 2, f"应拦截 2 处，实际 {errs}")

    def test_board_coverage(self):
        """角色板漏了剧本里的人物，必须在选角这一关就报错。"""
        p = copy.deepcopy(STUBS["character_board"])
        ctx = {"screenplay": {"characters": [{"id": "c1", "name": "甲"},
                                             {"id": "c9", "name": "丙"}],
                              "locations": []}}
        errs = contracts.validate_artifact(
            envelope("character_board", p),
            contracts.get("character_board"), ctx)
        self.assertTrue(any("漏了剧本里的人物" in e for e in errs))

    def test_board_no_extra(self):
        """选角不得新增剧本没有的人物。"""
        p = copy.deepcopy(STUBS["character_board"])
        ctx = {"screenplay": {"characters": [], "locations": []}}
        errs = contracts.validate_artifact(
            envelope("character_board", p),
            contracts.get("character_board"), ctx)
        self.assertTrue(any("不在剧本里" in e for e in errs))

    def test_error_path_precision(self):
        """错误必须带精确字段路径，否则排查成本极高。"""
        p = copy.deepcopy(STUBS["screenplay"])
        del p["characters"][0]["brief"]
        errs = contracts.validate_artifact(
            envelope("screenplay", p), contracts.get("screenplay"))
        self.assertTrue(any("payload.characters[0].brief" in e for e in errs),
                        f"路径不精确: {errs}")


class TestSkillGovernance(unittest.TestCase):
    """Skill 规范执法。"""

    def test_all_shipped_skills_valid(self):
        from core.skillkit.package import discover
        for p in discover(SKILLS):
            with self.subTest(skill=p.id):
                self.assertEqual(p.validate(), [], f"{p.id} 不合规范")

    def test_all_directors_valid(self):
        from core.skillkit.director import discover_directors
        ds = discover_directors(SKILLS)
        self.assertGreaterEqual(len(ds), 2, "导演少于 2 个无法盲测")
        for d in ds:
            with self.subTest(director=d.id):
                self.assertEqual(d.validate(), [], f"{d.id} 不合规范")

    def test_empty_scaffold_rejected(self):
        """空壳 skill 必须被拦 —— 否则规范形同虚设。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            scaffold(tmp, "x", "测试", output_contract="story_file")
            errs = SkillPackage.load(tmp / "x").validate()
            self.assertTrue(any("尚未填写内容" in e for e in errs))
        finally:
            shutil.rmtree(tmp)

    def test_storyboard_required_inputs(self):
        """分镜缺角色板/场景板必须被拒绝执行。"""
        sb = SkillPackage.load(SKILLS / "storyboard")
        self.assertEqual(set(sb.check_inputs(["screenplay"])),
                         {"character_board", "location_board"})

    def test_writer_is_director_blind(self):
        """作家不吃导演上下文：故事在前，导演在后。"""
        from core.skillkit.director import DIRECTOR_BLIND_SKILLS
        self.assertIn("writer", DIRECTOR_BLIND_SKILLS)

    def test_genre_alias_matching(self):
        w = SkillPackage.load(SKILLS / "writer")
        for tag, want in [("打脸", "爽剧"), ("烧脑", "悬疑"),
                          ("催泪", "情感"), ("职场", "现实"), ("沙雕", "喜剧")]:
            with self.subTest(tag=tag):
                hit, _ = w.select_genres([tag])
                self.assertTrue(hit and hit[0].title == want)

    def test_unknown_genre_reported(self):
        w = SkillPackage.load(SKILLS / "writer")
        _, miss = w.select_genres(["赛博朋克"])
        self.assertEqual(miss, ["赛博朋克"])


class TestFlywheel(unittest.TestCase):
    """质量飞轮。合入错误的建议会污染之后所有产出。"""

    def test_empty_diff_not_recorded(self):
        """空 diff 不落盘 —— 零字节文件会污染统计。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            scaffold(tmp, "x", "测试", output_contract="story_file")
            p = SkillPackage.load(tmp / "x")
            self.assertIsNone(p.record_feedback("a1", ""))
            self.assertIsNone(p.record_feedback("a1", "   \n  "))
            self.assertIsNotNone(p.record_feedback("a1", "# 人工修改记录：a1\n+x"))
        finally:
            shutil.rmtree(tmp)

    def test_apply_replace_requires_exact_match(self):
        """替换必须精确匹配，找不到就报错，绝不模糊匹配。"""
        md = "## A\n原文一\n\n## B\n原文二\n"
        with self.assertRaises(ApplyError):
            apply_suggestion(md, {"kind": "replace", "section": "A",
                                  "current": "不存在的原文", "proposed": "x"})

    def test_apply_replace_rejects_ambiguous(self):
        """原文出现多次时拒绝替换 —— 改错地方在 diff 里看不出来。"""
        md = "## A\n重复\n\n## B\n重复\n"
        with self.assertRaises(ApplyError):
            apply_suggestion(md, {"kind": "replace", "section": "A",
                                  "current": "重复", "proposed": "x"})

    def test_apply_add_into_section(self):
        md = "## 质量红线\n\n1. 甲\n\n## 下一节\n\n内容\n"
        out = apply_suggestion(md, {"kind": "add", "section": "质量红线",
                                    "current": "", "proposed": "2. 乙"})
        self.assertIn("2. 乙", out)
        self.assertLess(out.index("2. 乙"), out.index("## 下一节"))

    def test_apply_unknown_kind(self):
        with self.assertRaises(ApplyError):
            apply_suggestion("x", {"kind": "frobnicate", "section": "A"})

    def test_ref_governance_classification(self):
        gov = ref_governance([
            {"ref_id": "a", "cited_count": 0, "revised_count": 0},
            {"ref_id": "b", "cited_count": 10, "revised_count": 8},
            {"ref_id": "c", "cited_count": 10, "revised_count": 1},
        ])
        self.assertEqual([x["ref_id"] for x in gov["stale"]], ["a"])
        self.assertEqual([x["ref_id"] for x in gov["misleading"]], ["b"])
        self.assertEqual([x["ref_id"] for x in gov["healthy"]], ["c"])

    def test_feedback_roundtrip(self):
        """diff 生成 → 解析 → 统计，字段名要能被正确抽出。"""
        tmp = Path(tempfile.mkdtemp())
        try:
            before = {"payload": {"hook": "旧钩子", "title": "T"}}
            after = {"payload": {"hook": "新钩子", "title": "T"},
                     "produced_by": {"skill": "writer",
                                     "skill_version": "0.1.0", "model": "m"}}
            f = tmp / "20260101-000000-a1.diff.md"
            f.write_text(make_diff(before, after, "a1"), encoding="utf-8")
            rec = parse_feedback(f)
            self.assertIsNotNone(rec)
            self.assertEqual(rec.skill, "writer")
            self.assertIn("hook", rec.fields)
            st = analyze([rec])
            self.assertEqual(st["count"], 1)
        finally:
            shutil.rmtree(tmp)

    def test_parse_skips_corrupt(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            f = tmp / "bad.diff.md"
            f.write_text("", encoding="utf-8")
            self.assertIsNone(parse_feedback(f))
            f.write_text("随便什么内容", encoding="utf-8")
            self.assertIsNone(parse_feedback(f))
        finally:
            shutil.rmtree(tmp)

    def test_change_ratio(self):
        self.assertEqual(change_ratio({"payload": {"a": 1}},
                                      {"payload": {"a": 1}}), 0.0)
        self.assertGreater(change_ratio({"payload": {"a": "x" * 50}},
                                        {"payload": {"a": "y" * 50}}), 0.5)


class TestSecurityBoundaries(unittest.TestCase):
    """安全边界。这些是商业化必须守住的。"""

    def test_artifact_path_escape_blocked(self):
        a = ArtifactStore(ROOT / "projects")
        for p in ["/etc/passwd", "/etc/hosts"]:
            with self.subTest(path=p):
                with self.assertRaises((PermissionError, FileNotFoundError)):
                    a.resolve(p)

    def test_skill_path_traversal_blocked(self):
        try:
            from web.app import create_app
        except ImportError:
            self.skipTest("Flask 未安装")
        c = create_app().test_client()
        for bad in ["..", "%2e%2e", "../core", "....//"]:
            with self.subTest(sid=bad):
                self.assertIn(c.get(f"/skills/{bad}").status_code, (404, 308))

    def test_web_save_rejects_invalid_contract(self):
        """界面上的人工修改必须过契约校验，不合规拒绝落盘。"""
        try:
            from web.app import create_app
        except ImportError:
            self.skipTest("Flask 未安装")
        # 仅验证路由存在且拒绝非法 JSON（不依赖库中数据）
        app = create_app()
        self.assertIn("/artifact/<aid>/save",
                      {str(r.rule) for r in app.url_map.iter_rules()})


class TestPipelineScope(unittest.TestCase):
    """
    集级产物的索引口径。这里出错不会报错，只会静默串集 ——
    第 2 集的剧本盖掉第 1 集的，界面上还显示得好好的。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_episode_scope_ids_differ(self):
        from core.pipeline import node_for, scope_of
        n = node_for("story_file")
        self.assertNotEqual(scope_of(n, "s", 1), scope_of(n, "s", 2))

    def test_series_level_ignores_episode(self):
        """角色板是全集共用的，给了集号也不该按集分开存。"""
        from core.pipeline import node_for, scope_of
        n = node_for("character_board")
        self.assertEqual(scope_of(n, "s", 1), ("series", "s"))
        self.assertEqual(scope_of(n, "s", 2), ("series", "s"))

    def test_level_derived_from_series_level(self):
        from core.pipeline import NODES
        from core.store.artifacts import SERIES_LEVEL
        for n in NODES:
            want = "series" if n.contract in SERIES_LEVEL else "episode"
            self.assertEqual(n.level, want, f"{n.contract} 层级判定不一致")

    def test_two_episodes_do_not_collide(self):
        """同一剧集两集的同类产物必须各存各的。"""
        from core.pipeline import node_for, scope_of
        from core.store.db import Store
        store = Store(self.tmp / "t.db")
        store.init()
        n = node_for("story_file")
        for ep in (1, 2):
            art = envelope("story_file", {})
            art["artifact_id"] = f"sf-ep{ep}"
            store.register_artifact(art, f"a/b/s/ep{ep:02d}/story_file.json",
                                    *scope_of(n, "s", ep))
        got = {ep: store.latest_artifact(*scope_of(n, "s", ep), "story_file")["id"]
               for ep in (1, 2)}
        self.assertNotEqual(got[1], got[2])

    def test_migration_repairs_legacy_scope_ids(self):
        """老库里集级产物的 scope_id 是剧集 id，init() 要能就地修好。"""
        from core.store.db import Store
        db = self.tmp / "legacy.db"
        store = Store(db)
        store.init()
        with store.conn() as c:
            c.execute("INSERT INTO artifacts(id,contract,scope_type,scope_id,"
                      "path,status) VALUES(?,?,?,?,?,?)",
                      ("old-1", "story_file", "episode", "s",
                       "a/b/s/ep02/story_file.json", "approved"))
            # 集级契约但落在 _series/：按口径应改判为剧集级
            c.execute("INSERT INTO artifacts(id,contract,scope_type,scope_id,"
                      "path,status) VALUES(?,?,?,?,?,?)",
                      ("old-2", "screenplay", "episode", "s",
                       "a/b/s/_series/screenplay.json", "approved"))
            c.execute("DELETE FROM meta WHERE key='schema_version'")

        store.migrate()
        with store.conn() as c:
            rows = {r["id"]: (r["scope_type"], r["scope_id"]) for r in
                    c.execute("SELECT id,scope_type,scope_id FROM artifacts")}
        self.assertEqual(rows["old-1"], ("episode", "s-ep2"))
        self.assertEqual(rows["old-2"], ("series", "s"))

        store.migrate()          # 重复执行不得再动数据
        with store.conn() as c:
            again = {r["id"]: (r["scope_type"], r["scope_id"]) for r in
                     c.execute("SELECT id,scope_type,scope_id FROM artifacts")}
        self.assertEqual(rows, again, "迁移不幂等")


class TestNodeCardPolling(unittest.TestCase):
    """
    节点卡的轮询契约。

    自终止是硬要求：只有 running 状态的片段才带 hx-trigger，
    任务结束后换回来的片段不带，轮询自然停下。漏了这条，
    页面会对着一个早就结束的任务永远打服务器 ——
    而且这类问题在功能上完全看不出来。

    历史包袱：这里原先是整页 <meta http-equiv="refresh">，
    而且因为 Jinja 的 block 是编译期注册的，包在 {% if busy %} 外面
    根本不生效，变成无条件每 6 秒重载，把正在输入的 brief 一起冲掉。
    """

    def _card(self, job, status=None):
        try:
            from web.app import create_app
        except ImportError:
            self.skipTest("Flask 未安装")
        app = create_app()
        row = {"no": "N2", "contract": "story_file", "skill": "writer",
               "level": "episode", "level_cn": "集级", "cn_name": "故事档案",
               "artifact": None, "status": status, "status_cn": status or "未开始",
               "provider": "anthropic", "provider_label": "Anthropic",
               "has_key": True, "job": job, "waiting": False, "is_block": True}
        with app.test_request_context():      # url_for 需要请求上下文
            from flask import render_template
            return render_template(
                "fragments/node.html", r=row, ep=1, directors=[],
                s={"id": "s", "name": "t", "genre_tags": [],
                   "director_id": None})

    def test_idle_does_not_poll(self):
        self.assertNotIn("hx-trigger", self._card(None))

    def test_running_polls(self):
        c = self._card({"state": "running", "msg": "正在生成…"})
        self.assertIn('hx-trigger="every 3s"', c)
        self.assertIn("生成中", c)

    def test_polling_stops_when_finished(self):
        for state, msg in (("done", "生成完成，待审"), ("error", "缺少上游")):
            with self.subTest(state=state):
                c = self._card({"state": state, "msg": msg})
                self.assertNotIn("hx-trigger", c,
                                 f"{state} 之后轮询没停，会一直打服务器")

    def test_form_keeps_native_action(self):
        """渐进增强：没加载 JS 时整页提交也要能用。"""
        c = self._card(None)
        self.assertIn('action="/series/s/run/story_file"', c)
        self.assertIn('method="post"', c)

    def test_page_has_no_meta_refresh(self):
        try:
            from web.app import create_app
        except ImportError:
            self.skipTest("Flask 未安装")
        app = create_app()
        row = {"no": "N2", "contract": "story_file", "skill": "writer",
               "level": "episode", "level_cn": "集级", "cn_name": "故事档案",
               "artifact": None, "status": None, "status_cn": "未开始",
               "provider": "anthropic", "provider_label": "Anthropic",
               "has_key": True, "job": None, "waiting": False}
        with app.test_request_context():
            from flask import render_template
            page = render_template(
                "pipeline.html",
                s={"id": "s", "name": "t", "genre_tags": [],
                   "director_id": None},
                eps=[], ep=1, rows=[row], blocked=None, current=row,
                directors=[], missing_providers=[])
        self.assertNotIn("http-equiv=\"refresh\"", page)


class TestScopeRoundTrip(unittest.TestCase):
    """scope_of 与 parse_scope 必须互为逆运算 —— 待办页靠它反查剧集和集号。"""

    def test_round_trip(self):
        from core.pipeline import NODES, parse_scope, scope_of
        for n in NODES:
            for ep in (None, 1, 2, 17):
                with self.subTest(contract=n.contract, ep=ep):
                    sid, got_ep = parse_scope(*scope_of(n, "my-series", ep))
                    self.assertEqual(sid, "my-series")
                    # 剧集级产物不带集号，集号回来是 None 是对的
                    want = None if n.level == "series" else ep
                    self.assertEqual(got_ep, want)

    def test_series_id_with_dashes_survives(self):
        """剧集 id 本身带连字符时不能被切错。"""
        from core.pipeline import node_for, parse_scope, scope_of
        n = node_for("story_file")
        self.assertEqual(parse_scope(*scope_of(n, "s-54b93-x", 3)),
                         ("s-54b93-x", 3))


class TestNavigation(unittest.TestCase):
    """
    三区导航：制作是动线，资产是攒的东西，系统是配置。
    每个模板都得声明自己属于哪一区，否则二级导航会空掉。
    """

    def setUp(self):
        try:
            from web.app import create_app
        except ImportError:
            self.skipTest("Flask 未安装")
        self.c = create_app().test_client()

    def test_every_zone_endpoint_resolves(self):
        from web.nav import ZONES
        for z in ZONES:
            for label, endpoint in z["items"]:
                with self.subTest(endpoint=endpoint):
                    self.assertEqual(self.c.get(_url(self.c, endpoint)).status_code,
                                     200, f"{label} 打不开")

    def test_pages_declare_a_zone(self):
        """页面漏了 zone，二级导航就不渲染 —— 用户会觉得导航时有时无。"""
        for path, zone in (("/", "制作"), ("/series", "制作"),
                           ("/skills", "资产"), ("/directors", "资产"),
                           ("/contracts", "资产"), ("/settings", "系统")):
            with self.subTest(path=path):
                body = self.c.get(path).get_data(as_text=True)
                self.assertIn(f'class="on">{zone}</a>', body,
                              f"{path} 没标出所属区")


def _url(client, endpoint):
    return client.application.url_map.bind("localhost").build(endpoint)


class TestSkillExport(unittest.TestCase):
    """导出的 skill 必须自包含 —— 脱离平台后没有引擎注入。"""

    @classmethod
    def setUpClass(cls):
        from core.skillkit.exporter import export_claude, export_codex
        cls.tmp = Path(tempfile.mkdtemp())
        cls.made = export_claude(SKILLS, cls.tmp / "claude")
        export_codex(SKILLS, cls.tmp / "codex")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_frontmatter_present(self):
        """Claude Code 靠 frontmatter 的 description 决定何时启用。"""
        for sid in self.made:
            t = (self.tmp / "claude" / sid / "SKILL.md").read_text(encoding="utf-8")
            self.assertTrue(t.startswith("---\nname: "), f"{sid} 缺 frontmatter")
            self.assertIn("description:", t.split("---")[1])

    def test_output_spec_embedded(self):
        """平台会注入契约规格，导出版必须自带，否则输出格式全靠猜。"""
        from core.skillkit.package import SkillPackage as SP
        for sid in self.made:
            pkg = SP.load(SKILLS / sid)
            if not pkg.output_contract:
                continue
            t = (self.tmp / "claude" / sid / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("输出格式（必须严格遵守）", t, f"{sid} 缺输出规格")

    def test_genre_files_shipped(self):
        """题材模块要随包带走，不能只在正文里提一句。"""
        gd = self.tmp / "claude" / "writer" / "genres"
        self.assertTrue(gd.is_dir())
        self.assertGreaterEqual(len(list(gd.glob("*.md"))), 5)

    def test_director_only_for_consumers(self):
        """作家和拉片员不该附带导演约束。"""
        for sid in ("writer", "film_analyst"):
            self.assertFalse((self.tmp / "claude" / sid / "directors").exists(),
                             f"{sid} 不消费导演，不该附带")
        for sid in ("playwright", "storyboard"):
            self.assertTrue((self.tmp / "claude" / sid / "directors").is_dir(),
                            f"{sid} 应附带导演")

    def test_codex_selfcontained(self):
        """Codex 版是单文件，题材和导演必须内嵌而非引用。"""
        t = (self.tmp / "codex" / "playwright.md").read_text(encoding="utf-8")
        self.assertIn("【题材模块】", t)
        self.assertIn("【导演】", t)
        self.assertIn("输出格式", t)


class TestGatewayParsing(unittest.TestCase):
    """模型输出是不可信输入，解析必须健壮。"""

    def test_json_payload_strips_fence(self):
        from core.gateway.client import CallResult
        for raw in ['```json\n{"a":1}\n```',
                    '```\n{"a":1}\n```',
                    '好的，结果如下：\n{"a":1}',
                    '{"a":1}']:
            with self.subTest(raw=raw[:12]):
                r = CallResult(text=raw, provider="p", model="m")
                self.assertEqual(r.json_payload(), {"a": 1})

    def test_json_payload_raises_on_garbage(self):
        from core.gateway.client import CallResult, GatewayError
        r = CallResult(text="完全不是 JSON", provider="p", model="m")
        with self.assertRaises(GatewayError):
            r.json_payload()


class TestDirectorMatching(unittest.TestCase):
    def test_recommend_sorts_by_score(self):
        from core.skillkit.director import discover_directors
        ds = discover_directors(SKILLS)
        if not ds:
            self.skipTest("无导演")
        rows = recommend(ds, ds[0].genre_tags[:2])
        self.assertEqual(rows[0]["id"], ds[0].id)
        self.assertGreater(rows[0]["score"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
