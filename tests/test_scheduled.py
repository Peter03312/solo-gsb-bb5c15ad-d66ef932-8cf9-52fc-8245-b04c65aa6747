"""最早开封时刻（earliest_open_at）功能测试。

规则要点：
- 可选带时区字段，不早于提交时刻且早于截止时刻；不填行为与旧版完全一致；
- 两票在该时刻前集齐只进入 waiting（待开封），查询显示时刻与状态、不含正文；
- 到点自动开封；到点时不足两票的，截止前补齐第二票时立即开封；
- 到点前未撤销且已集齐两票的请求自到点起必开封：即使后台延迟，
  之后的撤销或到期扫描也不能使其失效；
- 到点前可撤销；截止前始终不足两票则 expired；旧票不能复用；
- 并发同意/撤销/定时开封/到期只产生一个终态，重启后一致。
"""
import datetime as dt
import time

import pytest
from fastapi.testclient import TestClient

from conftest import auth, digest_of, register, seal

BODY = "定时开封绝密资料 SCHEDULED-SECRET"


def _iso(seconds: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat()


def _setup(client):
    owner = register(client, "sowner")
    w1 = register(client, "sw1")
    w2 = register(client, "sw2")
    w3 = register(client, "sw3")
    d = seal(client, owner, BODY, ["sw1", "sw2", "sw3"])
    return owner, w1, w2, w3, d["document_id"], d["digest"]


def _create(client, owner, doc_id, *, earliest=None, deadline_in=60, reason="定时"):
    payload = {"reason": reason, "deadline": _iso(deadline_in)}
    if earliest is not None:
        payload["earliest_open_at"] = _iso(earliest) if isinstance(earliest, (int, float)) else earliest
    r = client.post(
        f"/documents/{doc_id}/open-requests", json=payload, headers=auth(owner)
    )
    return r


def _two_approvals(client, req, dg, w1, w2):
    r1 = client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w1))
    r2 = client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w2))
    return r1, r2


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------


def test_earliest_validation(client):
    owner, w1, w2, w3, doc_id, _ = _setup(client)

    # earliest 早于提交时刻 -> 400
    r = _create(client, owner, doc_id, earliest=-10, deadline_in=60)
    assert r.status_code == 400, r.text
    assert BODY not in r.text

    # earliest 等于 deadline -> 422（必须严格早于）
    same = _iso(60)
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "x", "deadline": same, "earliest_open_at": same},
        headers=auth(owner),
    )
    assert r.status_code == 422, r.text

    # earliest 晚于 deadline -> 422
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "x", "deadline": _iso(10), "earliest_open_at": _iso(20)},
        headers=auth(owner),
    )
    assert r.status_code == 422

    # earliest 不带时区 -> 422，且不回显内容
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "x", "deadline": _iso(60), "earliest_open_at": "2030-01-01T00:00:00"},
        headers=auth(owner),
    )
    assert r.status_code == 422


def test_without_earliest_is_backward_compatible(client):
    owner, w1, w2, w3, doc_id, dg = _setup(client)
    r = _create(client, owner, doc_id)
    assert r.status_code == 201
    data = r.json()
    assert data["status"] == "pending" and data["earliest_open_at"] is None

    r1, r2 = _two_approvals(client, data["request_id"], dg, w1, w2)
    assert r1.json()["status"] == "pending"
    assert "earliest_open_at" not in r1.json()
    assert r2.json()["status"] == "opened"
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["body"] == BODY


# ---------------------------------------------------------------------------
# waiting 状态
# ---------------------------------------------------------------------------


def test_two_approvals_before_earliest_enter_waiting(client):
    owner, w1, w2, w3, doc_id, dg = _setup(client)
    r = _create(client, owner, doc_id, earliest=3600, deadline_in=7200)
    assert r.status_code == 201
    req = r.json()["request_id"]
    assert r.json()["earliest_open_at"] is not None

    r1, r2 = _two_approvals(client, req, dg, w1, w2)
    assert r1.status_code == 200 and r1.json()["status"] == "pending"
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "waiting"
    assert r2.json()["approval_count"] == 2
    assert r2.json()["earliest_open_at"] == r.json()["earliest_open_at"]

    # 查询：显示 waiting + earliest_open_at，但无正文、无见证明细
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    info = got["latest_open_request"]
    assert got["status"] == "sealed" and info["status"] == "waiting"
    assert info["earliest_open_at"] is not None and info["approval_count"] == 2
    assert "body" not in got and "witness_approvals" not in got

    # 见证人也看不到正文
    assert "body" not in client.get(f"/documents/{doc_id}", headers=auth(w1)).json()

    # waiting 下任何同意（含第三见证人、重复）均 409
    for key in (w1, w3):
        rr = client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(key))
        assert rr.status_code == 409

    # waiting 下不能再提新请求
    assert _create(client, owner, doc_id, earliest=3700, deadline_in=7300).status_code == 409


def test_waiting_does_not_leak_body_in_errors(client):
    owner, w1, w2, w3, doc_id, dg = _setup(client)
    req = _create(client, owner, doc_id, earliest=3600, deadline_in=7200).json()["request_id"]
    _two_approvals(client, req, dg, w1, w2)

    outsider = register(client, "snoop")
    for resp in [
        client.get(f"/documents/{doc_id}", headers=auth(outsider)),
        client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(outsider)),
        client.post(f"/open-requests/{req}/revoke", json={}, headers=auth(w1)),
    ]:
        assert resp.status_code == 403
        assert BODY not in resp.text


# ---------------------------------------------------------------------------
# 到点开封
# ---------------------------------------------------------------------------


def test_waiting_opens_at_earliest_via_sweeper(client, sweep):
    owner, w1, w2, w3, doc_id, dg = _setup(client)
    req = _create(client, owner, doc_id, earliest=1, deadline_in=3600).json()["request_id"]
    _two_approvals(client, req, dg, w1, w2)

    time.sleep(1.2)
    assert sweep() == 0  # 本轮没有 expired，只有自动开封

    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "opened" and got["body"] == BODY
    assert got["open_request"]["status"] == "opened"

    # 开封后再撤销/同意均拒绝，且审计只开封一次
    assert client.post(f"/open-requests/{req}/revoke", json={}, headers=auth(owner)).status_code == 409
    records = client.get(f"/documents/{doc_id}/audit", headers=auth(owner)).json()["records"]
    actions = [r["action"] for r in records]
    assert actions == ["seal", "open_request_created", "approval", "approval",
                       "request_waiting", "opened"]


def test_late_second_vote_after_earliest_opens_immediately(client, sweep):
    owner, w1, w2, w3, doc_id, dg = _setup(client)
    req = _create(client, owner, doc_id, earliest=1, deadline_in=3600).json()["request_id"]
    # earliest 之前只集齐一票
    assert client.post(
        f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w1)
    ).json()["status"] == "pending"
    time.sleep(1.2)
    sweep()  # 到点但票数不足：既不开封也不过期

    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "sealed"
    assert got["latest_open_request"]["status"] == "pending"

    # 截止前补齐第二票：立即开封（无需再等扫描器）
    r = client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w2))
    assert r.status_code == 200 and r.json()["status"] == "opened"
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["body"] == BODY


def test_insufficient_votes_expires_at_deadline_even_after_earliest(client, sweep):
    from app.main import ExpirySweeper

    owner, w1, w2, w3, doc_id, dg = _setup(client)
    # earliest 1s、deadline 2s；到点时只有一票
    req = _create(client, owner, doc_id, earliest=1, deadline_in=2).json()["request_id"]
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w1))
    # 等到 1s<now<2s：到点但票不足，扫描器不应开封也不应过期
    time.sleep(1.2)
    sweep()
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "sealed"
    assert got["latest_open_request"]["status"] == "pending"

    time.sleep(1.0)
    ExpirySweeper.sweep_once()  # 过截止时刻仍 pending -> expired
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "sealed"
    assert got["latest_open_request"]["status"] == "expired"

    # 旧票作废：新请求从 0 票开始
    r = _create(client, owner, doc_id, earliest=1, deadline_in=3600)
    assert r.status_code == 201
    req2 = r.json()["request_id"]
    assert client.get(f"/documents/{doc_id}", headers=auth(owner)).json()[
        "latest_open_request"]["approval_count"] == 0
    r1, r2 = _two_approvals(client, req2, dg, w1, w2)
    assert r2.json()["status"] == "waiting"


# ---------------------------------------------------------------------------
# 到点必开封：撤销 / 到期扫描都不能作废（确定性，无后台扫描干扰）
# ---------------------------------------------------------------------------


@pytest.fixture
def passive_client(tmp_path, monkeypatch):
    """后台扫描间隔调大，保证测试期间不自动扫描，只靠显式触发与写事务判定。"""
    monkeypatch.setenv("SEAL_DB_PATH", str(tmp_path / "passive.db"))
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "3600")
    from app.main import app

    with TestClient(app) as c:
        yield c


def _wait_until(ts_iso: str) -> None:
    target = dt.datetime.fromisoformat(ts_iso)
    while dt.datetime.now(dt.timezone.utc) < target:
        time.sleep(0.01)


def test_due_waiting_survives_late_revoke(passive_client):
    """到点后后台尚未处理，封存人撤销：撤销必须失败，请求在同事务内开封。"""
    from app.main import ExpirySweeper

    c = passive_client
    owner, w1, w2, w3, doc_id, dg = _setup(c)
    resp = _create(c, owner, doc_id, earliest=1, deadline_in=2)
    req = resp.json()["request_id"]
    deadline_iso = resp.json()["deadline"]
    _two_approvals(c, req, dg, w1, w2)
    assert c.get(f"/documents/{doc_id}", headers=auth(owner)).json()[
        "latest_open_request"]["status"] == "waiting"

    # 越过截止时刻仍不扫描（模拟后台处理延迟），然后撤销
    _wait_until(deadline_iso)
    time.sleep(0.05)
    r = c.post(f"/open-requests/{req}/revoke", json={"reason": "迟来的撤销"}, headers=auth(owner))
    assert r.status_code == 409, r.text
    got = c.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "opened" and got["body"] == BODY
    assert got["open_request"]["status"] == "opened"

    # 之后到期扫描也不得失效：返回 0 条 expired，状态保持 opened
    assert ExpirySweeper.sweep_once() == 0
    got = c.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "opened"
    records = c.get(f"/documents/{doc_id}/audit", headers=auth(owner)).json()["records"]
    assert [r["action"] for r in records].count("opened") == 1
    assert "revoked" not in [r["action"] for r in records]
    assert "request_expired" not in [r["action"] for r in records]


def test_due_waiting_survives_late_sweep(passive_client):
    """越过 earliest 甚至 deadline 后扫描器才跑一轮：waiting 必须开封而非过期。"""
    from app.main import ExpirySweeper

    c = passive_client
    owner, w1, w2, w3, doc_id, dg = _setup(c)
    resp = _create(c, owner, doc_id, earliest=1, deadline_in=2)
    req = resp.json()["request_id"]
    _two_approvals(c, req, dg, w1, w2)

    _wait_until(resp.json()["deadline"])
    time.sleep(0.05)
    assert ExpirySweeper.sweep_once() == 0  # 没有请求被 expired
    got = c.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "opened" and got["body"] == BODY


def test_revoke_before_earliest_invalidates_votes(passive_client):
    """到点前撤销沿用现有撤销规则，旧票不能复用。"""
    c = passive_client
    owner, w1, w2, w3, doc_id, dg = _setup(c)
    req = _create(c, owner, doc_id, earliest=3600, deadline_in=7200).json()["request_id"]
    r1, _ = _two_approvals(c, req, dg, w1, w2)
    assert r1.status_code == 200

    r = c.post(f"/open-requests/{req}/revoke", json={"reason": "改主意"}, headers=auth(owner))
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    # 撤销后旧同意被拒
    assert c.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w3)
                  ).status_code == 409

    # 新请求重新从 0 票开始
    req2 = _create(c, owner, doc_id, earliest=3600, deadline_in=7200).json()["request_id"]
    got = c.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["latest_open_request"]["approval_count"] == 0
    r1, r2 = _two_approvals(c, req2, dg, w1, w2)
    assert r2.json()["status"] == "waiting"
