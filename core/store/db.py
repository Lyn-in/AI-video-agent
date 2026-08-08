"""
元数据层：SQLite。

存什么：项目结构、节点状态、产物索引、血缘、skill 版本、调用日志。
不存什么：产物正文。产物必须是文件系统里人可读可手改的 Markdown/JSON，
         塞进数据库就废了 —— 审核门要的就是「直接改文件」这个能力。
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 账号：关联一份频道圣经
CREATE TABLE IF NOT EXISTS accounts (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    show_bible  TEXT,                -- 频道圣经文件相对路径
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

-- 合集/季
CREATE TABLE IF NOT EXISTS collections (
    id          TEXT PRIMARY KEY,
    account_id  TEXT REFERENCES accounts(id),
    name        TEXT NOT NULL,
    season_no   INTEGER,
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

-- 剧集：角色板/场景板/导演都挂在这一层，锁死跨集一致性
CREATE TABLE IF NOT EXISTS series (
    id            TEXT PRIMARY KEY,
    collection_id TEXT REFERENCES collections(id),
    name          TEXT NOT NULL,
    director_id   TEXT,              -- 选定的导演 skill id
    genre_tags    TEXT,              -- JSON array
    created_at    TEXT DEFAULT (datetime('now','localtime'))
);

-- 集
CREATE TABLE IF NOT EXISTS episodes (
    id          TEXT PRIMARY KEY,
    series_id   TEXT REFERENCES series(id),
    episode_no  INTEGER NOT NULL,
    title       TEXT,
    status      TEXT DEFAULT 'draft',
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

-- 产物索引（正文在文件系统）
CREATE TABLE IF NOT EXISTS artifacts (
    id            TEXT PRIMARY KEY,
    contract      TEXT NOT NULL,
    scope_type    TEXT NOT NULL,     -- series | episode
    scope_id      TEXT NOT NULL,
    path          TEXT NOT NULL,     -- 相对 projects/ 的路径
    status        TEXT NOT NULL,     -- generating|pending_review|revised|approved
    skill_id      TEXT,
    skill_version TEXT,
    director_id   TEXT,
    model         TEXT,
    input_hash    TEXT,              -- 断点续跑判定
    created_at    TEXT DEFAULT (datetime('now','localtime')),
    updated_at    TEXT DEFAULT (datetime('now','localtime'))
);

-- 血缘：产物之间的输入依赖
CREATE TABLE IF NOT EXISTS lineage (
    artifact_id TEXT REFERENCES artifacts(id),
    input_id    TEXT,
    PRIMARY KEY (artifact_id, input_id)
);

-- 引用申报：哪条 reference 被哪个产物用了，用途是什么
CREATE TABLE IF NOT EXISTS citations (
    artifact_id    TEXT REFERENCES artifacts(id),
    ref_id         TEXT NOT NULL,
    skill_id       TEXT,
    purpose        TEXT,
    problem_solved TEXT,
    PRIMARY KEY (artifact_id, ref_id)
);

-- reference 健康度：引用次数、引用后大改率
CREATE TABLE IF NOT EXISTS ref_health (
    skill_id     TEXT NOT NULL,
    ref_id       TEXT NOT NULL,
    cited_count  INTEGER DEFAULT 0,
    revised_count INTEGER DEFAULT 0,
    PRIMARY KEY (skill_id, ref_id)
);

-- skill 版本登记
CREATE TABLE IF NOT EXISTS skill_versions (
    skill_id   TEXT NOT NULL,
    version    TEXT NOT NULL,
    path       TEXT,
    note       TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (skill_id, version, created_at)
);

-- 库自身的元信息。目前只放 schema_version，供迁移判定。
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_artifacts_scope
    ON artifacts(scope_type, scope_id, contract);
"""

# 当前 schema 版本。每加一次迁移 +1，并在 MIGRATIONS 里补一条。
SCHEMA_VERSION = 1


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def conn(self):
        # WAL：读写不互相阻塞。busy_timeout：并发写入时等待而非直接报错。
        # 工作台和命令行会同时操作同一个库，没有这两项迟早撞 database is locked。
        c = sqlite3.connect(self.db_path, timeout=15)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=15000")
        c.execute("PRAGMA foreign_keys=ON")
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def init(self):
        with self.conn() as c:
            c.executescript(SCHEMA)
        self.migrate()

    # ---------- 迁移 ----------
    def migrate(self):
        """
        幂等迁移。放在 init() 里自动跑，而不是做成一条要用户记住的命令 ——
        这是个双击启动的单机工具，没人会去读迁移说明。
        """
        with self.conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key='schema_version'"
                            ).fetchone()
            cur = int(row["value"]) if row else 0
            if cur >= SCHEMA_VERSION:
                return
            if cur < 1:
                _migrate_1_episode_scope(c)
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                      ("schema_version", str(SCHEMA_VERSION)))

    # ---------- 项目结构 ----------
    def create_account(self, id_, name, show_bible=None):
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO accounts(id,name,show_bible)"
                      " VALUES(?,?,?)", (id_, name, show_bible))

    def create_collection(self, id_, account_id, name, season_no=1):
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO collections"
                      "(id,account_id,name,season_no) VALUES(?,?,?,?)",
                      (id_, account_id, name, season_no))

    def create_series(self, id_, collection_id, name,
                      director_id=None, genre_tags=None):
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO series"
                      "(id,collection_id,name,director_id,genre_tags)"
                      " VALUES(?,?,?,?,?)",
                      (id_, collection_id, name, director_id,
                       json.dumps(genre_tags or [], ensure_ascii=False)))

    def create_episode(self, id_, series_id, episode_no, title=None):
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO episodes"
                      "(id,series_id,episode_no,title) VALUES(?,?,?,?)",
                      (id_, series_id, episode_no, title))

    # ---------- 产物 ----------
    def register_artifact(self, art: dict, path: str,
                          scope_type: str, scope_id: str, input_hash: str = ""):
        pb = art.get("produced_by", {})
        with self.conn() as c:
            c.execute("""INSERT OR REPLACE INTO artifacts
                (id,contract,scope_type,scope_id,path,status,
                 skill_id,skill_version,director_id,model,input_hash,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
                      (art["artifact_id"], art["contract"], scope_type, scope_id,
                       path, art["status"], pb.get("skill"),
                       pb.get("skill_version"), pb.get("director"),
                       pb.get("model"), input_hash))
            for inp in art.get("lineage", {}).get("inputs", []):
                c.execute("INSERT OR REPLACE INTO lineage VALUES(?,?)",
                          (art["artifact_id"], inp))
            for cit in art.get("references_cited", []):
                c.execute("INSERT OR REPLACE INTO citations VALUES(?,?,?,?,?)",
                          (art["artifact_id"], cit["ref_id"], pb.get("skill"),
                           cit.get("purpose"), cit.get("problem_solved")))
                c.execute("""INSERT INTO ref_health(skill_id,ref_id,cited_count)
                             VALUES(?,?,1)
                             ON CONFLICT(skill_id,ref_id)
                             DO UPDATE SET cited_count=cited_count+1""",
                          (pb.get("skill"), cit["ref_id"]))

    def set_status(self, artifact_id: str, status: str):
        with self.conn() as c:
            c.execute("UPDATE artifacts SET status=?,"
                      " updated_at=datetime('now','localtime') WHERE id=?",
                      (status, artifact_id))

    def latest_artifact(self, scope_type: str, scope_id: str, contract: str):
        with self.conn() as c:
            r = c.execute("""SELECT * FROM artifacts
                             WHERE scope_type=? AND scope_id=? AND contract=?
                             ORDER BY updated_at DESC LIMIT 1""",
                          (scope_type, scope_id, contract)).fetchone()
            return dict(r) if r else None

    def list_artifacts(self, scope_id: str | None = None):
        q = "SELECT * FROM artifacts"
        args = ()
        if scope_id:
            q += " WHERE scope_id=?"
            args = (scope_id,)
        q += " ORDER BY created_at"
        with self.conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    def mark_revised(self, artifact_id: str):
        """审核门里被人工大改过：对应引用的 reference 记一次大改，用于健康度治理。"""
        with self.conn() as c:
            rows = c.execute("SELECT skill_id, ref_id FROM citations"
                             " WHERE artifact_id=?", (artifact_id,)).fetchall()
            for r in rows:
                c.execute("""UPDATE ref_health SET revised_count=revised_count+1
                             WHERE skill_id=? AND ref_id=?""",
                          (r["skill_id"], r["ref_id"]))

    def ref_health(self, skill_id: str):
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM ref_health WHERE skill_id=?"
                " ORDER BY cited_count DESC", (skill_id,)).fetchall()]

    def record_skill_version(self, skill_id, version, path, note=""):
        with self.conn() as c:
            c.execute("INSERT INTO skill_versions(skill_id,version,path,note)"
                      " VALUES(?,?,?,?)", (skill_id, version, str(path), note))


# ---------- 迁移实现 ----------

_EP_IN_PATH = re.compile(r"(?:^|/)ep(\d+)/")


def _migrate_1_episode_scope(c):
    """
    集级产物原先拿剧集 id 当 scope_id，导致同一部剧的第 1 集和第 2 集
    在索引里互相覆盖（文件落在 ep01/ ep02/ 分得开，索引分不开）。
    改为「剧集id-ep集号」，集号从产物路径里回读。

    路径里没有 epNN/ 的行，说明产物当初落在 _series/，
    按 core.pipeline.scope_of() 的口径它就该记成剧集级，一并改正。
    """
    rows = c.execute(
        "SELECT id, scope_id, path FROM artifacts WHERE scope_type='episode'"
    ).fetchall()
    for r in rows:
        m = _EP_IN_PATH.search(r["path"] or "")
        if not m:
            c.execute("UPDATE artifacts SET scope_type='series' WHERE id=?",
                      (r["id"],))
            continue
        ep = int(m.group(1))
        sid = r["scope_id"] or ""
        # 已经是 <series>-ep<N> 形态就跳过，保证重复执行安全。
        if sid.endswith(f"-ep{ep}"):
            continue
        c.execute("UPDATE artifacts SET scope_id=? WHERE id=?",
                  (f"{sid}-ep{ep}", r["id"]))
