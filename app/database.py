"""SQLite 持久化层：项目、字幕条目、镜头、采纳状态、镜头留白审阅结论、
逐条文案修订。

文案修订：cues.edited_text / edited_graphemes 为修订稿（归一化后的可见文本
与其字素数），NULL 表示未修订——当前文案即 COALESCE(edited_text, text)。
导入列（text / graphemes / raw_hash）自导入后永不改写，恢复导入原文 =
把修订列置回 NULL。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

DB_PATH = os.environ.get("DATABASE_PATH", "/data/subtitle_station.db")
# 并发写路径（参数更新 / 采纳）先抢写锁再读状态：等待持锁事务期间
# 阻塞而不是立刻抛 SQLITE_BUSY。可用 BUSY_TIMEOUT_MS 覆盖。
BUSY_TIMEOUT_MS = int(os.environ.get("BUSY_TIMEOUT_MS", "30000"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    cps_limit   REAL NOT NULL,
    min_gap_ms  INTEGER NOT NULL,
    tolerance_ms INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS cues (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    cue_id       TEXT NOT NULL,
    ord          INTEGER NOT NULL,
    start_ms     INTEGER NOT NULL,
    end_ms       INTEGER NOT NULL,
    text         TEXT NOT NULL,
    raw_text     TEXT NOT NULL,
    graphemes    INTEGER NOT NULL,
    raw_hash     TEXT NOT NULL,
    edited_text  TEXT,
    edited_graphemes INTEGER
);
CREATE TABLE IF NOT EXISTS shots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    ord         INTEGER NOT NULL,
    start_ms    INTEGER NOT NULL,
    end_ms      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS adoptions (
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    cue_uid      INTEGER NOT NULL REFERENCES cues(id) ON DELETE CASCADE,
    new_start_ms INTEGER NOT NULL,
    new_end_ms   INTEGER NOT NULL,
    adopted_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (project_id, cue_uid)
);
-- 镜头留白审阅结论。审阅项身份 = (shot_ord, gap_start_ms, gap_end_ms)，
-- 镜头边界随行保存并在读取时一并校验（镜头导入后不可变，仅作防御）。
-- 边界变化的留白匹配不到旧主键 → 回到待审；过期行由写路径同事务清理。
CREATE TABLE IF NOT EXISTS gap_reviews (
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    shot_ord      INTEGER NOT NULL,
    shot_start_ms INTEGER NOT NULL,
    shot_end_ms   INTEGER NOT NULL,
    gap_start_ms  INTEGER NOT NULL,
    gap_end_ms    INTEGER NOT NULL,
    decision      TEXT NOT NULL CHECK (decision IN ('intentional','needs_subtitle')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (project_id, shot_ord, gap_start_ms, gap_end_ms)
);
"""


def _connect() -> sqlite3.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # isolation_level=None：事务边界完全由本模块显式控制（见 get_conn），
    # 不依赖 pysqlite 在第一条 DML 前隐式发 BEGIN——那会把读/求解留在
    # 事务之外，并发写仍能按旧快照落库。
    conn = sqlite3.connect(
        DB_PATH, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000.0
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn(write: bool = False) -> Iterator[sqlite3.Connection]:
    """提供一个事务边界明确的连接。

    write=False：`BEGIN` 延迟事务，在一致快照上只读（项目详情 / 导出 /
        列表），事务期间多次 SELECT 与随后的 build_analysis 不会被并发
        写入撕开。
    write=True：`BEGIN IMMEDIATE` 立刻取得 RESERVED 写锁后再读取任何
        项目参数、字幕、镜头与采纳状态。两条写路径（PUT settings、
        POST/DELETE adopt）由此串行：后到者阻塞等待先提交者，锁内读到
        的必为最新已提交状态，参数读取 / 提案校验 / 采纳核验 / 落库
        全部基于同一事务视图；任一步异常整体回滚，不留半更新。
    """
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        # 旧库迁移：逐条文案修订列（已存在的库按新 schema 不会重建表）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(cues)")}
        if "edited_text" not in cols:
            conn.execute("ALTER TABLE cues ADD COLUMN edited_text TEXT")
        if "edited_graphemes" not in cols:
            conn.execute("ALTER TABLE cues ADD COLUMN edited_graphemes INTEGER")
    finally:
        conn.close()


def create_project(
    conn: sqlite3.Connection,
    name: str,
    cps_limit: float,
    min_gap_ms: int,
    tolerance_ms: int,
    cues: list[dict[str, Any]],
    shots: list[dict[str, Any]],
) -> int:
    cur = conn.execute(
        "INSERT INTO projects (name, cps_limit, min_gap_ms, tolerance_ms) VALUES (?,?,?,?)",
        (name, cps_limit, min_gap_ms, tolerance_ms),
    )
    pid = cur.lastrowid
    conn.executemany(
        "INSERT INTO cues (project_id, cue_id, ord, start_ms, end_ms, text, raw_text,"
        " graphemes, raw_hash) VALUES (?,?,?,?,?,?,?,?,?)",
        [(pid, c["cue_id"], c["ord"], c["start_ms"], c["end_ms"], c["text"],
          c["raw_text"], c["graphemes"], c["raw_hash"]) for c in cues],
    )
    conn.executemany(
        "INSERT INTO shots (project_id, ord, start_ms, end_ms) VALUES (?,?,?,?)",
        [(pid, s["ord"], s["start_ms"], s["end_ms"]) for s in shots],
    )
    return pid


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM cues WHERE project_id=p.id) AS cue_count,"
        " (SELECT COUNT(*) FROM shots WHERE project_id=p.id) AS shot_count,"
        " (SELECT COUNT(*) FROM adoptions WHERE project_id=p.id) AS adopted_count"
        " FROM projects p ORDER BY p.id DESC"
    ))


def get_project(conn: sqlite3.Connection, pid: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()


def get_cues(conn: sqlite3.Connection, pid: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT c.*, a.new_start_ms, a.new_end_ms FROM cues c"
        " LEFT JOIN adoptions a ON a.cue_uid=c.id AND a.project_id=c.project_id"
        " WHERE c.project_id=? ORDER BY c.ord",
        (pid,),
    ))


def get_shots(conn: sqlite3.Connection, pid: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM shots WHERE project_id=? ORDER BY ord", (pid,)
    ))


def update_settings(
    conn: sqlite3.Connection, pid: int,
    cps_limit: float, min_gap_ms: int, tolerance_ms: int,
) -> None:
    conn.execute(
        "UPDATE projects SET cps_limit=?, min_gap_ms=?, tolerance_ms=? WHERE id=?",
        (cps_limit, min_gap_ms, tolerance_ms, pid),
    )


def upsert_adoption(
    conn: sqlite3.Connection, pid: int, cue_uid: int,
    new_start_ms: int, new_end_ms: int,
) -> None:
    conn.execute(
        "INSERT INTO adoptions (project_id, cue_uid, new_start_ms, new_end_ms)"
        " VALUES (?,?,?,?) ON CONFLICT(project_id, cue_uid) DO UPDATE SET"
        " new_start_ms=excluded.new_start_ms, new_end_ms=excluded.new_end_ms,"
        " adopted_at=datetime('now')",
        (pid, cue_uid, new_start_ms, new_end_ms),
    )


def delete_adoption(conn: sqlite3.Connection, pid: int, cue_uid: int) -> None:
    conn.execute(
        "DELETE FROM adoptions WHERE project_id=? AND cue_uid=?", (pid, cue_uid)
    )


# ---------- 逐条文案修订 ----------

def get_cue(conn: sqlite3.Connection, pid: int,
            cue_uid: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM cues WHERE project_id=? AND id=?", (pid, cue_uid)
    ).fetchone()


def current_text(row: sqlite3.Row) -> str:
    """一条 cue 的当前文案：已修订取修订稿，否则取导入时的可见文本。"""
    return row["edited_text"] if row["edited_text"] is not None else row["text"]


def current_graphemes(row: sqlite3.Row) -> int:
    """当前文案的字素数（CPS 与详情展示用）。"""
    return (row["edited_graphemes"] if row["edited_graphemes"] is not None
            else row["graphemes"])


def update_cue_text(
    conn: sqlite3.Connection, pid: int, cue_uid: int,
    edited_text: str, edited_graphemes: int,
) -> None:
    """落一条修订稿（已按导入同一套规则归一化）。导入列不改写。"""
    conn.execute(
        "UPDATE cues SET edited_text=?, edited_graphemes=?"
        " WHERE project_id=? AND id=?",
        (edited_text, edited_graphemes, pid, cue_uid),
    )


def clear_cue_text_edit(conn: sqlite3.Connection, pid: int, cue_uid: int) -> None:
    """恢复导入原文：清除修订稿，当前文案回落到导入列。"""
    conn.execute(
        "UPDATE cues SET edited_text=NULL, edited_graphemes=NULL"
        " WHERE project_id=? AND id=?",
        (pid, cue_uid),
    )


# ---------- 镜头留白审阅结论 ----------

def get_gap_reviews(conn: sqlite3.Connection, pid: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM gap_reviews WHERE project_id=?"
        " ORDER BY shot_ord, gap_start_ms",
        (pid,),
    ))


def upsert_gap_review(
    conn: sqlite3.Connection, pid: int, shot_ord: int,
    shot_start_ms: int, shot_end_ms: int,
    gap_start_ms: int, gap_end_ms: int, decision: str,
) -> None:
    conn.execute(
        "INSERT INTO gap_reviews (project_id, shot_ord, shot_start_ms, shot_end_ms,"
        " gap_start_ms, gap_end_ms, decision) VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(project_id, shot_ord, gap_start_ms, gap_end_ms) DO UPDATE SET"
        " shot_start_ms=excluded.shot_start_ms, shot_end_ms=excluded.shot_end_ms,"
        " decision=excluded.decision, updated_at=datetime('now')",
        (pid, shot_ord, shot_start_ms, shot_end_ms,
         gap_start_ms, gap_end_ms, decision),
    )


def delete_gap_review(
    conn: sqlite3.Connection, pid: int, shot_ord: int,
    gap_start_ms: int, gap_end_ms: int,
) -> None:
    conn.execute(
        "DELETE FROM gap_reviews WHERE project_id=? AND shot_ord=?"
        " AND gap_start_ms=? AND gap_end_ms=?",
        (pid, shot_ord, gap_start_ms, gap_end_ms),
    )


def delete_stale_gap_reviews(
    conn: sqlite3.Connection, pid: int,
    valid_keys: set[tuple[int, int, int]],
) -> int:
    """删除已不匹配任何当前留白项的过期结论（边界变化 → 回到待审）。

    valid_keys 为当前留白项的 (shot_ord, gap_start_ms, gap_end_ms) 集合；
    须在写事务内、按最新状态重算留白之后调用。
    """
    removed = 0
    for r in get_gap_reviews(conn, pid):
        key = (r["shot_ord"], r["gap_start_ms"], r["gap_end_ms"])
        if key not in valid_keys:
            delete_gap_review(conn, pid, *key)
            removed += 1
    return removed


def delete_project(conn: sqlite3.Connection, pid: int) -> None:
    conn.execute("DELETE FROM projects WHERE id=?", (pid,))
