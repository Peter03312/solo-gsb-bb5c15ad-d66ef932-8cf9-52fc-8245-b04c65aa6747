"""双人见证资料封存服务。

关键规则：
- 本地账号注册后凭 API Key（X-API-Key）操作，服务端只存密钥哈希；
- 封存后正文不可改；开封前任何查询与错误响应都不返回正文；
- 开封请求需两名不同指定见证人在截止时刻前分别同意，同一人重试只计一次；
- 同意必须绑定本次请求与封存时的摘要（客户端提交摘要，服务端比对）；
- 开封请求可选带时区的 earliest_open_at（最早开封时刻）：
  未填写时两票集齐即开封；填写后两票在该时刻前集齐只进入 waiting（待开封），
  到点自动开封；到点时不足两票的，截止前补齐第二票时立即开封；
- 撤销或到期后旧同意永久失效，不能用于后续请求；
- 已 waiting（未撤销、两票集齐）的请求到点必开封：后台扫描器与每个写事务
  都会先做确定性的“到点开封”判定，即使处理延迟，之后的撤销或到期扫描也无法使其失效；
- 资料未开封时，封存人可替换一名指定见证人：新见证人须已注册、非封存人、
  且不在当前名单（替换后仍是三名不同见证人）；有进行中的开封请求时，
  被替换者对本次请求的同意立即失效、其他同意保留，有效票不足两票则请求
  退回待同意（waiting -> pending），新见证人可凭封存摘要补票；已到最早
  开封时刻且两票已齐的请求先开封并拒绝替换；已过截止时刻的请求按原规则失效；
- 封存人可就未开封资料向另一名本地账号发起交接邀请：受让人须已注册、不是
  封存人本人也不是当前见证人；确认截止时刻带时区；每份资料同时至多一份
  待确认（pending）且未过期邀请（部分唯一索引兜底）；封存人可撤回待确认邀请；
  受让人须在截止前凭本人 X-API-Key 确认，已撤回/逾期/已确认均不可确认，
  逾期或撤回后封存人可重新邀请；
- 确认交接时先结算“到点开封 + 请求到期”：资料若已开封则拒绝交接；否则把
  进行中的开封请求置为 invalidated 并删除其全部旧同意（旧同意永久失效），
  再把封存人身份转移给受让人；见证名单、正文与摘要均不变；原封存人随即
  失去该资料的查看与操作权限，新封存人可重新申请开封；
- 交接邀请的发起/撤回/确认与开封请求的同意/撤销/替换/定时处理共用同一把
  写锁，全部按 BEGIN IMMEDIATE 已提交顺序产生唯一结果；成功交接与请求失效
  均追加审计；后台扫描器同时处理开封请求到期与交接邀请逾期；
- 封存人可为资料安排带时区的未来清除时刻（到期清除正文），到期前可改期或
  取消；交接后清除安排保留并改由新封存人管理，原封存人无权操作；
- 到期时无论扫描器是否准时运行，后续查询与操作都呈现已清除结果：扫描器、
  每个写事务与资料查询都会先做确定性到点清除判定——清空正文（body），
  状态置 purged，保留摘要、参与方与审计元数据，任何接口不再返回正文；
  已开封资料仍按期清除；未开封资料的进行中开封请求失效（旧同意作废）、
  待确认交接邀请失效；清除与最早开封同刻时清除优先，开封更早则先开封；
  已清除资料不能再开封、替换见证人、交接或再安排清除；已到期的改期或取消
  一律拒绝且不泄露正文；
- 所有“检查 + 写入”都在 BEGIN IMMEDIATE 短事务内完成，并发同意/撤销/定时开封/
  到期/替换见证人/交接确认/到期清除只有一个已提交结果；
- 每次操作按顺序追加到 audit_log，SQLite 持久化，重启后状态一致。
"""
import asyncio
import hashlib
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import settings
from .db import connect, init_db, now_utc, utc_str, write_tx
from .schemas import (
    ApproveIn,
    HandoverIn,
    OpenRequestIn,
    PurgeIn,
    RegisterIn,
    ReplaceWitnessIn,
    RevokeIn,
    SealIn,
)

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def add_audit(
    conn: sqlite3.Connection,
    *,
    actor_id: int | None,
    action: str,
    detail: str,
    document_id: int | None = None,
    request_id: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO audit_log (at, actor_id, action, document_id, request_id, detail)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (utc_str(now_utc()), actor_id, action, document_id, request_id, detail),
    )


# ---------------------------------------------------------------------------
# 定时开封 / 过期扫描
#
# 到点开封（waiting -> opened）与到期失效（pending -> expired）共用同一把
# 串行化写锁。waiting 请求到点后只能开封：任何写临界区内都先做“到点开封”
# 判定，因此即便后台处理延迟，之后到达的撤销或到期扫描也无法将其作废。
# ---------------------------------------------------------------------------


def open_request_now(
    tx: sqlite3.Connection,
    *,
    req_id: int,
    doc_id: int,
    now: str,
    actor_id: int | None = None,
    detail: str = "已到最早开封时刻且两名不同指定见证人均已同意，资料自动开封",
) -> None:
    """在已持有的写事务内把资料与请求置为 opened 并追加审计。

    资料一旦开封，其上待确认的封存人交接邀请不再可确认，随开封一并失效。
    """
    tx.execute(
        "UPDATE documents SET status='opened', opened_at=? WHERE id=? AND status='sealed'",
        (now, doc_id),
    )
    tx.execute(
        "UPDATE open_requests SET status='opened', resolved_at=? WHERE id=?",
        (now, req_id),
    )
    add_audit(
        tx,
        actor_id=actor_id,
        action="opened",
        document_id=doc_id,
        request_id=req_id,
        detail=detail,
    )
    invalidate_pending_invitations(
        tx,
        doc_id=doc_id,
        now=now,
        detail="资料已开封，待确认的封存人交接邀请失效",
    )


def invalidate_pending_invitations(
    tx: sqlite3.Connection, *, doc_id: int, now: str, detail: str,
    exclude_id: int | None = None,
) -> int:
    """在已持有的写事务内把该资料全部待确认交接邀请置为 expired 并逐条留痕。

    开封与交接确认会调用本函数（确认时排除正在确认的这一份）；返回失效条数。
    """
    rows = tx.execute(
        "SELECT id FROM handover_invitations"
        " WHERE document_id = ? AND status = 'pending'"
        + (" AND id != ?" if exclude_id is not None else ""),
        (doc_id, exclude_id) if exclude_id is not None else (doc_id,),
    ).fetchall()
    for row in rows:
        tx.execute(
            "UPDATE handover_invitations SET status='expired', resolved_at=? WHERE id=?",
            (now, row["id"]),
        )
        add_audit(
            tx,
            actor_id=None,
            action="handover_invitation_expired",
            document_id=doc_id,
            detail=f"交接邀请 {row['id']}：{detail}",
        )
    return len(rows)


def resolve_due_waiting(tx: sqlite3.Connection, now: str, request_id: int | None = None) -> int:
    """在已持有的写事务内把已到最早开封时刻的 waiting 请求确定性开封。

    所有写路径（同意/撤销/新建请求/后台扫描）进入临界区后都先调用本函数，
    保证“到点必开封”不依赖后台扫描器的及时性。返回本次开封条数。

    清除优先：资料已安排清除时刻且 purge_at <= earliest_open_at（清除更早
    或与最早开封同刻）时，请求不在此处开封，交由 purge_due_documents 直接
    清除（资料 purged、请求 invalidated）；只有 purge_at 严格晚于
    earliest_open_at（开封更早）的请求才在此处先开封。
    """
    # 清除优先条件：d.purge_at IS NULL OR d.purge_at > r.earliest_open_at 才开封
    if request_id is not None:
        rows = tx.execute(
            "SELECT r.id, r.document_id FROM open_requests r"
            " JOIN documents d ON d.id = r.document_id"
            " WHERE r.id = ? AND r.status = 'waiting' AND r.earliest_open_at <= ?"
            " AND (d.purge_at IS NULL OR d.purge_at > r.earliest_open_at)",
            (request_id, now),
        ).fetchall()
    else:
        rows = tx.execute(
            "SELECT r.id, r.document_id FROM open_requests r"
            " JOIN documents d ON d.id = r.document_id"
            " WHERE r.status = 'waiting' AND r.earliest_open_at <= ?"
            " AND (d.purge_at IS NULL OR d.purge_at > r.earliest_open_at)",
            (now,),
        ).fetchall()
    for row in rows:
        open_request_now(tx, req_id=row["id"], doc_id=row["document_id"], now=now)
    return len(rows)


# ---------------------------------------------------------------------------
# 到期清除正文（scheduled purge）
#
# 封存人可为资料安排带时区的未来清除时刻；到期后无论扫描器是否准时运行，
# 后续查询与操作都必须呈现已清除结果：扫描器、每个写事务与资料查询都会先
# 做确定性到点清除判定。清除生效时：清空正文（body 置空）、状态置 purged、
# purged_at 记为约定的清除时刻；摘要、参与方与审计元数据全部保留；未开封
# 资料的进行中开封请求失效（旧同意删除）、待确认交接邀请失效；已开封资料
# 同样按期清除。清除与最早开封同刻时清除优先，开封更早则先开封再清除。
# ---------------------------------------------------------------------------


def purge_document_now(
    tx: sqlite3.Connection,
    *,
    doc_id: int,
    now: str,
    actor_id: int | None = None,
) -> None:
    """在已持有的写事务内清除一份到点资料的正文并追加审计。

    结算顺序：先把“最早开封时刻严格早于清除时刻”的待开封请求补开
    （开封更早则先开封；读路径直接调用本函数时也能得到一致历史），
    再把仍进行中的开封请求失效并删除旧同意、使待确认交接邀请失效，
    最后清空正文、置 purged。清除与最早开封同刻时清除优先（不开封）。
    """
    doc = tx.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    purge_at = doc["purge_at"]

    # 开封时刻严格更早的 waiting 请求先开封（同刻不清除优先 -> 不在这里开）。
    # 写路径中 resolve_due_waiting 已处理过这些请求，此处是读路径/兜底补齐。
    earlier = tx.execute(
        "SELECT id FROM open_requests"
        " WHERE document_id = ? AND status = 'waiting' AND earliest_open_at < ?",
        (doc_id, purge_at),
    ).fetchall()
    for row in earlier:
        open_request_now(tx, req_id=row["id"], doc_id=doc_id, now=now)

    # 仍进行中的开封请求失效（过截止时刻的按原规则 expired），旧同意删除。
    active = tx.execute(
        "SELECT * FROM open_requests"
        " WHERE document_id = ? AND status IN ('pending', 'waiting')"
        " ORDER BY id DESC LIMIT 1",
        (doc_id,),
    ).fetchone()
    if active is not None:
        if active["status"] == "pending" and active["deadline"] <= now:
            tx.execute(
                "UPDATE open_requests SET status='expired', resolved_at=? WHERE id=?",
                (now, active["id"]),
            )
            add_audit(
                tx,
                actor_id=None,
                action="request_expired",
                document_id=doc_id,
                request_id=active["id"],
                detail="清除时刻到达时开封请求已过截止时刻，请求按原规则失效，已有同意作废",
            )
        else:
            tx.execute(
                "UPDATE open_requests SET status='invalidated', resolved_at=? WHERE id=?",
                (now, active["id"]),
            )
            add_audit(
                tx,
                actor_id=None,
                action="request_invalidated",
                document_id=doc_id,
                request_id=active["id"],
                detail="资料已到清除时刻，进行中的开封请求失效，其全部旧同意作废",
            )
        tx.execute("DELETE FROM approvals WHERE request_id = ?", (active["id"],))

    # 待确认的封存人交接邀请随清除失效。
    invalidate_pending_invitations(
        tx,
        doc_id=doc_id,
        now=now,
        detail="资料已到清除时刻，待确认的封存人交接邀请失效",
    )

    # 清空正文、置 purged；purged_at 记为约定的清除时刻，与扫描及时性无关。
    # 摘要、参与方（封存人/见证人）与审计元数据全部保留。
    tx.execute(
        "UPDATE documents SET status='purged', body='', purged_at=?"
        " WHERE id=? AND status IN ('sealed', 'opened')",
        (purge_at, doc_id),
    )
    add_audit(
        tx,
        actor_id=actor_id,
        action="purged",
        document_id=doc_id,
        detail=(
            f"已到清除时刻 {purge_at}，资料正文已清除；"
            "摘要、参与方与审计元数据保留，任何接口不再返回正文"
        ),
    )


def purge_due_documents(
    tx: sqlite3.Connection, now: str, document_id: int | None = None
) -> int:
    """在已持有的写事务内把已到清除时刻的资料确定性清除。返回本次清除份数。

    与到点开封同理：扫描器、每个写事务与资料查询都先做本判定，
    “到期必清除”不依赖后台扫描器的及时性。
    """
    if document_id is not None:
        rows = tx.execute(
            "SELECT id FROM documents"
            " WHERE id = ? AND purge_at IS NOT NULL AND purge_at <= ?"
            " AND status != 'purged'",
            (document_id, now),
        ).fetchall()
    else:
        rows = tx.execute(
            "SELECT id FROM documents"
            " WHERE purge_at IS NOT NULL AND purge_at <= ? AND status != 'purged'",
            (now,),
        ).fetchall()
    for row in rows:
        purge_document_now(tx, doc_id=row["id"], now=now)
    return len(rows)


def settle_due_purges_for_read(db: sqlite3.Connection, document_id: int | None = None) -> None:
    """读路径的确定性到点清除：仅当存在到点未清除的资料时才进入写事务。

    保证“到期后查询也呈现已清除结果”，同时不改变未清除资料的只读行为。
    """
    now = utc_str(now_utc())
    if document_id is not None:
        due = db.execute(
            "SELECT 1 FROM documents"
            " WHERE id = ? AND purge_at IS NOT NULL AND purge_at <= ?"
            " AND status != 'purged'",
            (document_id, now),
        ).fetchone()
    else:
        due = db.execute(
            "SELECT 1 FROM documents"
            " WHERE purge_at IS NOT NULL AND purge_at <= ? AND status != 'purged'"
            " LIMIT 1",
            (now,),
        ).fetchone()
    if due is None:
        return
    with write_tx(db) as tx:
        purge_due_documents(tx, utc_str(now_utc()), document_id)


def expire_due_invitations(tx: sqlite3.Connection, now: str, invitation_id: int | None = None) -> int:
    """在已持有的写事务内把已过确认截止时刻仍 pending 的交接邀请确定性置为 expired。

    与到点开封同理：所有交接写路径进入临界区后先调用本函数，逾期结论不依赖
    后台扫描器的及时性。返回失效条数。
    """
    if invitation_id is not None:
        rows = tx.execute(
            "SELECT id, document_id FROM handover_invitations"
            " WHERE id = ? AND status = 'pending' AND confirm_deadline <= ?",
            (invitation_id, now),
        ).fetchall()
    else:
        rows = tx.execute(
            "SELECT id, document_id FROM handover_invitations"
            " WHERE status = 'pending' AND confirm_deadline <= ?",
            (now,),
        ).fetchall()
    for row in rows:
        tx.execute(
            "UPDATE handover_invitations SET status='expired', resolved_at=? WHERE id=?",
            (now, row["id"]),
        )
        add_audit(
            tx,
            actor_id=None,
            action="handover_invitation_expired",
            document_id=row["document_id"],
            detail=f"交接邀请 {row['id']} 已过确认截止时刻且未被确认，邀请失效，可重新邀请",
        )
    return len(rows)


class ExpirySweeper:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._stop = asyncio.Event()  # 允许同一进程内停止后再次启动（测试/多服务）
        await asyncio.to_thread(init_db)
        await asyncio.to_thread(self.sweep_once)  # 重启后先恢复一次
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(settings.expiry_interval_seconds)
                await asyncio.to_thread(self.sweep_once)
            except asyncio.CancelledError:
                break

    @staticmethod
    def sweep_once() -> int:
        """一轮定时处理：先把到点的 waiting 请求开封（清除优先的不开），
        再把到清除时刻的资料清除正文，然后把过截止时刻仍
        pending（票数不足）的请求置为 expired，最后把过确认截止时刻仍
        pending 的交接邀请置为 expired；返回失效（expired）请求条数。"""
        conn = connect()
        try:
            with write_tx(conn) as tx:
                now = utc_str(now_utc())
                # 第一步：到点开封。waiting 必然满足 earliest_open_at < deadline，
                # 到点即生效，永远不会走到下面的过期分支。
                resolve_due_waiting(tx, now)
                # 第二步：到清除时刻的资料清空正文（purged）；其进行中请求、
                # 旧同意与待确认交接邀请在 purge_document_now 内一并结算。
                purge_due_documents(tx, now)
                # 第三步：到截止时刻仍未集齐两票（pending）的请求失效。
                rows = tx.execute(
                    "SELECT id, document_id FROM open_requests"
                    " WHERE status = 'pending' AND deadline <= ?",
                    (now,),
                ).fetchall()
                for row in rows:
                    tx.execute(
                        "UPDATE open_requests SET status='expired', resolved_at=? WHERE id=?",
                        (now, row["id"]),
                    )
                    add_audit(
                        tx,
                        actor_id=None,
                        action="request_expired",
                        document_id=row["document_id"],
                        request_id=row["id"],
                        detail="开封请求到期前未取得两名不同见证人同意，请求失效，已有同意作废",
                    )
                # 第四步：过确认截止时刻仍待确认的交接邀请失效（逾期后可重新邀请）。
                expire_due_invitations(tx, now)
                return len(rows)
        finally:
            conn.close()


sweeper = ExpirySweeper()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await sweeper.start()
    try:
        yield
    finally:
        await sweeper.stop()


app = FastAPI(
    title="双人见证资料封存服务",
    version="1.5.0",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def _validation_exc_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 错误体只返回字段路径与类型信息，绝不回显提交内容（否则会泄露封存正文）。"""
    safe_errors = [
        {
            "loc": list(e.get("loc", [])),
            "msg": e.get("msg", "参数错误"),
            "type": e.get("type", "value_error"),
        }
        for e in exc.errors()
    ]
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        content={"error": "请求参数校验失败", "details": safe_errors})


# ---------------------------------------------------------------------------
# 依赖：数据库连接 + API Key 认证（无效凭据一律 401，不写任何记录）
# ---------------------------------------------------------------------------


def get_db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def current_user(
    x_api_key: str | None = Header(default=None),
    db: sqlite3.Connection = Depends(get_db),
) -> sqlite3.Row:
    if not x_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 API Key")
    row = db.execute(
        "SELECT * FROM users WHERE key_hash = ?", (sha256_hex(x_api_key),)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="凭据无效")
    return row


def err(status_code: int, message: str) -> HTTPException:
    """统一错误体，只含一句不含正文/摘要的提示。"""
    return HTTPException(status_code=status_code, detail=message)


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------


@app.post("/register", status_code=201)
def register(payload: RegisterIn, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """本地注册：用户名唯一；只在本次响应中返回 API Key，之后无法找回。"""
    api_key = secrets.token_hex(32)
    with write_tx(db) as tx:
        exists = tx.execute("SELECT 1 FROM users WHERE username = ?", (payload.username,)).fetchone()
        if exists:
            raise err(409, "用户名已存在")
        cur = tx.execute(
            "INSERT INTO users (username, key_hash, created_at) VALUES (?, ?, ?)",
            (payload.username, sha256_hex(api_key), utc_str(now_utc())),
        )
        add_audit(tx, actor_id=cur.lastrowid, action="register", detail=f"用户 {payload.username} 注册")
        user_id = cur.lastrowid
    return {"user_id": user_id, "username": payload.username, "api_key": api_key}


@app.get("/me")
def me(user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    return {"user_id": user["id"], "username": user["username"]}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 资料封存
# ---------------------------------------------------------------------------


@app.post("/documents", status_code=201)
def seal_document(
    payload: SealIn,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    if len(set(payload.witnesses)) != 3:
        raise err(400, "必须指定三名互不相同的见证人")
    if user["username"] in payload.witnesses:
        raise err(400, "封存人不能同时担任见证人")

    with write_tx(db) as tx:
        placeholders = ",".join("?" for _ in payload.witnesses)
        witness_rows = tx.execute(
            f"SELECT id, username FROM users WHERE username IN ({placeholders})",
            payload.witnesses,
        ).fetchall()
        found = {row["username"] for row in witness_rows}
        missing = [w for w in payload.witnesses if w not in found]
        if missing:
            raise err(400, f"见证人尚未注册: {', '.join(missing)}")
        id_by_name = {row["username"]: row["id"] for row in witness_rows}

        digest = sha256_hex(payload.body)
        now = utc_str(now_utc())
        cur = tx.execute(
            "INSERT INTO documents (owner_id, body, digest, status, created_at)"
            " VALUES (?, ?, ?, 'sealed', ?)",
            (user["id"], payload.body, digest, now),
        )
        doc_id = cur.lastrowid
        for position, witness_name in enumerate(payload.witnesses):
            tx.execute(
                "INSERT INTO document_witnesses (document_id, user_id, position) VALUES (?, ?, ?)",
                (doc_id, id_by_name[witness_name], position),
            )
        add_audit(
            tx,
            actor_id=user["id"],
            action="seal",
            document_id=doc_id,
            detail=f"封存资料，摘要 {digest[:12]}…；见证人: {', '.join(payload.witnesses)}",
        )

    # 注意：响应不含正文
    return {"document_id": doc_id, "status": "sealed", "digest": digest, "witnesses": payload.witnesses}


def load_doc_for_participant(
    db: sqlite3.Connection, doc_id: int, user: sqlite3.Row
) -> sqlite3.Row:
    """取资料并校验参与方（封存人或指定见证人）；否则统一 403，不区分是否存在。"""
    doc = db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if doc is None:
        raise err(403, "无权访问该资料或资料不存在")
    if doc["owner_id"] == user["id"]:
        return doc
    is_witness = db.execute(
        "SELECT 1 FROM document_witnesses WHERE document_id = ? AND user_id = ?",
        (doc_id, user["id"]),
    ).fetchone()
    if is_witness is None:
        raise err(403, "无权访问该资料或资料不存在")
    return doc


@app.get("/documents")
def list_documents(
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    """仅列出本人封存或本人见证的资料（只有元数据，永不返回正文）。

    到清除时刻的资料在查询前被确定性清除（purged），列表呈现已清除状态。
    """
    settle_due_purges_for_read(db)
    rows = db.execute(
        """
        SELECT DISTINCT d.id, d.status, d.created_at, d.opened_at, d.purge_at, d.purged_at
        FROM documents d
        LEFT JOIN document_witnesses w ON w.document_id = d.id
        WHERE d.owner_id = ? OR w.user_id = ?
        ORDER BY d.id
        """,
        (user["id"], user["id"]),
    ).fetchall()
    return {"documents": [dict(row) for row in rows]}


def _open_request_meta(req: sqlite3.Row) -> dict[str, Any]:
    """开封请求的元数据视图（不含正文、不含投票人）。"""
    return {
        "request_id": req["id"],
        "reason": req["reason"],
        "deadline": req["deadline"],
        "earliest_open_at": req["earliest_open_at"],
        "status": req["status"],
        "created_at": req["created_at"],
        "resolved_at": req["resolved_at"],
    }


@app.get("/documents/{doc_id}")
def get_document(
    doc_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    """查询资料。开封前不含正文；开封后返回正文、摘要、理由与见证记录。

    已到清除时刻的资料先被确定性清除：返回 purged 状态与清除时刻，
    保留摘要、参与方与审计元数据，正文永不返回。除此之外只读，不改状态。
    """
    settle_due_purges_for_read(db, doc_id)
    doc = load_doc_for_participant(db, doc_id, user)

    witnesses = [
        row["username"]
        for row in db.execute(
            """SELECT u.username FROM document_witnesses w
               JOIN users u ON u.id = w.user_id
               WHERE w.document_id = ? ORDER BY w.position""",
            (doc_id,),
        ).fetchall()
    ]

    result: dict[str, Any] = {
        "document_id": doc_id,
        "status": doc["status"],
        "created_at": doc["created_at"],
        "witnesses": witnesses,
    }

    latest = db.execute(
        "SELECT * FROM open_requests WHERE document_id = ? ORDER BY id DESC LIMIT 1",
        (doc_id,),
    ).fetchone()

    if doc["status"] == "opened":
        result["body"] = doc["body"]            # 开封后才返回正文
        result["digest"] = doc["digest"]
        result["opened_at"] = doc["opened_at"]
        result["purge_at"] = doc["purge_at"]    # 已安排的清除时刻（可能为 null）
        if latest is not None:
            result["open_request"] = _open_request_meta(latest)
            witness_records = db.execute(
                """SELECT u.username, a.bound_digest, a.created_at AS approved_at
                   FROM approvals a JOIN users u ON u.id = a.witness_id
                   WHERE a.request_id = ? ORDER BY a.id""",
                (latest["id"],),
            ).fetchall()
            result["witness_approvals"] = [dict(row) for row in witness_records]
    elif doc["status"] == "purged":
        # 已清除：保留摘要、参与方与审计元数据；任何接口不再返回正文
        result["digest"] = doc["digest"]
        result["purge_at"] = doc["purge_at"]
        result["purged_at"] = doc["purged_at"]
        if doc["opened_at"] is not None:
            result["opened_at"] = doc["opened_at"]
        if latest is not None:
            result["open_request"] = _open_request_meta(latest)
            witness_records = db.execute(
                """SELECT u.username, a.bound_digest, a.created_at AS approved_at
                   FROM approvals a JOIN users u ON u.id = a.witness_id
                   WHERE a.request_id = ? ORDER BY a.id""",
                (latest["id"],),
            ).fetchall()
            result["witness_approvals"] = [dict(row) for row in witness_records]
    else:
        # 开封前：只给当前开封请求的元数据，不给正文，也不给谁已同意
        result["purge_at"] = doc["purge_at"]    # 已安排的清除时刻（可能为 null）
        if latest is not None:
            result["latest_open_request"] = {
                "request_id": latest["id"],
                "reason": latest["reason"],
                "deadline": latest["deadline"],
                "earliest_open_at": latest["earliest_open_at"],
                "status": latest["status"],
                "created_at": latest["created_at"],
                "approval_count": db.execute(
                    "SELECT COUNT(*) AS c FROM approvals WHERE request_id = ?",
                    (latest["id"],),
                ).fetchone()["c"],
            }
    return result


# ---------------------------------------------------------------------------
# 开封请求
# ---------------------------------------------------------------------------


@app.post("/documents/{doc_id}/open-requests", status_code=201)
def create_open_request(
    doc_id: int,
    payload: OpenRequestIn,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    doc = load_doc_for_participant(db, doc_id, user)
    if doc["owner_id"] != user["id"]:
        raise err(403, "只有封存人可以提交开封请求")

    deadline = utc_str(payload.deadline)
    earliest = utc_str(payload.earliest_open_at) if payload.earliest_open_at else None
    now = utc_str(now_utc())
    if deadline <= now:
        raise err(400, "截止时刻必须晚于当前时间")
    if earliest is not None and earliest < now:
        raise err(400, "最早开封时刻不能早于当前时间")

    # 清除若在本临界区内生效，清除先落库，拒绝在提交后再返回（与同意/撤销一致）
    post_commit_error: HTTPException | None = None
    request_id: int | None = None
    with write_tx(db) as tx:
        # 锁内先处理到点的定时开封与到期清除，避免与同意/撤销/扫描并发
        resolve_due_waiting(tx, now)
        purge_due_documents(tx, now)
        doc = tx.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if doc["status"] == "purged":
            post_commit_error = err(409, "资料已清除，不能提交开封请求")
        elif doc["status"] != "sealed":
            raise err(409, "资料已开封，重复开封被拒绝")
        active = None if post_commit_error else tx.execute(
            "SELECT id, deadline FROM open_requests"
            " WHERE document_id = ? AND status IN ('pending', 'waiting')",
            (doc_id,),
        ).fetchone()
        if active is not None:
            if active["deadline"] <= now:
                # 理论上扫描器很快会处理；在同一临界区内顺手确定性失效
                tx.execute(
                    "UPDATE open_requests SET status='expired', resolved_at=? WHERE id=?",
                    (now, active["id"]),
                )
                add_audit(
                    tx,
                    actor_id=None,
                    action="request_expired",
                    document_id=doc_id,
                    request_id=active["id"],
                    detail="旧开封请求已过截止时刻，提交新请求时确认失效，旧同意作废",
                )
            else:
                raise err(409, "已有进行中的开封请求，请先撤销或等待其到期")

        if post_commit_error is None:
            cur = tx.execute(
                "INSERT INTO open_requests (document_id, reason, deadline, status, created_at, earliest_open_at)"
                " VALUES (?, ?, ?, 'pending', ?, ?)",
                (doc_id, payload.reason, deadline, now, earliest),
            )
            request_id = cur.lastrowid
            timing = f"最早开封时刻: {earliest}" if earliest else "未指定最早开封时刻，两票集齐即开封"
            add_audit(
                tx,
                actor_id=user["id"],
                action="open_request_created",
                document_id=doc_id,
                request_id=request_id,
                detail=f"提交开封请求，理由: {payload.reason}；截止时刻: {deadline}；{timing}",
            )

    if post_commit_error is not None:
        raise post_commit_error
    assert request_id is not None
    return {
        "request_id": request_id,
        "document_id": doc_id,
        "status": "pending",
        "reason": payload.reason,
        "deadline": deadline,
        "earliest_open_at": earliest,
    }


@app.post("/open-requests/{request_id}/approve")
def approve_open_request(
    request_id: int,
    payload: ApproveIn,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    """见证人同意：必须是该资料的三名指定见证人之一；摘要须与封存摘要一致。

    所有判定均在写事务内完成；任何拒绝路径都不写入数据（状态不变）。
    """
    meta = db.execute(
        """SELECT r.id AS request_id, r.document_id, r.status AS req_status,
                  r.deadline, r.earliest_open_at, d.status AS doc_status, d.digest
           FROM open_requests r JOIN documents d ON d.id = r.document_id
           WHERE r.id = ?""",
        (request_id,),
    ).fetchone()
    if meta is None:
        raise err(403, "无权操作该请求或请求不存在")
    is_witness = db.execute(
        "SELECT 1 FROM document_witnesses WHERE document_id = ? AND user_id = ?",
        (meta["document_id"], user["id"]),
    ).fetchone()
    if is_witness is None:
        raise err(403, "只有指定见证人可以同意")

    now = utc_str(now_utc())
    # 到点开封/到期清除必须先落库：若本请求因此开封（或资料因此清除）而本次
    # 同意仍需拒绝，要等事务提交后再抛错，绝不能让回滚把已经生效的判定撤销。
    post_commit_error: HTTPException | None = None
    response: dict[str, Any] | None = None
    try:
        with write_tx(db) as tx:
            # 临界区第一步：到点的 waiting 请求必开封、到清除时刻的资料必清除；
            # 之后任何状态判定都以此为准，因此定时处理即使被后台延迟，
            # 也不会被并发同意/撤销/扫描作废。
            due_opened = resolve_due_waiting(tx, now, request_id) > 0
            purged_here = purge_due_documents(tx, now, meta["document_id"]) > 0
            req = tx.execute(
                "SELECT * FROM open_requests WHERE id = ?", (request_id,)
            ).fetchone()
            doc = tx.execute(
                "SELECT * FROM documents WHERE id = ?", (req["document_id"],)
            ).fetchone()

            if doc["status"] != "sealed":
                if doc["status"] == "purged":
                    not_sealed = err(409, "资料已清除，同意被拒绝")
                else:
                    not_sealed = err(409, "资料已开封，重复开封被拒绝")
                if due_opened or purged_here:
                    post_commit_error = not_sealed
                else:
                    raise not_sealed
            elif req["status"] == "waiting":
                raise err(409, "两票已集齐，待到达最早开封时刻自动开封，同意被拒绝")
            elif req["status"] != "pending":
                raise err(409, f"开封请求当前状态为 {req['status']}，同意被拒绝")
            elif req["deadline"] <= now:
                raise err(409, "已过截止时刻，同意被拒绝，请求即将失效")
            elif not tx.execute(
                "SELECT 1 FROM document_witnesses WHERE document_id = ? AND user_id = ?",
                (doc["id"], user["id"]),
            ).fetchone():
                # 见证人名单可在资料未开封时被封存人替换：进入写临界区后必须以
                # “当前名单”为准复核，防止“预校验通过后在锁外被替换”的旧见证人投票。
                raise err(403, "只有当前指定见证人可以同意")
            elif payload.digest != doc["digest"]:
                # 不同意即不绑定本次请求：不落库
                raise err(409, "提交的摘要与封存时摘要不一致，拒绝同意")
            else:
                already = tx.execute(
                    "SELECT 1 FROM approvals WHERE request_id = ? AND witness_id = ?",
                    (request_id, user["id"]),
                ).fetchone()
                if already is not None:
                    raise err(409, "你已同意本次请求，重复提交只计一次")

                tx.execute(
                    "INSERT INTO approvals (request_id, witness_id, bound_digest, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (request_id, user["id"], doc["digest"], now),
                )
                add_audit(
                    tx,
                    actor_id=user["id"],
                    action="approval",
                    document_id=doc["id"],
                    request_id=request_id,
                    detail="见证人同意开封，已绑定本次请求与封存摘要",
                )

                count = tx.execute(
                    "SELECT COUNT(*) AS c FROM approvals WHERE request_id = ?", (request_id,)
                ).fetchone()["c"]

                new_status = "pending"
                if count >= 2:
                    # 第二名不同见证人到位。UNIQUE(request_id, witness_id)
                    # 保证两名同意必然来自不同见证人。
                    earliest = req["earliest_open_at"]
                    if earliest is None or earliest <= now:
                        # 未设最早开封时刻（集齐即开），或已到最早开封时刻：立即开封
                        open_request_now(
                            tx,
                            req_id=request_id,
                            doc_id=doc["id"],
                            now=now,
                            actor_id=user["id"],
                            detail="两名不同指定见证人均已同意，资料正式开封",
                        )
                        new_status = "opened"
                    else:
                        # 两票在最早开封时刻前集齐：只进入待开封，到点自动开封
                        tx.execute(
                            "UPDATE open_requests SET status='waiting' WHERE id=?",
                            (request_id,),
                        )
                        add_audit(
                            tx,
                            actor_id=user["id"],
                            action="request_waiting",
                            document_id=doc["id"],
                            request_id=request_id,
                            detail=f"两名不同指定见证人均已同意，待最早开封时刻 {earliest} 自动开封",
                        )
                        new_status = "waiting"

                response = {
                    "request_id": request_id,
                    "document_id": doc["id"],
                    "approval_count": count,
                    "required": 2,
                    "status": new_status,
                }
                if req["earliest_open_at"] is not None:
                    response["earliest_open_at"] = req["earliest_open_at"]
        if post_commit_error is not None:
            raise post_commit_error
        assert response is not None
        return response
    except sqlite3.IntegrityError:
        # 唯一约束兜底（并发重复同意）
        raise err(409, "你已同意本次请求，重复提交只计一次")


@app.post("/open-requests/{request_id}/revoke")
def revoke_open_request(
    request_id: int,
    payload: RevokeIn,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    meta = db.execute(
        """SELECT r.id, r.document_id, r.status, r.deadline, d.owner_id
           FROM open_requests r JOIN documents d ON d.id = r.document_id
           WHERE r.id = ?""",
        (request_id,),
    ).fetchone()
    if meta is None:
        raise err(403, "无权操作该请求或请求不存在")
    if meta["owner_id"] != user["id"]:
        raise err(403, "只有封存人可以撤销开封请求")

    now = utc_str(now_utc())
    # 与同意相同：到点开封/到期清除先提交，拒绝（撤销已随开封/清除失效的请求）
    # 在提交后再返回。
    post_commit_error: HTTPException | None = None
    with write_tx(db) as tx:
        due_opened = resolve_due_waiting(tx, now, request_id) > 0
        purged_here = purge_due_documents(tx, now, meta["document_id"]) > 0
        req = tx.execute("SELECT * FROM open_requests WHERE id = ?", (request_id,)).fetchone()
        doc = tx.execute("SELECT * FROM documents WHERE id = ?", (req["document_id"],)).fetchone()
        if doc["status"] != "sealed":
            if doc["status"] == "purged":
                not_sealed = err(409, "资料已清除，无法撤销")
            else:
                not_sealed = err(409, "资料已开封，无法撤销")
            if due_opened or purged_here:
                post_commit_error = not_sealed
            else:
                raise not_sealed
        elif req["status"] not in ("pending", "waiting"):
            raise err(409, f"开封请求当前状态为 {req['status']}，无法撤销")
        elif req["deadline"] <= now:
            raise err(409, "已过截止时刻，不能撤销，请求即将失效")
        else:
            waiting_note = "（请求已集齐两票，待到点开封）" if req["status"] == "waiting" else ""
            tx.execute(
                "UPDATE open_requests SET status='revoked', resolved_at=? WHERE id=?",
                (now, request_id),
            )
            add_audit(
                tx,
                actor_id=user["id"],
                action="revoked",
                document_id=req["document_id"],
                request_id=request_id,
                detail=f"封存人撤销开封请求{waiting_note}，已有同意全部作废。原因: {payload.reason or '（未填写）'}",
            )
    if post_commit_error is not None:
        raise post_commit_error
    return {"request_id": request_id, "status": "revoked"}


# ---------------------------------------------------------------------------
# 见证人替换（资料未开封）
#
# 封存人可在资料未开封时把一名指定见证人换成新见证人。新见证人必须已注册、
# 非封存人、且不在当前名单；替换后仍是三名互不相同的见证人。与同意/撤销
# 相同：临界区内先做确定性到点开封——已到最早开封时刻且两票已齐的请求会
# 先开封，本次替换随即被拒绝；已过截止时刻的进行中请求按原规则先失效，
# 再完成替换。被替换者对进行中请求的同意立即删除（失效），其他同意保留，
# 有效票不足两票时 waiting 退回 pending；旧见证人立刻失去访问与同意权限。
# ---------------------------------------------------------------------------


@app.post("/documents/{doc_id}/witness-replacements")
def replace_witness(
    doc_id: int,
    payload: ReplaceWitnessIn,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    doc = load_doc_for_participant(db, doc_id, user)
    if doc["owner_id"] != user["id"]:
        raise err(403, "只有封存人可以替换见证人")

    now = utc_str(now_utc())
    post_commit_error: HTTPException | None = None
    result: dict[str, Any] = {}
    with write_tx(db) as tx:
        # 临界区第一步与其他写路径一致：先确定性到点开封与到期清除。
        # 用全局解析：与扫描器等价，顺带把已到点的 waiting 请求开封、
        # 到清除时刻的资料清除。
        resolve_due_waiting(tx, now)
        purge_due_documents(tx, now)
        doc = tx.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if doc["status"] != "sealed":
            # 资料已开封/已清除即不允许替换：无论状态变更是本临界区刚触发的
            # 到点判定，还是并发事务在更早时完成的，结论一致。
            # 若为前者，变更必须先随本事务提交，故错误延迟到提交后再抛出。
            if doc["status"] == "purged":
                post_commit_error = err(409, "资料已清除，无法替换见证人")
            else:
                post_commit_error = err(409, "资料已开封，无法替换见证人")
        else:
            old_row = tx.execute(
                """SELECT w.id, w.user_id, u.username FROM document_witnesses w
                   JOIN users u ON u.id = w.user_id
                   WHERE w.document_id = ? AND u.username = ?""",
                (doc_id, payload.old_witness),
            ).fetchone()
            if old_row is None:
                raise err(404, "被替换者不在当前见证人名单中")

            new_user = tx.execute(
                "SELECT id, username FROM users WHERE username = ?",
                (payload.new_witness,),
            ).fetchone()
            if new_user is None:
                raise err(400, "新见证人尚未注册")
            if new_user["id"] == user["id"]:
                raise err(400, "封存人不能同时担任见证人")
            already = tx.execute(
                "SELECT 1 FROM document_witnesses WHERE document_id = ? AND user_id = ?",
                (doc_id, new_user["id"]),
            ).fetchone()
            if already is not None:
                raise err(409, "新见证人已在当前见证人名单中")
            # 待确认交接邀请的受让人不能成为见证人：交接规则要求受让人始终
            # 不是当前见证人，否则确认后会出现“新封存人同时是见证人”。
            pending_assignee = tx.execute(
                "SELECT 1 FROM handover_invitations"
                " WHERE document_id = ? AND status = 'pending' AND to_user_id = ?",
                (doc_id, new_user["id"]),
            ).fetchone()
            if pending_assignee is not None:
                raise err(409, "该账号是待确认交接邀请的受让人，不能同时成为见证人")

            # 进行中的开封请求（pending/waiting）。已过截止时刻的 pending
            # 请求按原规则在同一临界区内先确定性失效，再完成替换。
            active = tx.execute(
                "SELECT * FROM open_requests"
                " WHERE document_id = ? AND status IN ('pending', 'waiting')"
                " ORDER BY id DESC LIMIT 1",
                (doc_id,),
            ).fetchone()
            expired_id: int | None = None
            if active is not None and active["status"] == "pending" and active["deadline"] <= now:
                tx.execute(
                    "UPDATE open_requests SET status='expired', resolved_at=? WHERE id=?",
                    (now, active["id"]),
                )
                add_audit(
                    tx,
                    actor_id=None,
                    action="request_expired",
                    document_id=doc_id,
                    request_id=active["id"],
                    detail="替换见证人时确认开封请求已过截止时刻，请求按原规则失效，已有同意作废",
                )
                expired_id = active["id"]
                active = None

            # 替换名单：新见证人占据被替换者的位置，三名互异不变。
            tx.execute(
                "UPDATE document_witnesses SET user_id = ? WHERE id = ?",
                (new_user["id"], old_row["id"]),
            )

            details = [
                f"封存人替换见证人：{payload.old_witness} -> {payload.new_witness}",
                "新见证人已注册且不在原名单，替换后仍为三名互不相同见证人",
            ]

            req_info: dict[str, Any] | None = None
            if expired_id is not None:
                req_info = {
                    "request_id": expired_id,
                    "status": "expired",
                    "approval_count": 0,
                }
                details.append(f"进行中的开封请求 {expired_id} 已过截止时刻，按原规则失效")
            elif active is not None:
                removed = tx.execute(
                    "DELETE FROM approvals WHERE request_id = ? AND witness_id = ?",
                    (active["id"], old_row["user_id"]),
                ).rowcount
                if removed:
                    details.append(
                        f"被替换者 {payload.old_witness} 对开封请求 {active['id']} 的同意已失效"
                    )

                count = tx.execute(
                    "SELECT COUNT(*) AS c FROM approvals WHERE request_id = ?",
                    (active["id"],),
                ).fetchone()["c"]
                req_status = active["status"]
                if req_status == "waiting" and count < 2:
                    # 被替换者带走一票后有效票不足两票：待开封退回待同意，
                    # 新见证人可凭封存摘要为本次请求补票。
                    tx.execute(
                        "UPDATE open_requests SET status='pending' WHERE id=?",
                        (active["id"],),
                    )
                    req_status = "pending"
                    details.append(
                        f"开封请求 {active['id']} 有效票不足两票，由 waiting 退回 pending，"
                        "新见证人可凭封存摘要补票"
                    )
                else:
                    details.append("其他见证人对本次请求的同意保留")

                req_info = {
                    "request_id": active["id"],
                    "status": req_status,
                    "approval_count": count,
                }

            add_audit(
                tx,
                actor_id=user["id"],
                action="witness_replaced",
                document_id=doc_id,
                request_id=req_info["request_id"] if req_info else None,
                detail="；".join(details),
            )

            witnesses = [
                row["username"]
                for row in tx.execute(
                    """SELECT u.username FROM document_witnesses w
                       JOIN users u ON u.id = w.user_id
                       WHERE w.document_id = ? ORDER BY w.position""",
                    (doc_id,),
                ).fetchall()
            ]
            result = {
                "document_id": doc_id,
                "status": "sealed",
                "witnesses": witnesses,
                "open_request": req_info,
            }

    if post_commit_error is not None:
        raise post_commit_error
    return result


# ---------------------------------------------------------------------------
# 封存人交接（资料未开封）
#
# 封存人可就未开封资料向另一名本地账号发起交接邀请，填写受让人账号与带时区
# 的确认截止时刻。受让人须已注册、不是封存人本人、不是当前见证人；每份资料
# 同时至多一份待确认且未过期的邀请（DB 部分唯一索引 + 事务内预检双保险）。
# 封存人可撤回待确认邀请；受让人须在截止前凭本人 X-API-Key 确认。
#
# 确认结算顺序（全部在同一写临界区内）：
#   1) 确定性到点开封（resolve_due_waiting）与开封请求到期判定；
#   2) 资料已开封 -> 拒绝交接（开封若为本临界区触发则先提交再报错）；
#   3) 把进行中的开封请求置为 invalidated，并删除其全部旧同意（永久失效）；
#   4) 转移 documents.owner_id；见证名单、正文、摘要一律不变；
#   5) 原封存人失去该资料查看/操作权限，新封存人可重新申请开封。
# 已撤回/逾期/已确认/资料已开封的邀请一律不能确认；逾期或撤回后可重新邀请。
# 开封前任何响应（含全部错误体）都不返回正文。
# ---------------------------------------------------------------------------


def serialize_invitation(tx: sqlite3.Connection, inv: sqlite3.Row) -> dict[str, Any]:
    """组装邀请状态；只含账号名与状态/时刻等元数据，绝不含正文或摘要。"""
    from_user = tx.execute(
        "SELECT username FROM users WHERE id = ?", (inv["from_user_id"],)
    ).fetchone()["username"]
    to_user = tx.execute(
        "SELECT username FROM users WHERE id = ?", (inv["to_user_id"],)
    ).fetchone()["username"]
    return {
        "invitation_id": inv["id"],
        "document_id": inv["document_id"],
        "from_username": from_user,
        "to_username": to_user,
        "confirm_deadline": inv["confirm_deadline"],
        "status": inv["status"],
        "created_at": inv["created_at"],
        "resolved_at": inv["resolved_at"],
    }


@app.post("/documents/{doc_id}/handover-invitations", status_code=201)
def create_handover_invitation(
    doc_id: int,
    payload: HandoverIn,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    doc = load_doc_for_participant(db, doc_id, user)
    if doc["owner_id"] != user["id"]:
        raise err(403, "只有封存人可以发起交接")

    deadline = utc_str(payload.confirm_deadline)
    now = utc_str(now_utc())
    if deadline <= now:
        raise err(400, "确认截止时刻必须晚于当前时间")

    post_commit_error: HTTPException | None = None
    invitation: dict[str, Any] | None = None
    try:
        with write_tx(db) as tx:
            # 临界区第一步与其他写路径一致：先确定性到点开封与到期清除
            # （开封/清除会连带使既有待确认邀请失效，必须先落库）。
            resolve_due_waiting(tx, now)
            purge_due_documents(tx, now)
            expire_due_invitations(tx, now)

            doc = tx.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
            if doc["status"] != "sealed":
                # 开封/清除若为本临界区刚触发，必须先提交，错误延迟到提交后抛出。
                if doc["status"] == "purged":
                    post_commit_error = err(409, "资料已清除，不能发起交接")
                else:
                    post_commit_error = err(409, "资料已开封，不能发起交接")
            else:
                if doc["owner_id"] != user["id"]:
                    raise err(403, "只有封存人可以发起交接")

                to_user = tx.execute(
                    "SELECT id, username FROM users WHERE username = ?",
                    (payload.to_username,),
                ).fetchone()
                if to_user is None:
                    raise err(400, "受让人尚未注册")
                if to_user["id"] == user["id"]:
                    raise err(400, "受让人不能是封存人本人")
                is_witness = tx.execute(
                    "SELECT 1 FROM document_witnesses WHERE document_id = ? AND user_id = ?",
                    (doc_id, to_user["id"]),
                ).fetchone()
                if is_witness is not None:
                    raise err(400, "受让人不能是当前见证人")

                active = tx.execute(
                    "SELECT id FROM handover_invitations"
                    " WHERE document_id = ? AND status = 'pending'",
                    (doc_id,),
                ).fetchone()
                if active is not None:
                    raise err(409, "已有待确认且未过期的交接邀请，请先撤回或等待其逾期")

                cur = tx.execute(
                    "INSERT INTO handover_invitations"
                    " (document_id, from_user_id, to_user_id, confirm_deadline, status, created_at)"
                    " VALUES (?, ?, ?, ?, 'pending', ?)",
                    (doc_id, user["id"], to_user["id"], deadline, now),
                )
                invitation_id = cur.lastrowid
                add_audit(
                    tx,
                    actor_id=user["id"],
                    action="handover_invited",
                    document_id=doc_id,
                    detail=(
                        f"封存人向 {payload.to_username} 发起交接邀请 {invitation_id}，"
                        f"确认截止时刻: {deadline}"
                    ),
                )
                inv = tx.execute(
                    "SELECT * FROM handover_invitations WHERE id = ?", (invitation_id,)
                ).fetchone()
                invitation = serialize_invitation(tx, inv)
        if post_commit_error is not None:
            raise post_commit_error
        assert invitation is not None
        return invitation
    except sqlite3.IntegrityError:
        # 部分唯一索引兜底（并发重复邀请）：每份资料至多一份待确认邀请
        raise err(409, "已有待确认且未过期的交接邀请，请先撤回或等待其逾期")


def _load_invitation_for_owner(
    db: sqlite3.Connection, invitation_id: int, user: sqlite3.Row
) -> sqlite3.Row:
    """取邀请并校验“当前封存人”身份；其他情况统一 403，不区分是否存在。"""
    inv = db.execute(
        "SELECT * FROM handover_invitations WHERE id = ?", (invitation_id,)
    ).fetchone()
    if inv is None:
        raise err(403, "无权操作该邀请或邀请不存在")
    doc = db.execute(
        "SELECT * FROM documents WHERE id = ?", (inv["document_id"],)
    ).fetchone()
    if doc is None or doc["owner_id"] != user["id"] or inv["from_user_id"] != user["id"]:
        raise err(403, "只有发起交接的封存人可以操作该邀请")
    return inv


@app.post("/handover-invitations/{invitation_id}/revoke")
def revoke_handover_invitation(
    invitation_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    _load_invitation_for_owner(db, invitation_id, user)

    now = utc_str(now_utc())
    # 与同意/撤销开封请求相同：若开封/清除在本临界区内被确定性触发，
    # 状态变更先提交，撤回失败在提交后再返回。
    post_commit_error: HTTPException | None = None
    invitation: dict[str, Any] | None = None
    with write_tx(db) as tx:
        resolve_due_waiting(tx, now)
        purge_due_documents(tx, now)
        expired_here = expire_due_invitations(tx, now, invitation_id) > 0

        inv = tx.execute(
            "SELECT * FROM handover_invitations WHERE id = ?", (invitation_id,)
        ).fetchone()
        doc = tx.execute(
            "SELECT * FROM documents WHERE id = ?", (inv["document_id"],)
        ).fetchone()
        if doc["owner_id"] != user["id"] or inv["from_user_id"] != user["id"]:
            raise err(403, "只有发起交接的封存人可以撤回该邀请")
        if doc["status"] != "sealed":
            if doc["status"] == "purged":
                post_commit_error = err(409, "资料已清除，邀请已失效，无法撤回")
            else:
                post_commit_error = err(409, "资料已开封，邀请已失效，无法撤回")
        elif expired_here or inv["status"] == "expired":
            post_commit_error = err(409, "交接邀请已逾期，不能撤回（可重新邀请）")
        elif inv["status"] == "confirmed":
            raise err(409, "交接已完成，不能撤回")
        elif inv["status"] == "revoked":
            raise err(409, "交接邀请已撤回")
        else:
            tx.execute(
                "UPDATE handover_invitations SET status='revoked', resolved_at=? WHERE id=?",
                (now, invitation_id),
            )
            add_audit(
                tx,
                actor_id=user["id"],
                action="handover_revoked",
                document_id=inv["document_id"],
                detail=f"封存人撤回交接邀请 {invitation_id}",
            )
            inv = tx.execute(
                "SELECT * FROM handover_invitations WHERE id = ?", (invitation_id,)
            ).fetchone()
            invitation = serialize_invitation(tx, inv)
    if post_commit_error is not None:
        raise post_commit_error
    assert invitation is not None
    return invitation


@app.post("/handover-invitations/{invitation_id}/confirm")
def confirm_handover(
    invitation_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    """受让人凭本人 X-API-Key 在截止前确认交接。

    成功后返回邀请状态、当前封存人、当前见证名单与旧开封请求结果；
    任何拒绝路径都不转移身份、不泄露正文。
    """
    pre = db.execute(
        "SELECT * FROM handover_invitations WHERE id = ?", (invitation_id,)
    ).fetchone()
    if pre is None:
        raise err(403, "无权操作该邀请或邀请不存在")
    if pre["to_user_id"] != user["id"]:
        # 发起方、见证人、无关账号一律不可确认，且不区分邀请是否存在
        raise err(403, "只有受让人本人可以确认交接")

    now = utc_str(now_utc())
    post_commit_error: HTTPException | None = None
    response: dict[str, Any] | None = None
    with write_tx(db) as tx:
        # 结算顺序第 1 步：到点开封 / 到期清除 / 邀请逾期全部确定性落库。
        resolve_due_waiting(tx, now)
        purge_due_documents(tx, now)
        expire_due_invitations(tx, now)

        inv = tx.execute(
            "SELECT * FROM handover_invitations WHERE id = ?", (invitation_id,)
        ).fetchone()
        doc = tx.execute(
            "SELECT * FROM documents WHERE id = ?", (inv["document_id"],)
        ).fetchone()

        # 先锁定当前进行中（pending/waiting）的开封请求（若有），后续结算以它为准。
        active = tx.execute(
            "SELECT * FROM open_requests"
            " WHERE document_id = ? AND status IN ('pending', 'waiting')"
            " ORDER BY id DESC LIMIT 1",
            (doc["id"],),
        ).fetchone()

        # 与提交开封请求/替换见证人路径一致：已过截止时刻仍 pending 的
        # 旧请求先按原规则确定性失效（其同意随后面的交接结算一并删除）。
        if active is not None and active["status"] == "pending" and active["deadline"] <= now:
            tx.execute(
                "UPDATE open_requests SET status='expired', resolved_at=? WHERE id=?",
                (now, active["id"]),
            )
            add_audit(
                tx,
                actor_id=None,
                action="request_expired",
                document_id=doc["id"],
                request_id=active["id"],
                detail="确认交接时确认开封请求已过截止时刻，请求按原规则失效，已有同意作废",
            )

        if inv["to_user_id"] != user["id"]:
            raise err(403, "只有受让人本人可以确认交接")
        if doc["status"] != "sealed":
            # 开封/清除（含本临界区刚触发的到点判定）后拒绝交接；状态变更必须先随
            # 本事务提交，故错误延迟到提交后再返回（与同意/替换路径一致）。
            if doc["status"] == "purged":
                post_commit_error = err(409, "资料已清除，交接被拒绝")
            else:
                post_commit_error = err(409, "资料已开封，交接被拒绝")
        elif inv["status"] == "expired":
            # 本临界区刚判定逾期也要先提交，故延迟到提交后再报错
            post_commit_error = err(409, "交接邀请已逾期，不能确认（请封存人重新邀请）")
        elif inv["status"] == "revoked":
            raise err(409, "交接邀请已撤回，不能确认")
        elif inv["status"] == "confirmed":
            raise err(409, "交接邀请已确认")
        elif inv["confirm_deadline"] <= now:
            # 锁内兜底：即便上面的统一逾期处理未命中（理论上不会），也绝不超期确认
            post_commit_error = err(409, "已过确认截止时刻，不能确认")
        else:
            # 结算顺序第 2 步：进行中的开封请求失效（过截止的上面已置 expired），
            # 旧同意删除。
            old_request_info: dict[str, Any] | None = None
            if active is not None:
                if active["deadline"] <= now:
                    # 上面已按原规则把过截止时刻的 pending 请求置 expired
                    old_status = "expired"
                else:
                    tx.execute(
                        "UPDATE open_requests SET status='invalidated', resolved_at=? WHERE id=?",
                        (now, active["id"]),
                    )
                    add_audit(
                        tx,
                        actor_id=user["id"],
                        action="request_invalidated",
                        document_id=doc["id"],
                        request_id=active["id"],
                        detail="封存人交接确认：进行中的开封请求失效，其全部旧同意作废，新封存人可重新申请开封",
                    )
                    old_status = "invalidated"
                tx.execute(
                    "DELETE FROM approvals WHERE request_id = ?", (active["id"],)
                )
                old_request_info = {
                    "request_id": active["id"],
                    "status": old_status,
                    "approval_count": 0,
                }

            # 该资料其他待确认邀请（部分唯一索引保证至多一份，这里防御性处理）
            # 一并失效；正在确认的本邀请下一步置为 confirmed，不能误判为逾期。
            invalidate_pending_invitations(
                tx,
                doc_id=doc["id"],
                now=now,
                exclude_id=invitation_id,
                detail="封存人交接已完成，其他待确认邀请失效",
            )

            old_owner = tx.execute(
                "SELECT username FROM users WHERE id = ?", (doc["owner_id"],)
            ).fetchone()["username"]

            # 结算顺序第 3 步：转移封存人身份；名单、正文、摘要不变。
            tx.execute(
                "UPDATE documents SET owner_id = ? WHERE id = ?",
                (user["id"], doc["id"]),
            )
            tx.execute(
                "UPDATE handover_invitations SET status='confirmed', resolved_at=? WHERE id=?",
                (now, invitation_id),
            )
            add_audit(
                tx,
                actor_id=user["id"],
                action="handover_confirmed",
                document_id=doc["id"],
                detail=(
                    f"封存人交接完成：{old_owner} -> {user['username']}；"
                    "见证名单、正文与摘要不变；原封存人失去该资料权限"
                ),
            )

            witnesses = [
                row["username"]
                for row in tx.execute(
                    """SELECT u.username FROM document_witnesses w
                       JOIN users u ON u.id = w.user_id
                       WHERE w.document_id = ? ORDER BY w.position""",
                    (doc["id"],),
                ).fetchall()
            ]
            inv = tx.execute(
                "SELECT * FROM handover_invitations WHERE id = ?", (invitation_id,)
            ).fetchone()
            response = {
                **serialize_invitation(tx, inv),
                "document_id": doc["id"],
                "document_status": "sealed",
                "current_owner": user["username"],
                "previous_owner": old_owner,
                "witnesses": witnesses,
                "old_open_request": old_request_info,
            }
    if post_commit_error is not None:
        raise post_commit_error
    assert response is not None
    return response


@app.get("/handover-invitations/{invitation_id}")
def get_handover_invitation(
    invitation_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    """查询邀请状态：邀请双方或该资料当前参与方（封存人/见证人）可见；只含元数据。

    资料到清除时刻的，先确定性清除（待确认邀请随之失效），再返回最新状态。
    """
    inv = db.execute(
        "SELECT * FROM handover_invitations WHERE id = ?", (invitation_id,)
    ).fetchone()
    if inv is None:
        raise err(403, "无权操作该邀请或邀请不存在")
    settle_due_purges_for_read(db, inv["document_id"])
    inv = db.execute(
        "SELECT * FROM handover_invitations WHERE id = ?", (invitation_id,)
    ).fetchone()
    is_party = user["id"] in (inv["from_user_id"], inv["to_user_id"])
    is_participant = False
    doc = db.execute(
        "SELECT owner_id FROM documents WHERE id = ?", (inv["document_id"],)
    ).fetchone()
    if doc is not None:
        if doc["owner_id"] == user["id"]:
            is_participant = True
        else:
            is_participant = db.execute(
                "SELECT 1 FROM document_witnesses WHERE document_id = ? AND user_id = ?",
                (inv["document_id"], user["id"]),
            ).fetchone() is not None
    if not is_party and not is_participant:
        raise err(403, "无权操作该邀请或邀请不存在")
    return serialize_invitation(db, inv)


# ---------------------------------------------------------------------------
# 到期清除正文（资料封存人专享）
#
# 封存人凭本人密钥为资料安排带时区的未来清除时刻；到期前可改期（再次 PUT）
# 或取消（DELETE）。交接后清除安排保留，改由新封存人管理（原封存人 403）。
# 到点判定与开封/交接共用同一把写锁：清除若在本临界区内生效，清除先落库，
# 改期/取消的拒绝在提交后再返回；已到期的改期或取消一律 409，且不泄露正文。
# ---------------------------------------------------------------------------


@app.put("/documents/{doc_id}/purge")
def schedule_purge(
    doc_id: int,
    payload: PurgeIn,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    """安排或改期到期清除时刻。已开封资料同样可安排（到点照常清除正文）。"""
    doc = load_doc_for_participant(db, doc_id, user)
    if doc["owner_id"] != user["id"]:
        raise err(403, "只有封存人可以安排到期清除")

    purge_at = utc_str(payload.purge_at)
    now = utc_str(now_utc())
    if purge_at <= now:
        raise err(400, "清除时刻必须晚于当前时间")

    post_commit_error: HTTPException | None = None
    result: dict[str, Any] | None = None
    with write_tx(db) as tx:
        # 临界区第一步与其他写路径一致：先确定性到点开封与到期清除。
        # 清除时刻已到的资料在此生效（purged），随后的改期请求随即被拒。
        resolve_due_waiting(tx, now)
        purge_due_documents(tx, now)

        doc = tx.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if doc["status"] == "purged":
            # 清除可能刚在本临界区生效：必须先提交，错误延迟到提交后抛出
            post_commit_error = err(409, "资料已清除，不能再安排或改期清除")
        else:
            previous = doc["purge_at"]
            tx.execute(
                "UPDATE documents SET purge_at = ? WHERE id = ?",
                (purge_at, doc_id),
            )
            if previous is None:
                add_audit(
                    tx,
                    actor_id=user["id"],
                    action="purge_scheduled",
                    document_id=doc_id,
                    detail=f"封存人安排到期清除，清除时刻: {purge_at}",
                )
            else:
                add_audit(
                    tx,
                    actor_id=user["id"],
                    action="purge_rescheduled",
                    document_id=doc_id,
                    detail=f"封存人改期到期清除：{previous} -> {purge_at}",
                )
            result = {
                "document_id": doc_id,
                "status": doc["status"],
                "purge_at": purge_at,
                "previous_purge_at": previous,
            }
    if post_commit_error is not None:
        raise post_commit_error
    assert result is not None
    return result


@app.delete("/documents/{doc_id}/purge")
def cancel_purge(
    doc_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    """在清除时刻到来前取消已安排的到期清除。"""
    doc = load_doc_for_participant(db, doc_id, user)
    if doc["owner_id"] != user["id"]:
        raise err(403, "只有封存人可以取消到期清除")

    now = utc_str(now_utc())
    post_commit_error: HTTPException | None = None
    result: dict[str, Any] | None = None
    with write_tx(db) as tx:
        # 与改期相同：先确定性到点开封与到期清除；清除时刻已到的资料在此
        # 生效，取消请求随即被拒（已到期不能取消）。
        resolve_due_waiting(tx, now)
        purge_due_documents(tx, now)

        doc = tx.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if doc["status"] == "purged":
            post_commit_error = err(409, "资料已清除，不能取消清除")
        elif doc["purge_at"] is None:
            raise err(409, "当前没有已安排的清除时刻")
        else:
            previous = doc["purge_at"]
            tx.execute(
                "UPDATE documents SET purge_at = NULL WHERE id = ?",
                (doc_id,),
            )
            add_audit(
                tx,
                actor_id=user["id"],
                action="purge_canceled",
                document_id=doc_id,
                detail=f"封存人取消到期清除（原清除时刻: {previous}）",
            )
            result = {"document_id": doc_id, "status": doc["status"], "purge_at": None}
    if post_commit_error is not None:
        raise post_commit_error
    assert result is not None
    return result


# ---------------------------------------------------------------------------
# 操作记录（按顺序持久保存；非参与方不可见）
# ---------------------------------------------------------------------------


@app.get("/documents/{doc_id}/audit")
def get_audit(
    doc_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    # 到清除时刻的资料先确定性清除，审计随即包含 purged 记录
    settle_due_purges_for_read(db, doc_id)
    load_doc_for_participant(db, doc_id, user)
    rows = db.execute(
        """SELECT a.id, a.at, a.action, a.request_id, a.detail,
                  u.username AS actor
           FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id
           WHERE a.document_id = ?
           ORDER BY a.id""",
        (doc_id,),
    ).fetchall()
    return {"document_id": doc_id, "records": [dict(row) for row in rows]}


def main() -> None:  # pragma: no cover
    init_db()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
