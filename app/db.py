"""SQLite 连接、建表与事务。

写事务一律使用 BEGIN IMMEDIATE：进入事务即拿写锁，配合短事务，
把“检查状态 -> 写入”放入同一个串行化临界区，保证并发同意/撤销
只能有一个已提交结果。
"""
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from .config import settings

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    key_hash    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY,
    owner_id     INTEGER NOT NULL REFERENCES users(id),
    body         TEXT NOT NULL,
    digest       TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('sealed', 'opened', 'purged')),
    opened_at    TEXT,
    purge_at     TEXT,                       -- 可空：计划的到期清除时刻（UTC，可排序）
    purged_at    TEXT,                       -- 可空：清除生效时刻（= 约定的 purge_at）
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_witnesses (
    id           INTEGER PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    position     INTEGER NOT NULL,
    UNIQUE(document_id, user_id)
);

CREATE TABLE IF NOT EXISTS open_requests (
    id               INTEGER PRIMARY KEY,
    document_id      INTEGER NOT NULL REFERENCES documents(id),
    reason           TEXT NOT NULL,
    deadline         TEXT NOT NULL,            -- UTC，固定微秒精度，可直接按字典序比较
    status           TEXT NOT NULL CHECK (status IN ('pending', 'waiting', 'opened', 'revoked', 'expired', 'invalidated')),
    created_at       TEXT NOT NULL,
    resolved_at      TEXT,
    earliest_open_at TEXT                      -- 可空：最早开封时刻；两票到齐但未到该时刻时状态为 waiting
);
CREATE INDEX IF NOT EXISTS idx_open_requests_document ON open_requests(document_id);

-- 封存人交接邀请：每份资料至多一份 pending（部分唯一索引兜底并发），
-- 逾期/撤回/确认/资料开封后均离开 pending，封存人可重新邀请。
CREATE TABLE IF NOT EXISTS handover_invitations (
    id               INTEGER PRIMARY KEY,
    document_id      INTEGER NOT NULL REFERENCES documents(id),
    from_user_id     INTEGER NOT NULL REFERENCES users(id),   -- 发起时的封存人
    to_user_id       INTEGER NOT NULL REFERENCES users(id),   -- 受让人
    confirm_deadline TEXT NOT NULL,            -- 带时区确认截止时刻（UTC 存储，字符串可排序）
    status           TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'revoked', 'expired')),
    created_at       TEXT NOT NULL,
    resolved_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_handover_one_pending
    ON handover_invitations(document_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_handover_document ON handover_invitations(document_id);

CREATE TABLE IF NOT EXISTS approvals (
    id           INTEGER PRIMARY KEY,
    request_id   INTEGER NOT NULL REFERENCES open_requests(id) ON DELETE CASCADE,
    witness_id   INTEGER NOT NULL REFERENCES users(id),
    bound_digest TEXT NOT NULL,            -- 同意时绑定的原摘要
    created_at   TEXT NOT NULL,
    UNIQUE(request_id, witness_id)         -- 同一人对同一请求重试只计一次
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,       -- 自增序号即操作的全局顺序
    at          TEXT NOT NULL,
    actor_id    INTEGER REFERENCES users(id),
    action      TEXT NOT NULL,
    document_id INTEGER,
    request_id  INTEGER,
    detail      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_document ON audit_log(document_id);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    # check_same_thread=False：FastAPI 的 sync 依赖/端点可能被调度到不同的
    # 线程池工作线程；安全性由 BEGIN IMMEDIATE 短事务 + busy_timeout 保证。
    conn = sqlite3.connect(
        db_path or settings.db_path,
        timeout=30.0,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


# 旧库迁移（幂等）：补 earliest_open_at 列，并在 CHECK 约束不含 waiting/invalidated 时重建表
_WAITING_CHECK = re.compile(r"CHECK\s*\(\s*status\s+IN\s*\(([^)]*)\)", re.IGNORECASE)
_MIGRATION_REBUILD = """
CREATE TABLE open_requests_new (
    id               INTEGER PRIMARY KEY,
    document_id      INTEGER NOT NULL REFERENCES documents(id),
    reason           TEXT NOT NULL,
    deadline         TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('pending', 'waiting', 'opened', 'revoked', 'expired', 'invalidated')),
    created_at       TEXT NOT NULL,
    resolved_at      TEXT,
    earliest_open_at TEXT
);
INSERT INTO open_requests_new (id, document_id, reason, deadline, status, created_at, resolved_at, earliest_open_at)
SELECT id, document_id, reason, deadline, status, created_at, resolved_at, earliest_open_at FROM open_requests;
DROP TABLE open_requests;
ALTER TABLE open_requests_new RENAME TO open_requests;
CREATE INDEX IF NOT EXISTS idx_open_requests_document ON open_requests(document_id);
"""

# 旧库迁移（幂等）：documents 的 CHECK 约束不含 purged 时重建表
# （purge_at / purged_at 两列先由 ALTER TABLE 补齐，重建时随数据一并搬移）
_MIGRATION_REBUILD_DOCUMENTS = """
CREATE TABLE documents_new (
    id           INTEGER PRIMARY KEY,
    owner_id     INTEGER NOT NULL REFERENCES users(id),
    body         TEXT NOT NULL,
    digest       TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('sealed', 'opened', 'purged')),
    opened_at    TEXT,
    purge_at     TEXT,
    purged_at    TEXT,
    created_at   TEXT NOT NULL
);
INSERT INTO documents_new (id, owner_id, body, digest, status, opened_at, purge_at, purged_at, created_at)
SELECT id, owner_id, body, digest, status, opened_at, purge_at, purged_at, created_at FROM documents;
DROP TABLE documents;
ALTER TABLE documents_new RENAME TO documents;
"""


def init_db(db_path: str | None = None) -> None:
    """幂等初始化：创建库表并开启 WAL / 外键（容器初始化时与应用启动时各调用一次也安全）。"""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        # 旧版库迁移：CREATE TABLE IF NOT EXISTS 不会更新既有表结构
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(open_requests)")}
        if "earliest_open_at" not in cols:
            conn.execute("ALTER TABLE open_requests ADD COLUMN earliest_open_at TEXT")
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='open_requests'"
        ).fetchone()["sql"]
        m = _WAITING_CHECK.search(sql)
        allowed = m.group(1).replace('"', "'").lower() if m else ""
        if m is None or "'waiting'" not in allowed or "'invalidated'" not in allowed:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.executescript(_MIGRATION_REBUILD)
            conn.execute("PRAGMA foreign_keys=ON")
        # documents 迁移：补 purge_at / purged_at 列，CHECK 不含 purged 时重建表
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
        if "purge_at" not in cols:
            conn.execute("ALTER TABLE documents ADD COLUMN purge_at TEXT")
        if "purged_at" not in cols:
            conn.execute("ALTER TABLE documents ADD COLUMN purged_at TEXT")
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()["sql"]
        m = _WAITING_CHECK.search(sql)
        allowed = m.group(1).replace('"', "'").lower() if m else ""
        if m is None or "'purged'" not in allowed:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.executescript(_MIGRATION_REBUILD_DOCUMENTS)
            conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即加写锁的写事务；异常回滚并原样抛出。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_str(dt: datetime) -> str:
    """统一格式：带 +00:00 的 UTC 时间，微秒固定 6 位，保证字符串可排序比较。"""
    if dt.tzinfo is None:
        raise ValueError("deadline 必须带时区信息")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")
