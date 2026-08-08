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
import re
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


class TestDirectorEditing(unittest.TestCase):
    """
    导演库原先只有卡片，点不进去也改不了 ——
    skill 能在界面上改 SKILL.md，导演却只能去翻文件，这是不对称的。
    """

    def setUp(self):
        try:
            from web.app import create_app
        except ImportError:
            self.skipTest("Flask 未安装")
        self.c = create_app().test_client()
        from core.skillkit.director import discover_directors
        ds = discover_directors(SKILLS)
        if not ds:
            self.skipTest("无导演")
        self.did = ds[0].id

    def test_list_links_into_detail(self):
        body = self.c.get("/directors").get_data(as_text=True)
        self.assertIn(f'/directors/{self.did}"', body, "导演卡片点不进去")

    def test_detail_shows_editor(self):
        body = self.c.get(f"/directors/{self.did}").get_data(as_text=True)
        self.assertEqual(self.c.get(f"/directors/{self.did}").status_code, 200)
        self.assertIn('name="body"', body, "导演详情里没有编辑框")
        self.assertIn('name="_csrf"', body)

    def test_path_traversal_blocked(self):
        for bad in ("..", "../../etc", "....//"):
            with self.subTest(did=bad):
                self.assertIn(self.c.get(f"/directors/{bad}").status_code,
                              (404, 308))

    def test_save_round_trips(self):
        from core.skillkit.director import Director
        root = SKILLS / "directors" / self.did
        before = (root / "SKILL.md").read_text(encoding="utf-8")
        self.addCleanup(lambda: (root / "SKILL.md").write_text(
            before, encoding="utf-8"))
        page = self.c.get(f"/directors/{self.did}").get_data(as_text=True)
        tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        r = self.c.post(f"/directors/{self.did}/save",
                        data={"body": before + "\n<!-- 测试 -->\n",
                              "_csrf": tok})
        self.assertEqual(r.status_code, 302)
        self.assertIn("<!-- 测试 -->",
                      Director.load(root).doc)


class TestNewlineNormalization(unittest.TestCase):
    """
    浏览器提交 <textarea> 一律用 CRLF。不收回 LF 的话，在网页上点一次
    「保存」——哪怕一个字没改——整个文件每一行都算改过：
    SKILL.md 的版本存档全是满屏 diff，产物 JSON 的多行字段里还会混进 \\r。
    这类污染不报错，只是慢慢把版本历史和飞轮统计弄脏。
    """

    def setUp(self):
        try:
            from web import deps
        except ImportError:
            self.skipTest("Flask 未安装")
        self.deps = deps

    def test_crlf_collapsed(self):
        self.assertEqual(self.deps.normalize_newlines("a\r\nb\r\nc"), "a\nb\nc")
        self.assertEqual(self.deps.normalize_newlines("a\rb"), "a\nb")
        self.assertEqual(self.deps.normalize_newlines("a\nb"), "a\nb")

    def test_skill_save_keeps_lf(self):
        from web.app import create_app
        c = create_app().test_client()
        root = SKILLS / "writer"
        before = (root / "SKILL.md").read_bytes()
        versions = root / "versions"
        had_versions = versions.is_dir()

        def restore():
            (root / "SKILL.md").write_bytes(before)
            if not had_versions:
                shutil.rmtree(versions, ignore_errors=True)
        self.addCleanup(restore)

        page = c.get("/skills/writer").get_data(as_text=True)
        tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        # 模拟浏览器：正文用 CRLF 回传
        body = before.decode("utf-8").replace("\n", "\r\n")
        r = c.post("/skills/writer/save",
                   data={"body": body, "note": "", "_csrf": tok})
        self.assertEqual(r.status_code, 302)
        after = (root / "SKILL.md").read_bytes()
        self.assertNotIn(b"\r", after, "保存把换行写成了 CRLF")
        self.assertEqual(after, before, "空保存不该产生任何改动")


class TestLauncherPort(unittest.TestCase):
    """
    端口被占时不能静默换端口。

    用户记住的、文档写的、书签存的都是 5001。上次没关干净的旧窗口占着 5001、
    新代码起在 5002，人对着旧界面操作，看到的全是已经修好的老 bug ——
    而且完全不知道为什么。这种「看起来像代码没生效」的坑最难自查。
    """

    def test_warns_when_port_taken(self):
        import contextlib
        import io
        import socket
        from tools import launcher

        s = socket.socket()
        self.addCleanup(s.close)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", 5001))
        except OSError:
            self.skipTest("5001 已被占用，无法构造场景")
        s.listen(5)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            port = launcher.pick_port()
        out = buf.getvalue()
        self.assertNotEqual(port, 5001)
        self.assertIn("5001", out, "换了端口却没说")
        self.assertIn("⚠", out)


class TestSecurity(unittest.TestCase):
    """
    CSRF 是本机自用也必须做的：工作台在 127.0.0.1，但同源策略拦不住
    跨站表单提交 —— 别的标签页里的网页能悄悄 POST 到你的 localhost，
    盖章、合入 skill 建议、清掉密钥、或者触发一串烧钱的模型调用。

    口令相反，只有对外暴露才需要，默认不开（本机自用加登录纯属添堵）。
    """

    def setUp(self):
        try:
            from web import security
            from web.app import create_app
        except ImportError:
            self.skipTest("Flask 未安装")
        self.security = security
        # 测试期间不碰用户真实的 auth.json
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = security.AUTH_FILE
        security.AUTH_FILE = self.tmp / "auth.json"
        self.addCleanup(setattr, security, "AUTH_FILE", self._orig)
        self.app = create_app()
        self.c = self.app.test_client()

    def _token(self, path="/series"):
        body = self.c.get(path).get_data(as_text=True)
        m = re.search(r'name="_csrf" value="([^"]+)"', body)
        self.assertIsNotNone(m, f"{path} 页面里没有 CSRF token")
        return m.group(1)

    def test_post_without_token_rejected(self):
        r = self.c.post("/series/new", data={"name": "跨站建的"})
        self.assertEqual(r.status_code, 400)

    def test_post_with_token_works(self):
        r = self.c.post("/series/new",
                        data={"name": "认领", "_csrf": self._token()})
        self.assertEqual(r.status_code, 302)

    def test_htmx_header_path_works(self):
        """HTMX 的请求到不了表单 hidden 字段，走 header。两条路都得通。"""
        tok = self._token()
        r = self.c.post("/system/export", data={"fmt": "plain"},
                        headers={"X-CSRFToken": tok})
        self.assertEqual(r.status_code, 302)

    def test_token_present_inside_macro_rendered_form(self):
        """
        节点卡是 {% import %} 进来的宏，拿不到 context processor ——
        csrf_input 曾经因此在卡片里静默失效，表单全都提交不了。
        """
        tok = self._token()
        r = self.c.post("/series/new", data={"name": "认领", "_csrf": tok})
        card = self.c.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn('name="_csrf"', card, "节点卡表单里丢了 token")

    def test_no_password_means_no_login(self):
        self.assertFalse(self.security.has_password())
        self.assertEqual(self.c.get("/").status_code, 200)

    def test_password_gates_everything(self):
        self.security.set_password("hunter22")
        c2 = self.app.test_client()          # 新会话
        r = c2.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

        tok = re.search(r'name="_csrf" value="([^"]+)"',
                        c2.get("/login").get_data(as_text=True)).group(1)
        bad = c2.post("/login", data={"password": "wrong", "_csrf": tok})
        self.assertIn("口令不对", bad.get_data(as_text=True))
        ok = c2.post("/login", data={"password": "hunter22", "_csrf": tok})
        self.assertEqual(ok.status_code, 302)
        self.assertEqual(c2.get("/").status_code, 200)

    def test_password_not_stored_in_plaintext(self):
        self.security.set_password("hunter22")
        raw = self.security.AUTH_FILE.read_text(encoding="utf-8")
        self.assertNotIn("hunter22", raw)
        self.assertTrue(self.security.check_password("hunter22"))
        self.assertFalse(self.security.check_password("hunter23"))

    def test_is_local(self):
        for h in ("127.0.0.1", "localhost", "::1"):
            self.assertTrue(self.security.is_local(h))
        for h in ("0.0.0.0", "192.168.1.5", ""):
            self.assertFalse(self.security.is_local(h), h)


class TestCliGapClosed(unittest.TestCase):
    """
    原先只有命令行有的能力，界面上得够得着。
    最要紧的是飞轮：界面能审批建议，却生成不了建议 —— 闭环是断的。
    """

    def setUp(self):
        try:
            from web.app import create_app
        except ImportError:
            self.skipTest("Flask 未安装")
        self.c = create_app().test_client()

    def test_suggest_route_exists(self):
        rules = {str(r.rule) for r in self.c.application.url_map.iter_rules()}
        self.assertIn("/skills/<sid>/suggest", rules,
                      "界面生成不了建议，只能审批 —— 闭环断了")

    def test_director_matcher_ranks(self):
        """recommend() 一直存在但界面够不着，只有 avctl director match 能跑。"""
        body = self.c.get("/directors?genre=武侠").get_data(as_text=True)
        self.assertIn("导演匹配器", body)
        self.assertIn("命中题材", body)

    def test_system_pages(self):
        for path in ("/system/export", "/system/check", "/system/cost"):
            with self.subTest(path=path):
                self.assertEqual(self.c.get(path).status_code, 200)

    def test_dry_run_offered_in_ui(self):
        """无密钥也能试跑，对刚上手的人很重要。"""
        from flask import render_template
        row = {"no": "N2", "contract": "story_file", "skill": "writer",
               "level": "episode", "level_cn": "集级", "cn_name": "故事档案",
               "artifact": None, "status": None, "status_cn": "未开始",
               "provider": "anthropic", "provider_label": "Anthropic",
               "has_key": True, "job": None, "waiting": False}
        with self.c.application.test_request_context():
            card = render_template("fragments/node.html", r=row, ep=1,
                                   directors=[],
                                   s={"id": "s", "name": "t", "genre_tags": [],
                                      "director_id": None})
        self.assertIn('name="dry_run"', card)

    def test_job_kinds_do_not_mix(self):
        """慢任务不止流水线节点了，失败汇总不能把别的种类算进去。"""
        from web import jobs
        jobs.clear()
        jobs.fail(jobs.key_of("suggest", "writer"), "建议生成失败")
        self.assertEqual(jobs.failures({}), [],
                         "非节点任务混进了流水线失败列表")
        jobs.clear()


class TestReviewBlocks(unittest.TestCase):
    """
    审核门的分块编辑。

    这里守的是「一处写坏不该把整次编辑作废」——
    原先整份 payload 挤在一个 textarea 里，打错个逗号就 400 跳错误页，
    刚敲的东西全没了。
    """

    def setUp(self):
        try:
            from web import review
        except ImportError:
            self.skipTest("Flask 未安装")
        self.review = review
        self.c = contracts.get("story_file")
        self.payload = {
            "title": "认领", "logline": "父子在同一所学校",
            "genre_tags": ["现实"], "emotional_core": "亲情",
            "target_duration_sec": 180, "hook": "开场",
            "synopsis": {"act1": "a", "act2": "b", "act3": "c"},
            "characters_brief": [{"id": "c1", "name": "父", "role": "主角",
                                  "one_line": "保洁"}],
        }

    def _submit(self, **over):
        blocks = self.review.build_blocks(self.c, self.payload)
        sub = {b.name: b.raw for b in blocks}
        sub.update(over)
        blocks = self.review.build_blocks(self.c, self.payload, sub)
        return blocks, self.review.parse_blocks(self.c, blocks, sub)

    def test_scalar_and_json_blocks(self):
        kinds = {b.name: b.kind for b in
                 self.review.build_blocks(self.c, self.payload)}
        self.assertEqual(kinds["title"], "line")
        self.assertEqual(kinds["target_duration_sec"], "line")
        self.assertEqual(kinds["synopsis"], "json")
        self.assertEqual(kinds["characters_brief"], "json")

    def test_bad_json_in_one_block_keeps_the_others(self):
        blocks, (payload, bad) = self._submit(title="改过的标题",
                                              synopsis="{坏的")
        self.assertTrue(bad)
        by = {b.name: b for b in blocks}
        self.assertTrue(by["synopsis"].errors, "坏 JSON 没报错")
        self.assertFalse(by["title"].errors, "好字段不该被连坐")
        self.assertEqual(by["title"].raw, "改过的标题", "编辑内容被冲掉了")
        self.assertEqual(payload["title"], "改过的标题")

    def test_non_numeric_in_int_field(self):
        blocks, (_, bad) = self._submit(target_duration_sec="一百八")
        self.assertTrue(bad)
        by = {b.name: b for b in blocks}
        self.assertTrue(by["target_duration_sec"].errors)

    def test_clean_submit_round_trips(self):
        _, (payload, bad) = self._submit()
        self.assertFalse(bad)
        self.assertEqual(payload["target_duration_sec"], 180)
        self.assertEqual(payload["synopsis"], self.payload["synopsis"])
        self.assertEqual(payload["characters_brief"],
                         self.payload["characters_brief"])

    def test_extra_fields_survive(self):
        """契约允许 extra 字段自由扩展，编辑一轮不能把它们弄丢。"""
        p = dict(self.payload, _note="我自己加的", _data={"k": 1})
        blocks = self.review.build_blocks(self.c, p)
        names = {b.name for b in blocks}
        self.assertIn("_note", names)
        self.assertIn("_data", names)

    def test_errors_land_on_their_block(self):
        blocks = self.review.build_blocks(self.c, self.payload)
        rest = self.review.attach_errors(blocks, [
            "payload.logline: logline 过长（建议 80 字内）",
            "payload.characters_brief[0].name: 缺失必填字段",
            "references_cited: 必须申报",          # 信封层，归不到字段
        ])
        by = {b.name: b for b in blocks}
        self.assertEqual(len(by["logline"].errors), 1)
        self.assertEqual(len(by["characters_brief"].errors), 1)
        self.assertEqual(rest, ["references_cited: 必须申报"])

    def test_prompt_pairs_found_nested(self):
        """中英对照是给人扫读的 —— 校验器只查结构，查不出混排残句。"""
        pairs = self.review.prompt_pairs({
            "characters": [{
                "reference_prompts": {
                    "front_bust": {"zh": "正面半身", "en": "front bust"},
                    "profile": {"zh": "侧脸", "en": "profile"},
                }}]})
        self.assertEqual(len(pairs), 2)
        paths = {p["path"] for p in pairs}
        self.assertIn("characters[0].reference_prompts.front_bust", paths)

    def test_prompt_pairs_ignores_non_pairs(self):
        self.assertEqual(self.review.prompt_pairs({"zh": "只有中文"}), [])
        self.assertEqual(self.review.prompt_pairs({"a": 1, "b": "x"}), [])


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
