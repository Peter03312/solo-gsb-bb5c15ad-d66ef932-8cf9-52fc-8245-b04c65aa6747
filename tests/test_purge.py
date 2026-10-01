"""到期清除（scheduled purge）功能测试。

规则要点：
- 封存人凭现有密钥提交带时区的未来清除时刻；到期前可改期或取消；
- 交接后由新封存人管理，原封存人无权操作（统一 403）；
- 到点确定性清除（不依赖扫描器准时）：清空正文、置 purged、记录清除时刻，
  保留摘要、参与方与审计元数据；此后任何接口都不返回正文；
- 已开封资料仍按期清除；未开封资料进行中的开封请求失效、旧同意作废，
  待确认交接邀请失效；
- 清除与最早开封同刻时清除优先；开封更早则先开封；
- 并发交接确认、见证同意与清除只形成一个一致结果；
- 已清除资料不能再开封、换见证人或交接；已到期的改期/取消拒绝且不泄露正文；
- 重启后恢复扫描一致。
"""
import datetime as dt
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import auth, digest_of, register, seal

BODY = "到期清除绝密资料 PURGE-SECRET"


def _iso(seconds: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat()


def _setup(client, *, owner_name="powner", names=("pw1", "pw2", "pw3"), body=BODY):
    owner = register(client, owner_name)
    ws = [register(client, n) for n in names]
    d = seal(client, owner, body, list(names))
    return owner, ws, d["document_id"], d["digest"]


def _schedule(client, owner, doc_id, when):
    purge_at = _iso(when) if isinstance(when, (int, float)) else when
    return client.post(
        f"/documents/{doc_id}/purge-schedules",
        json={"purge_at": purge_at},
        headers=auth(owner),
    )


# ---------------------------------------------------------------------------
# 安排 / 校验
# ---------------------------------------------------------------------------


def test_schedule_purge_validation(client):
    owner, ws, doc_id, _ = _setup(client)

    # 不带时区 -> 422，且不回显正文
    r = client.post(
        f"/documents/{doc_id}/purge-schedules",
        json={"purge_at": "2030-01-01T00:00:00"},
        headers=auth(owner),
    )
    assert r.status_code == 422
    assert BODY not in r.text

    # 过去时刻 -> 400
    r = _schedule(client, owner, doc_id, -10)
    assert r.status_code == 400, r.text
    assert BODY not in r.text

    # 见证人无权安排
    r = _schedule(client, ws[0], doc_id, 3600)
    assert r.status_code == 403

    # 无关账号无权（不区分资料是否存在）
    outsider = register(client, "psnoop")
    r = _schedule(client, outsider, doc_id, 3600)
    assert r.status_code == 403
    assert BODY not in r.text
    r = client.post(
        "/documents/999999/purge-schedules",
        json={"purge_at": _iso(3600)},
        headers=auth(outsider),
    )
    assert r.status_code == 403

    # 无密钥 -> 401
    r = client.post(f"/documents/{doc_id}/purge-schedules", json={"purge_at": _iso(3600)})
    assert r.status_code == 401


def test_schedule_appears_in_document_metadata_without_body(client):
    owner, ws, doc_id, _ = _setup(client)
    r = _schedule(client, owner, doc_id, 3600)
    assert r.status_code == 201, r.text
    sch = r.json()
    assert sch["status"] == "scheduled" and sch["document_id"] == doc_id
    assert sch["resolved_at"] is None

    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "sealed" and "body" not in got
    assert got["purge_schedule"]["schedule_id"] == sch["schedule_id"]
    assert got["purge_schedule"]["status"] == "scheduled"
    assert got["purge_schedule"]["purge_at"] == sch["purge_at"]

    # 列表不带正文，只带状态
    listed = client.get("/documents", headers=auth(owner)).json()["documents"]
    assert {"id": doc_id, "status": "sealed"} in [
        {"id": d["id"], "status": d["status"]} for d in listed
    ]
    assert all("body" not in d for d in listed)


def test_duplicate_schedule_conflicts_until_cancelled(client):
    owner, ws, doc_id, _ = _setup(client)
    assert _schedule(client, owner, doc_id, 3600).status_code == 201
    assert _schedule(client, owner, doc_id, 7200).status_code == 409

    sid = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()[
        "purge_schedule"]["schedule_id"]
    r = client.post(f"/purge-schedules/{sid}/cancel", headers=auth(owner))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"

    # 取消后可重新安排
    r = _schedule(client, owner, doc_id, 7200)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "scheduled"


# ---------------------------------------------------------------------------
# 改期 / 取消
# ---------------------------------------------------------------------------


def test_reschedule_before_due(client):
    owner, ws, doc_id, _ = _setup(client)
    sid = _schedule(client, owner, doc_id, 3600).json()["schedule_id"]

    r = client.post(
        f"/purge-schedules/{sid}/reschedule",
        json={"purge_at": _iso(7200)},
        headers=auth(owner),
    )
    assert r.status_code == 200
    assert r.json()["schedule_id"] == sid and r.json()["status"] == "scheduled"

    # 过去时刻 -> 400；不带时区 -> 422
    r = client.post(
        f"/purge-schedules/{sid}/reschedule",
        json={"purge_at": _iso(-1)},
        headers=auth(owner),
    )
    assert r.status_code == 400 and BODY not in r.text
    r = client.post(
        f"/purge-schedules/{sid}/reschedule",
        json={"purge_at": "2030-01-01T00:00:00"},
        headers=auth(owner),
    )
    assert r.status_code == 422 and BODY not in r.text


def test_cancel_then_reschedule_conflicts(client):
    owner, ws, doc_id, _ = _setup(client)
    sid = _schedule(client, owner, doc_id, 3600).json()["schedule_id"]
    assert client.post(f"/purge-schedules/{sid}/cancel", headers=auth(owner)).status_code == 200

    r = client.post(
        f"/purge-schedules/{sid}/reschedule",
        json={"purge_at": _iso(3600)},
        headers=auth(owner),
    )
    assert r.status_code == 409
    r = client.post(f"/purge-schedules/{sid}/cancel", headers=auth(owner))
    assert r.status_code == 409


def test_reschedule_cancel_requires_current_owner(client):
    owner, ws, doc_id, _ = _setup(client)
    sid = _schedule(client, owner, doc_id, 3600).json()["schedule_id"]

    # 见证人无权
    r = client.post(
        f"/purge-schedules/{sid}/reschedule",
        json={"purge_at": _iso(3600)},
        headers=auth(ws[0]),
    )
    assert r.status_code == 403
    assert client.post(f"/purge-schedules/{sid}/cancel", headers=auth(ws[0])).status_code == 403

    # 不存在的安排：非封存人/无关账号统一 403
    assert client.post(
        "/purge-schedules/9999/reschedule",
        json={"purge_at": _iso(3600)},
        headers=auth(owner),
    ).status_code == 403
    assert client.post("/purge-schedules/9999/cancel", headers=auth(owner)).status_code == 403


# ---------------------------------------------------------------------------
# 到点清除（扫描器）
# ---------------------------------------------------------------------------


def test_sweeper_purges_sealed_document(client, sweep):
    owner, ws, doc_id, dg = _setup(client)
    sid = _schedule(client, owner, doc_id, 1).json()["schedule_id"]

    # 先建一个进行中（带票）的开封请求与一份待确认交接邀请
    req = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "想开封", "deadline": _iso(3600)},
        headers=auth(owner),
    ).json()["request_id"]
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(ws[0]))

    erin = register(client, "perin")
    inv = client.post(
        f"/documents/{doc_id}/handover-invitations",
        json={"to_username": "perin", "confirm_deadline": _iso(3600)},
        headers=auth(owner),
    ).json()["invitation_id"]

    time.sleep(1.2)
    sweep()

    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "purged"
    assert got["purged_at"] is not None
    assert "body" not in got
    assert got["digest"] == dg  # 摘要保留
    assert got["witnesses"] == ["pw1", "pw2", "pw3"]  # 参与方保留
    assert got["purge_schedule"]["status"] == "done"
    assert got["purge_schedule"]["resolved_at"] == got["purged_at"]

    # 进行中的开封请求失效、旧同意作废
    info = got["latest_open_request"]
    assert info["status"] == "invalidated" and info["approval_count"] == 0

    # 待确认交接邀请失效
    inv_got = client.get(f"/handover-invitations/{inv}", headers=auth(erin)).json()
    assert inv_got["status"] == "expired"

    # 列表同步
    listed = {d["id"]: d for d in client.get("/documents", headers=auth(owner)).json()["documents"]}
    assert listed[doc_id]["status"] == "purged" and listed[doc_id]["purged_at"] is not None
    assert all("body" not in d for d in listed.values())

    # 审计元数据完整保留且顺序正确
    records = client.get(f"/documents/{doc_id}/audit", headers=auth(owner)).json()["records"]
    actions = [r["action"] for r in records]
    assert actions == [
        "seal", "purge_scheduled", "open_request_created", "approval",
        "handover_invited", "request_invalidated", "handover_invitation_expired", "purged",
    ]
    assert all(BODY not in r["detail"] for r in records)


def test_sweeper_purges_opened_document_too(client, sweep):
    owner, ws, doc_id, dg = _setup(client, body="已开封也要清除 BODY")
    req = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "先开封", "deadline": _iso(3600)},
        headers=auth(owner),
    ).json()["request_id"]
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(ws[0]))
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(ws[1]))
    opened = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert opened["status"] == "opened" and opened["body"] == "已开封也要清除 BODY"

    assert _schedule(client, owner, doc_id, 1).status_code == 201
    time.sleep(1.2)
    sweep()

    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "purged"
    assert "body" not in got and got["digest"] == dg
    # 开封是历史事实：开封记录保留，但不再返回正文与见证明细
    assert got["latest_open_request"]["status"] == "opened"
    assert "witness_approvals" not in got


def test_purge_does_not_depend_on_sweeper_lateness(passive_client):
    """扫描器不运行时，到点后的查询/操作也必须呈现已清除结果。"""
    c = passive_client
    owner, ws, doc_id, dg = _setup(c)
    _schedule(c, owner, doc_id, 1)

    time.sleep(1.2)
    # GET 即触发确定性清除
    got = c.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "purged" and "body" not in got
    assert got["digest"] == dg


def test_witnesses_see_purged_metadata_but_never_body(client, sweep):
    owner, ws, doc_id, dg = _setup(client)
    _schedule(client, owner, doc_id, 1)
    time.sleep(1.2)
    sweep()

    for key in ws:
        got = client.get(f"/documents/{doc_id}", headers=auth(key)).json()
        assert got["status"] == "purged" and "body" not in got
        assert got["digest"] == dg
        records = client.get(f"/documents/{doc_id}/audit", headers=auth(key)).json()["records"]
        assert records[-1]["action"] == "purged"

    outsider = register(client, "poutsider")
    assert client.get(f"/documents/{doc_id}", headers=auth(outsider)).status_code == 403


def test_database_body_is_emptied(client, sweep):
    import sqlite3

    owner, ws, doc_id, dg = _setup(client)
    _schedule(client, owner, doc_id, 1)
    time.sleep(1.2)
    sweep()

    db_path = os.environ["SEAL_DB_PATH"]
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT body, status, digest, purged_at FROM documents WHERE id=?",
                       (doc_id,)).fetchone()
    conn.close()
    assert row[0] == "" and row[1] == "purged" and row[2] == dg and row[3] is not None


# ---------------------------------------------------------------------------
# 清除与开封的先后
# ---------------------------------------------------------------------------


@pytest.fixture
def passive_client(tmp_path, monkeypatch):
    """后台扫描间隔调大，保证测试期间不自动扫描，只靠显式触发与写/读事务判定。"""
    monkeypatch.setenv("SEAL_DB_PATH", str(tmp_path / "passive-purge.db"))
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "3600")
    from app.main import app

    with TestClient(app) as c:
        yield c


def _wait_until(ts_iso: str) -> None:
    target = dt.datetime.fromisoformat(ts_iso)
    while dt.datetime.now(dt.timezone.utc) < target:
        time.sleep(0.005)


def test_purge_ties_with_earliest_open_purge_wins(passive_client):
    """清除时刻 == 最早开封时刻：第二票在该时刻到达也必须清除优先。"""
    c = passive_client
    owner, ws, doc_id, dg = _setup(c)
    # 清除与最早开封同刻；开封截止时刻在很远的未来
    t = _iso(1)
    _r = c.post(
        f"/documents/{doc_id}/purge-schedules", json={"purge_at": t}, headers=auth(owner)
    )
    assert _r.status_code == 201
    req = c.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "同刻赛跑", "deadline": _iso(3600), "earliest_open_at": t},
        headers=auth(owner),
    ).json()["request_id"]
    # 到点前只有一票
    assert c.post(
        f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(ws[0])
    ).json()["status"] == "pending"

    _wait_until(t)
    time.sleep(0.02)
    # 第二票在同刻到达：清除优先，同意被拒，资料绝不会开封
    r = c.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(ws[1]))
    assert r.status_code == 409, r.text
    assert BODY not in r.text
    got = c.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "purged" and "body" not in got
    assert got["latest_open_request"]["status"] == "invalidated"
    assert got["latest_open_request"]["approval_count"] == 0

    # 审计里只能有 purged，不能有 opened
    records = c.get(f"/documents/{doc_id}/audit", headers=auth(owner)).json()["records"]
    actions = [x["action"] for x in records]
    assert "opened" not in actions and "purged" in actions


def test_waiting_request_same_instant_purged_not_opened(passive_client):
    """两票已齐在 waiting，清除时刻与最早开封时刻相同：清除，不开封。"""
    c = passive_client
    owner, ws, doc_id, dg = _setup(c)
    t = _iso(1)
    _schedule(c, owner, doc_id, t)
    req = c.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "同刻 waiting", "deadline": _iso(3600), "earliest_open_at": t},
        headers=auth(owner),
    ).json()["request_id"]
    c.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(ws[0]))
    r = c.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(ws[1]))
    assert r.json()["status"] == "waiting"

    _wait_until(t)
    time.sleep(0.02)
    got = c.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "purged"
    assert got["latest_open_request"]["status"] == "invalidated"


def test_earlier_open_then_purge(passive_client):
    """开封时刻更早：先开封，清除到达后按已开封资料清除。"""
    c = passive_client
    owner, ws, doc_id, dg = _setup(c, body="先开封后清除 BODY")
    earliest = _iso(1)
    req = c.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "先开", "deadline": _iso(3600), "earliest_open_at": earliest},
        headers=auth(owner),
    ).json()["request_id"]
    c.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(ws[0]))
    c.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(ws[1]))

    # 开封后再安排一个更晚的清除
    _wait_until(earliest)
    time.sleep(0.05)
    got = c.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "opened" and got["body"] == "先开封后清除 BODY"

    purge_t = _iso(1)
    r = c.post(
        f"/documents/{doc_id}/purge-schedules",
        json={"purge_at": purge_t},
        headers=auth(owner),
    )
    assert r.status_code == 201, r.text
    _wait_until(r.json()["purge_at"])
    time.sleep(0.02)
    got = c.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "purged" and "body" not in got


# ---------------------------------------------------------------------------
# 已到期后的改期/取消：拒绝且已清除先落库
# ---------------------------------------------------------------------------


def test_reschedule_cancel_after_due_rejected_and_purged(passive_client):
    c = passive_client
    owner, ws, doc_id, _ = _setup(c)
    sid = _schedule(c, owner, doc_id, 1).json()["schedule_id"]
    time.sleep(1.2)

    r = c.post(
        f"/purge-schedules/{sid}/reschedule",
        json={"purge_at": _iso(3600)},
        headers=auth(owner),
    )
    assert r.status_code == 409, r.text
    assert BODY not in r.text
    got = c.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "purged"

    # 取消同样拒绝
    r = c.post(f"/purge-schedules/{sid}/cancel", headers=auth(owner))
    assert r.status_code == 409 and BODY not in r.text


# ---------------------------------------------------------------------------
# 已清除后一切操作拒绝
# ---------------------------------------------------------------------------


def test_purged_document_rejects_all_operations(client, sweep):
    owner, ws, doc_id, dg = _setup(client)
    sid = _schedule(client, owner, doc_id, 1).json()["schedule_id"]

    # 一个旧开封请求（带票）与邀请
    req = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "x", "deadline": _iso(3600)},
        headers=auth(owner),
    ).json()["request_id"]
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(ws[0]))
    erin = register(client, "perin2")
    inv = client.post(
        f"/documents/{doc_id}/handover-invitations",
        json={"to_username": "perin2", "confirm_deadline": _iso(3600)},
        headers=auth(owner),
    ).json()["invitation_id"]

    time.sleep(1.2)
    sweep()

    # 不能再开封
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "x", "deadline": _iso(3600)},
        headers=auth(owner),
    )
    assert r.status_code == 409 and BODY not in r.text
    # 旧请求不能同意/撤销
    assert client.post(
        f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(ws[1])
    ).status_code == 409
    assert client.post(f"/open-requests/{req}/revoke", json={}, headers=auth(owner)
                       ).status_code == 409
    # 不能换见证人
    new_w = register(client, "pneww")
    assert client.post(
        f"/documents/{doc_id}/witness-replacements",
        json={"old_witness": "pw1", "new_witness": "pneww"},
        headers=auth(owner),
    ).status_code == 409
    # 不能发起交接
    erin2 = register(client, "perin3")
    assert client.post(
        f"/documents/{doc_id}/handover-invitations",
        json={"to_username": "perin3", "confirm_deadline": _iso(3600)},
        headers=auth(owner),
    ).status_code == 409
    # 旧邀请不能确认（已失效）/撤回
    assert client.post(f"/handover-invitations/{inv}/confirm", headers=auth(erin)
                       ).status_code == 409
    assert client.post(f"/handover-invitations/{inv}/revoke", headers=auth(owner)
                       ).status_code == 409
    # 不能改期/取消/重新安排
    assert client.post(
        f"/purge-schedules/{sid}/reschedule",
        json={"purge_at": _iso(3600)},
        headers=auth(owner),
    ).status_code == 409
    assert client.post(f"/purge-schedules/{sid}/cancel", headers=auth(owner)
                       ).status_code == 409
    assert _schedule(client, owner, doc_id, 3600).status_code == 409
    # 所有错误体都不含正文
    assert BODY not in client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "x", "deadline": _iso(3600)},
        headers=auth(owner),
    ).text


# ---------------------------------------------------------------------------
# 交接后由新封存人管理清除安排
# ---------------------------------------------------------------------------


def test_handover_new_owner_manages_schedule_old_owner_locked_out(client):
    owner, ws, doc_id, _ = _setup(client)
    sid = _schedule(client, owner, doc_id, 3600).json()["schedule_id"]

    erin_name = "pherin"
    erin = register(client, erin_name)
    inv_id = client.post(
        f"/documents/{doc_id}/handover-invitations",
        json={"to_username": erin_name, "confirm_deadline": _iso(3600)},
        headers=auth(owner),
    ).json()["invitation_id"]

    r = client.post(f"/handover-invitations/{inv_id}/confirm", headers=auth(erin))
    assert r.status_code == 200, r.text

    # 原封存人无权改期/取消/查询
    assert client.post(
        f"/purge-schedules/{sid}/reschedule",
        json={"purge_at": _iso(3600)},
        headers=auth(owner),
    ).status_code == 403
    assert client.post(f"/purge-schedules/{sid}/cancel", headers=auth(owner)
                       ).status_code == 403
    assert client.get(f"/documents/{doc_id}", headers=auth(owner)).status_code == 403

    # 新封存人可改期（安排保留、id 不变）
    r = client.post(
        f"/purge-schedules/{sid}/reschedule",
        json={"purge_at": _iso(1)},
        headers=auth(erin),
    )
    assert r.status_code == 200 and r.json()["schedule_id"] == sid

    # 新封存人也可取消后重新安排
    assert client.post(f"/purge-schedules/{sid}/cancel", headers=auth(erin)
                       ).status_code == 200
    r = _schedule(client, erin, doc_id, 3600)
    assert r.status_code == 201
    # 见证人名单不变
    got = client.get(f"/documents/{doc_id}", headers=auth(erin)).json()
    assert got["witnesses"] == ["pw1", "pw2", "pw3"]


def test_schedule_survives_handover_and_purges_for_new_owner(client, sweep):
    owner, ws, doc_id, dg = _setup(client)
    _schedule(client, owner, doc_id, 1)

    erin = register(client, "phnew")
    inv_id = client.post(
        f"/documents/{doc_id}/handover-invitations",
        json={"to_username": "phnew", "confirm_deadline": _iso(3600)},
        headers=auth(owner),
    ).json()["invitation_id"]
    assert client.post(f"/handover-invitations/{inv_id}/confirm", headers=auth(erin)
                       ).status_code == 200

    time.sleep(1.2)
    sweep()

    got = client.get(f"/documents/{doc_id}", headers=auth(erin)).json()
    assert got["status"] == "purged" and "body" not in got and got["digest"] == dg
    # 原封存人依旧 403
    assert client.get(f"/documents/{doc_id}", headers=auth(owner)).status_code == 403


# ---------------------------------------------------------------------------
# 重启恢复
# ---------------------------------------------------------------------------


def test_due_purge_recovered_after_restart(tmp_path, monkeypatch):
    """停机期间到达清除时刻：重启恢复扫描必须呈现已清除结果。"""
    db = tmp_path / "restart-purge.db"
    monkeypatch.setenv("SEAL_DB_PATH", str(db))
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "3600")

    import uvicorn

    from app.main import app

    def serve(port):
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        return server, thread

    def wait_ready(base):
        end = time.time() + 10
        while time.time() < end:
            try:
                if httpx.get(base + "/health", timeout=1).status_code == 200:
                    return
            except Exception:
                time.sleep(0.1)
        pytest.fail("server did not start")

    def free_port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    port1 = free_port()
    server1, thread1 = serve(port1)
    base1 = f"http://127.0.0.1:{port1}"
    wait_ready(base1)
    body = "停机期间清除正文 RESTART-PURGE-SECRET"
    try:
        with httpx.Client(timeout=10) as c:
            ok = c.post(base1 + "/register", json={"username": "rp-owner"}).json()
            okey = ok["api_key"]
            for n in ("rp1", "rp2", "rp3"):
                c.post(base1 + "/register", json={"username": n})
            doc_id = c.post(
                base1 + "/documents",
                json={"body": body, "witnesses": ["rp1", "rp2", "rp3"]},
                headers={"X-API-Key": okey},
            ).json()["document_id"]
            purge_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()
            r = c.post(
                f"{base1}/documents/{doc_id}/purge-schedules",
                json={"purge_at": purge_at},
                headers={"X-API-Key": okey},
            )
            assert r.status_code == 201
    finally:
        server1.should_exit = True
        thread1.join(timeout=5)

    time.sleep(1.3)  # 清除时刻在“停机”期间流逝

    port2 = free_port()
    server2, thread2 = serve(port2)
    base2 = f"http://127.0.0.1:{port2}"
    wait_ready(base2)
    try:
        with httpx.Client(timeout=10) as c:
            h = {"X-API-Key": okey}
            got = c.get(f"{base2}/documents/{doc_id}", headers=h).json()
            assert got["status"] == "purged" and "body" not in got
            assert body not in c.get(f"{base2}/documents", headers=h).text
            records = c.get(f"{base2}/documents/{doc_id}/audit", headers=h).json()["records"]
            assert [r["action"] for r in records].count("purged") == 1

        import sqlite3

        conn = sqlite3.connect(str(db))
        row = conn.execute("SELECT body, status FROM documents WHERE id=?", (doc_id,)).fetchone()
        conn.close()
        assert row == ("", "purged")
    finally:
        server2.should_exit = True
        thread2.join(timeout=5)


# ---------------------------------------------------------------------------
# 真实并发：清除时刻 第二票同意 / 交接确认赛跑，只产生一个一致结果
# ---------------------------------------------------------------------------


def _start_server(tmp_path, monkeypatch, db_name="concurrent-purge.db", sweep_seconds=3600):
    db = tmp_path / db_name
    monkeypatch.setenv("SEAL_DB_PATH", str(db))
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", str(sweep_seconds))

    import uvicorn

    from app.main import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    end = time.time() + 10
    while time.time() < end:
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        pytest.fail("server did not start")
    return server, thread, base, str(db)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_concurrent_purge_vs_approval_and_confirm(tmp_path, monkeypatch):
    server, thread, base, db_path = _start_server(tmp_path, monkeypatch)
    try:
        with httpx.Client(timeout=10) as c:
            owner = c.post(base + "/register", json={"username": "cp-owner"}).json()["api_key"]
            wkeys = []
            for n in ("cp1", "cp2", "cp3"):
                wkeys.append(c.post(base + "/register", json={"username": n}).json()["api_key"])
            assignee = c.post(base + "/register", json={"username": "cpnew"}).json()["api_key"]
            body = "并发清除正文 CPURGE-SECRET"
            dg = digest_of(body)
            doc_id = c.post(
                base + "/documents",
                json={"body": body, "witnesses": ["cp1", "cp2", "cp3"]},
                headers={"X-API-Key": owner},
            ).json()["document_id"]

            t = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()
            c.post(f"{base}/documents/{doc_id}/purge-schedules",
                   json={"purge_at": t}, headers={"X-API-Key": owner})
            req_id = c.post(
                f"{base}/documents/{doc_id}/open-requests",
                json={"reason": "赛跑", "deadline": _iso(3600)},
                headers={"X-API-Key": owner},
            ).json()["request_id"]
            c.post(f"{base}/open-requests/{req_id}/approve", json={"digest": dg},
                   headers={"X-API-Key": wkeys[0]})
            inv_id = c.post(
                f"{base}/documents/{doc_id}/handover-invitations",
                json={"to_username": "cpnew", "confirm_deadline": _iso(3600)},
                headers={"X-API-Key": owner},
            ).json()["invitation_id"]

        _wait_until(t)
        time.sleep(0.02)
        barrier = threading.Barrier(2)

        def approve():
            barrier.wait()
            with httpx.Client(timeout=10) as c:
                return c.post(f"{base}/open-requests/{req_id}/approve",
                              json={"digest": dg},
                              headers={"X-API-Key": wkeys[1]}).status_code

        def confirm():
            barrier.wait()
            with httpx.Client(timeout=10) as c:
                return c.post(f"{base}/handover-invitations/{inv_id}/confirm",
                              headers={"X-API-Key": assignee}).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            ap_code, cf_code = list(pool.map(lambda f: f(), [approve, confirm]))

        with httpx.Client(timeout=10) as c:
            h = {"X-API-Key": owner}
            got = c.get(f"{base}/documents/{doc_id}", headers=h).json()
            # 唯一一致结果：已清除（清除优先）；二者都不可能成功
            assert got["status"] == "purged", got
            assert ap_code == 409 and cf_code == 409, (ap_code, cf_code)
            assert "body" not in got
            assert got["latest_open_request"]["status"] == "invalidated"
            assert got["latest_open_request"]["approval_count"] == 0

            import sqlite3

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT owner_id, body FROM documents WHERE id=?", (doc_id,)
            ).fetchone()
            owner_id_now = row[0]
            owner_id_old = conn.execute(
                "SELECT id FROM users WHERE username=?", ("cp-owner",)
            ).fetchone()[0]
            n_opened = conn.execute(
                "SELECT COUNT(*) FROM open_requests WHERE document_id=? AND status='opened'",
                (doc_id,),
            ).fetchone()[0]
            conn.close()
            # 身份没有被交接转移；正文已清空；从未开封
            assert owner_id_now == owner_id_old
            assert row[1] == "" and n_opened == 0
            records = c.get(f"{base}/documents/{doc_id}/audit", headers=h).json()["records"]
            assert [r["action"] for r in records].count("purged") == 1
            assert "handover_confirmed" not in [r["action"] for r in records]
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 旧库迁移：旧版 documents/open_requests 表结构升级后功能可用
# ---------------------------------------------------------------------------


def test_migration_from_legacy_schema(tmp_path, monkeypatch):
    import sqlite3 as sql

    db_path = tmp_path / "legacy.db"
    conn = sql.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
            key_hash TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL,
            body TEXT NOT NULL, digest TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('sealed', 'opened')),
            opened_at TEXT, created_at TEXT NOT NULL);
        CREATE TABLE document_witnesses (
            id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, position INTEGER NOT NULL,
            UNIQUE(document_id, user_id));
        CREATE TABLE open_requests (
            id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL,
            reason TEXT NOT NULL, deadline TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending','waiting','opened','revoked','expired')),
            created_at TEXT NOT NULL, resolved_at TEXT, earliest_open_at TEXT);
        CREATE TABLE handover_invitations (
            id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL,
            from_user_id INTEGER NOT NULL, to_user_id INTEGER NOT NULL,
            confirm_deadline TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending','confirmed','revoked','expired')),
            created_at TEXT NOT NULL, resolved_at TEXT);
        CREATE TABLE approvals (
            id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL,
            witness_id INTEGER NOT NULL, bound_digest TEXT NOT NULL,
            created_at TEXT NOT NULL, UNIQUE(request_id, witness_id));
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor_id INTEGER,
            action TEXT NOT NULL, document_id INTEGER, request_id INTEGER,
            detail TEXT NOT NULL);
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("SEAL_DB_PATH", str(db_path))
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "3600")
    from app.db import init_db

    init_db(str(db_path))  # 幂等迁移不报错

    from app.main import app

    with TestClient(app) as c:
        owner = register(c, "legacy-owner")
        for n in ("lg1", "lg2", "lg3"):
            register(c, n)
        d = seal(c, owner, "旧库迁移后的正文 LEGACY-BODY", ["lg1", "lg2", "lg3"])
        r = _schedule(c, owner, d["document_id"], 3600)
        assert r.status_code == 201
        got = c.get(f"/documents/{d['document_id']}", headers=auth(owner)).json()
        assert got["purge_schedule"]["status"] == "scheduled"
