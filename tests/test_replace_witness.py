"""封存后替换指定见证人功能测试。

规则要点（规格）：
- 资料未开封时，封存人可提交“被替换者 + 新见证人账号”完成替换；
- 新见证人须已注册、非封存人、不在当前名单；替换后仍为三名不同见证人；
- 有进行中的开封请求时：替换须在截止前完成；被替换者对本次请求的同意失效，
  其他同意保留；有效票不足两票时待开封请求退回待同意（waiting -> pending），
  新见证人凭封存摘要为本次请求投票；
- 到达最早开封时刻且两票已齐的请求须先开封并拒绝替换；
- 已过截止时刻（过期）的请求按原规则失效，随后替换照常完成；
- 旧见证人随即失去资料访问和同意权限；
- 替换结果返回当前名单、请求状态与有效票数，变更留审计记录；
- 其他拒绝不改状态且不泄露正文；现有封存/开封/查询接口保持兼容。
"""
import datetime as dt
import time

import pytest

from conftest import auth, register, seal

BODY = "替换见证人绝密资料 REPLACE-WITNESS-SECRET"


def _deadline(minutes: int = 30) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).isoformat()


def _setup(client, names=("owner", "w1", "w2", "w3", "w4")):
    keys = {n: register(client, n) for n in names}
    d = seal(client, keys["owner"], BODY, ["w1", "w2", "w3"])
    return keys, d["document_id"], d["digest"]


def _replace(client, owner_key, doc_id, old, new):
    return client.post(
        f"/documents/{doc_id}/witness-replacements",
        json={"old_witness": old, "new_witness": new},
        headers=auth(owner_key),
    )


def _create_request(client, owner_key, doc_id, *, deadline_min=30, earliest=None, reason="替换测试"):
    payload = {"reason": reason, "deadline": _deadline(deadline_min)}
    if earliest is not None:
        payload["earliest_open_at"] = (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=earliest)
        ).isoformat()
    r = client.post(
        f"/documents/{doc_id}/open-requests", json=payload, headers=auth(owner_key)
    )
    assert r.status_code == 201, r.text
    return r.json()["request_id"]


def _approve(client, key, req, dg):
    return client.post(
        f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(key)
    )


def _witnesses(client, owner_key, doc_id):
    return client.get(f"/documents/{doc_id}", headers=auth(owner_key)).json()["witnesses"]


# ---------------------------------------------------------------------------
# 无进行中请求：直接换名单
# ---------------------------------------------------------------------------


def test_replace_without_active_request(client):
    keys, doc_id, _ = _setup(client)
    r = _replace(client, keys["owner"], doc_id, "w3", "w4")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "sealed"
    assert data["witnesses"] == ["w1", "w2", "w4"]
    assert data["open_request"] is None

    # 查询接口名单同步更新（现有查询接口兼容）
    assert _witnesses(client, keys["owner"], doc_id) == ["w1", "w2", "w4"]

    # 旧见证人立即失去访问权，新见证人获得访问权
    assert client.get(f"/documents/{doc_id}", headers=auth(keys["w3"])).status_code == 403
    assert client.get(f"/documents/{doc_id}", headers=auth(keys["w4"])).status_code == 200

    # 列表不再出现在旧见证人侧
    assert str(doc_id) not in client.get("/documents", headers=auth(keys["w3"])).text

    # 审计留痕
    records = client.get(f"/documents/{doc_id}/audit", headers=auth(keys["owner"])).json()["records"]
    last = records[-1]
    assert last["action"] == "witness_replaced"
    assert "w3" in last["detail"] and "w4" in last["detail"]


def test_replace_validation_and_no_state_change(client):
    keys, doc_id, _ = _setup(client)

    # 新见证人未注册 -> 400，名单不变
    r = _replace(client, keys["owner"], doc_id, "w1", "ghost")
    assert r.status_code == 400
    assert _witnesses(client, keys["owner"], doc_id) == ["w1", "w2", "w3"]

    # 被替换者不在当前名单 -> 404
    r = _replace(client, keys["owner"], doc_id, "w4", "w1")
    assert r.status_code == 404

    # 新见证人即封存人 -> 400
    r = _replace(client, keys["owner"], doc_id, "w1", "owner")
    assert r.status_code == 400

    # 新见证人已在当前名单（含与被替换者相同）-> 409
    r = _replace(client, keys["owner"], doc_id, "w1", "w2")
    assert r.status_code == 409
    r = _replace(client, keys["owner"], doc_id, "w1", "w1")
    assert r.status_code == 409
    assert _witnesses(client, keys["owner"], doc_id) == ["w1", "w2", "w3"]

    # 非封存人（含见证人本人、外人）-> 403
    assert _replace(client, keys["w1"], doc_id, "w1", "w4").status_code == 403
    outsider = register(client, "outsider")
    assert _replace(client, outsider, doc_id, "w1", "w4").status_code == 403

    # 缺少/错误密钥 -> 401
    r = client.post(
        f"/documents/{doc_id}/witness-replacements",
        json={"old_witness": "w1", "new_witness": "w4"},
    )
    assert r.status_code == 401
    r = client.post(
        f"/documents/{doc_id}/witness-replacements",
        json={"old_witness": "w1", "new_witness": "w4"},
        headers=auth("deadbeef"),
    )
    assert r.status_code == 401

    # 参数缺失 -> 422，且不回显任何正文
    r = client.post(
        f"/documents/{doc_id}/witness-replacements",
        json={"old_witness": "w1"},
        headers=auth(keys["owner"]),
    )
    assert r.status_code == 422

    # 非参与方访问不存在的资料也统一 403
    assert _replace(client, outsider, 999, "x", "y").status_code == 403


def test_replace_never_leaks_body(client):
    keys, doc_id, _ = _setup(client)
    for resp in [
        _replace(client, keys["owner"], doc_id, "w1", "ghost"),
        _replace(client, keys["owner"], doc_id, "w4", "w1"),
        _replace(client, keys["owner"], doc_id, "w1", "w2"),
        _replace(client, keys["w1"], doc_id, "w1", "w4"),
        client.post(
            f"/documents/{doc_id}/witness-replacements",
            json={"old_witness": "w1"},
            headers=auth(keys["owner"]),
        ),
    ]:
        assert BODY not in resp.text, resp.text


# ---------------------------------------------------------------------------
# 进行中的 pending 请求：被替换者的票失效、其他票保留、可补票
# ---------------------------------------------------------------------------


def test_replace_removes_outgoing_vote_but_keeps_others(client):
    keys, doc_id, dg = _setup(client)
    req = _create_request(client, keys["owner"], doc_id)
    # w2 先投一票
    assert _approve(client, keys["w2"], req, dg).json()["approval_count"] == 1

    # 替换已投票的 w2 -> w4：该票失效，有效票清零，请求退回待同意
    r = _replace(client, keys["owner"], doc_id, "w2", "w4")
    assert r.status_code == 200, r.text
    info = r.json()["open_request"]
    assert info == {"request_id": req, "status": "pending", "approval_count": 0}
    assert r.json()["witnesses"] == ["w1", "w4", "w3"]

    got = client.get(f"/documents/{doc_id}", headers=auth(keys["owner"])).json()
    assert got["status"] == "sealed"
    assert got["latest_open_request"]["approval_count"] == 0

    # 旧见证人 w2 立即失去同意权限（403，不写票）
    assert _approve(client, keys["w2"], req, dg).status_code == 403

    # 新见证人 w4 凭封存摘要投票；与保留的 w1 一起完成两票开封
    assert _approve(client, keys["w4"], req, dg).json()["approval_count"] == 1
    r = _approve(client, keys["w1"], req, dg)
    assert r.status_code == 200 and r.json()["status"] == "opened"
    got = client.get(f"/documents/{doc_id}", headers=auth(keys["owner"])).json()
    assert got["body"] == BODY
    names = [a["username"] for a in got["witness_approvals"]]
    assert names == ["w4", "w1"]  # w2 的票已删除，不计入开封见证


def test_replace_non_voting_witness_keeps_pending_votes(client):
    keys, doc_id, dg = _setup(client)
    req = _create_request(client, keys["owner"], doc_id)
    _approve(client, keys["w1"], req, dg)  # 只有 w1 投票
    # 替换未投票的 w3 -> w4：w1 的票保留
    r = _replace(client, keys["owner"], doc_id, "w3", "w4")
    assert r.status_code == 200
    assert r.json()["open_request"] == {
        "request_id": req, "status": "pending", "approval_count": 1
    }
    # 新见证人 w4 投票即两票开封
    assert _approve(client, keys["w4"], req, dg).json()["status"] == "opened"


# ---------------------------------------------------------------------------
# waiting（两票集齐、待到点）：替换投票者 -> 退回 pending；替换未投票者 -> 维持 waiting
# ---------------------------------------------------------------------------


def test_replace_voter_during_waiting_downgrades_to_pending(client):
    keys, doc_id, dg = _setup(client)
    req = _create_request(client, keys["owner"], doc_id, deadline_min=120, earliest=3600)
    assert _approve(client, keys["w1"], req, dg).json()["status"] == "pending"
    assert _approve(client, keys["w2"], req, dg).json()["status"] == "waiting"

    # 替换投票者 w2：带走一票 -> 退回 pending，w1 的票保留
    r = _replace(client, keys["owner"], doc_id, "w2", "w4")
    assert r.status_code == 200, r.text
    assert r.json()["open_request"] == {
        "request_id": req, "status": "pending", "approval_count": 1
    }
    got = client.get(f"/documents/{doc_id}", headers=auth(keys["owner"])).json()
    assert got["latest_open_request"]["status"] == "pending"
    assert got["latest_open_request"]["approval_count"] == 1
    assert "body" not in got

    # 旧 w2 不能再投票；新 w4 凭摘要补票 -> 重新 waiting（最早时刻未到）
    assert _approve(client, keys["w2"], req, dg).status_code == 403
    r = _approve(client, keys["w4"], req, dg)
    assert r.json()["status"] == "waiting" and r.json()["approval_count"] == 2

    records = client.get(f"/documents/{doc_id}/audit", headers=auth(keys["owner"])).json()["records"]
    actions = [r["action"] for r in records]
    assert "witness_replaced" in actions
    # waiting -> pending 不产生 opened/expired/revoked
    assert "opened" not in actions and "request_expired" not in actions


def test_replace_non_voter_during_waiting_keeps_waiting(client):
    keys, doc_id, dg = _setup(client)
    req = _create_request(client, keys["owner"], doc_id, deadline_min=120, earliest=3600)
    _approve(client, keys["w1"], req, dg)
    _approve(client, keys["w2"], req, dg)  # 未投票者是 w3
    r = _replace(client, keys["owner"], doc_id, "w3", "w4")
    assert r.status_code == 200
    assert r.json()["open_request"] == {
        "request_id": req, "status": "waiting", "approval_count": 2
    }
    got = client.get(f"/documents/{doc_id}", headers=auth(keys["owner"])).json()
    assert got["latest_open_request"]["status"] == "waiting"


# ---------------------------------------------------------------------------
# 到点必开封：已到最早开封时刻且两票齐 -> 先开封，拒绝替换，名单不变
# ---------------------------------------------------------------------------


@pytest.fixture
def passive_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SEAL_DB_PATH", str(tmp_path / "passive-replace.db"))
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "3600")
    from app.main import app

    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


def _wait_until(ts_iso: str) -> None:
    target = dt.datetime.fromisoformat(ts_iso)
    while dt.datetime.now(dt.timezone.utc) < target:
        time.sleep(0.01)


def test_due_waiting_opens_first_and_rejects_replacement(passive_client):
    c = passive_client
    keys, doc_id, dg = _setup(c)
    resp = c.post(
        f"/documents/{doc_id}/open-requests",
        json={
            "reason": "到点替换赛跑",
            "deadline": _deadline(60),
            "earliest_open_at": (
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)
            ).isoformat(),
        },
        headers=auth(keys["owner"]),
    ).json()
    req = resp["request_id"]
    _approve(c, keys["w1"], req, dg)
    _approve(c, keys["w2"], req, dg)
    assert c.get(f"/documents/{doc_id}", headers=auth(keys["owner"])).json()[
        "latest_open_request"]["status"] == "waiting"

    _wait_until(resp["earliest_open_at"])
    time.sleep(0.05)
    # 后台不扫描：替换事务必须先确定性开封，再拒绝替换
    r = _replace(c, keys["owner"], doc_id, "w3", "w4")
    assert r.status_code == 409, r.text
    assert BODY not in r.text

    got = c.get(f"/documents/{doc_id}", headers=auth(keys["owner"])).json()
    assert got["status"] == "opened" and got["body"] == BODY
    assert got["witnesses"] == ["w1", "w2", "w3"]          # 名单未变
    assert c.get(f"/documents/{doc_id}", headers=auth(keys["w4"])).status_code == 403

    records = c.get(f"/documents/{doc_id}/audit", headers=auth(keys["owner"])).json()["records"]
    actions = [r["action"] for r in records]
    assert "witness_replaced" not in actions
    assert actions == ["seal", "open_request_created", "approval", "approval",
                       "request_waiting", "opened"]


# ---------------------------------------------------------------------------
# 已过截止时刻：进行中的请求按原规则失效，替换随后完成
# ---------------------------------------------------------------------------


def test_past_deadline_request_expires_then_replacement_completes(passive_client):
    # 禁用后台扫描：确定性验证“替换写事务内把过截止的 pending 按原规则失效”
    c = passive_client
    keys, doc_id, dg = _setup(c)
    deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()
    req = c.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "过期替换", "deadline": deadline},
        headers=auth(keys["owner"]),
    ).json()["request_id"]
    _approve(c, keys["w1"], req, dg)
    time.sleep(1.15)

    # 替换临界区内按原规则把过截止的 pending 置 expired，再完成替换
    r = _replace(c, keys["owner"], doc_id, "w2", "w4")
    assert r.status_code == 200, r.text
    assert r.json()["witnesses"] == ["w1", "w4", "w3"]
    assert r.json()["open_request"]["status"] == "expired"
    assert r.json()["open_request"]["approval_count"] == 0

    got = c.get(f"/documents/{doc_id}", headers=auth(keys["owner"])).json()
    assert got["latest_open_request"]["status"] == "expired"

    records = c.get(f"/documents/{doc_id}/audit", headers=auth(keys["owner"])).json()["records"]
    actions = [r["action"] for r in records]
    assert actions[-2] == "request_expired"
    assert actions[-1] == "witness_replaced"

    # 旧票随失效作废：新请求从 0 票开始，新见证人可参与
    req2 = _create_request(c, keys["owner"], doc_id)
    assert c.get(f"/documents/{doc_id}", headers=auth(keys["owner"])).json()[
        "latest_open_request"]["approval_count"] == 0
    _approve(c, keys["w1"], req2, dg)
    assert _approve(c, keys["w4"], req2, dg).json()["status"] == "opened"


# ---------------------------------------------------------------------------
# 其他兼容与审计
# ---------------------------------------------------------------------------


def test_replace_persists_across_restart(passive_client):
    import sqlite3

    c = passive_client
    keys, doc_id, dg = _setup(c)
    req = _create_request(c, keys["owner"], doc_id, deadline_min=120, earliest=3600)
    _approve(c, keys["w1"], req, dg)
    _approve(c, keys["w2"], req, dg)
    r = _replace(c, keys["owner"], doc_id, "w2", "w4")
    assert r.json()["open_request"]["status"] == "pending"

    import os

    db_path = os.environ["SEAL_DB_PATH"]
    # 冷读库：名单已是新名单、请求回到 pending、w2 的同意行已删除
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    names = [
        row[0]
        for row in conn.execute(
            """SELECT u.username FROM document_witnesses w JOIN users u ON u.id=w.user_id
               WHERE w.document_id=? ORDER BY w.position""",
            (doc_id,),
        ).fetchall()
    ]
    assert names == ["w1", "w4", "w3"]
    status = conn.execute(
        "SELECT status FROM open_requests WHERE id=?", (req,)
    ).fetchone()[0]
    assert status == "pending"
    cnt = conn.execute(
        "SELECT COUNT(*) FROM approvals WHERE request_id=?", (req,)
    ).fetchone()[0]
    assert cnt == 1
    conn.close()


def test_replace_works_when_only_terminal_requests_exist(client):
    """上一轮请求已 revoked/expired/opened-for-another-doc 时替换照常进行。"""
    keys, doc_id, dg = _setup(client)
    req = _create_request(client, keys["owner"], doc_id)
    _approve(client, keys["w1"], req, dg)
    client.post(
        f"/open-requests/{req}/revoke", json={"reason": "算了"}, headers=auth(keys["owner"])
    )
    r = _replace(client, keys["owner"], doc_id, "w2", "w4")
    assert r.status_code == 200
    assert r.json()["witnesses"] == ["w1", "w4", "w3"]
    assert r.json()["open_request"] is None          # 无进行中请求
    # 历史审计（撤销记录）仍在
    records = client.get(f"/documents/{doc_id}/audit", headers=auth(keys["owner"])).json()["records"]
    assert "revoked" in [r["action"] for r in records]


# ---------------------------------------------------------------------------
# 并发：替换见证人 vs 第二票同意 —— 全部在 BEGIN IMMEDIATE 临界区内，
# 只有一个已提交终态（要么第二票开封，要么替换生效后由新名单补票）。
# ---------------------------------------------------------------------------


def test_concurrent_replace_vs_approve_single_outcome(tmp_path, monkeypatch):
    import socket
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import httpx
    import uvicorn

    monkeypatch.setenv("SEAL_DB_PATH", str(tmp_path / "concurrent-replace.db"))
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "60")
    from app.main import app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
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
        raise AssertionError("server did not start")

    try:
        with httpx.Client(timeout=10) as c:
            def reg(n):
                return c.post(base + "/register", json={"username": n}).json()["api_key"]

            owner, w1, w2, w3, w4 = (reg(n) for n in ("cowner", "cw1", "cw2", "cw3", "cw4"))
            body = "并发替换正文 CONCURRENT-REPLACE-SECRET"
            import hashlib

            dg = hashlib.sha256(body.encode()).hexdigest()
            doc_id = c.post(
                base + "/documents", json={"body": body, "witnesses": ["cw1", "cw2", "cw3"]},
                headers={"X-API-Key": owner},
            ).json()["document_id"]
            deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat()
            req_id = c.post(
                f"{base}/documents/{doc_id}/open-requests",
                json={"reason": "并发赛跑", "deadline": deadline},
                headers={"X-API-Key": owner},
            ).json()["request_id"]
            # w1 先投一票；第二票（w2）与替换 w2->w4 同时发生
            assert c.post(
                f"{base}/open-requests/{req_id}/approve", json={"digest": dg},
                headers={"X-API-Key": w1},
            ).status_code == 200

        barrier = threading.Barrier(2)

        def approve_w2():
            barrier.wait()
            with httpx.Client(timeout=10) as c:
                return c.post(
                    f"{base}/open-requests/{req_id}/approve", json={"digest": dg},
                    headers={"X-API-Key": w2},
                ).status_code

        def replace_w2():
            barrier.wait()
            with httpx.Client(timeout=10) as c:
                r = c.post(
                    f"{base}/documents/{doc_id}/witness-replacements",
                    json={"old_witness": "cw2", "new_witness": "cw4"},
                    headers={"X-API-Key": owner},
                )
                return r.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            ap_code, rp_code = list(pool.map(lambda f: f(), [approve_w2, replace_w2]))

        with httpx.Client(timeout=10) as c:
            h = {"X-API-Key": owner}
            got = c.get(f"{base}/documents/{doc_id}", headers=h).json()
            witnesses = got["witnesses"]
            records = c.get(f"{base}/documents/{doc_id}/audit", headers=h).json()["records"]
            opened = [r for r in records if r["action"] == "opened"]
            replaced = [r for r in records if r["action"] == "witness_replaced"]

            if got["status"] == "opened":
                # 第二票先提交并开封：替换必须被拒绝，名单不变，开封恰好一次
                assert ap_code == 200
                assert rp_code == 409
                assert witnesses == ["cw1", "cw2", "cw3"]
                assert len(opened) == 1 and not replaced
                assert got["body"] == body
            else:
                # 替换先提交：名单已换，w2 的同意（即便已落库）随之删除，
                # 请求退回 pending 且恰好保留 w1 一票；w2 此后无同意权。
                assert rp_code == 200
                assert witnesses == ["cw1", "cw4", "cw3"]
                assert len(replaced) == 1 and not opened
                info = c.get(f"{base}/documents/{doc_id}", headers=h).json()["latest_open_request"]
                assert info["status"] == "pending"
                assert info["approval_count"] == 1
                assert c.post(
                    f"{base}/open-requests/{req_id}/approve", json={"digest": dg},
                    headers={"X-API-Key": w2},
                ).status_code == 403
                # w4 作为新见证人凭摘要补第二票 -> 与保留的 w1 一起开封
                r = c.post(
                    f"{base}/open-requests/{req_id}/approve", json={"digest": dg},
                    headers={"X-API-Key": w4},
                )
                assert r.status_code == 200 and r.json()["status"] == "opened"
                assert c.get(f"{base}/documents/{doc_id}", headers=h).json()["body"] == body

            # 开封/替换审计各自至多一次，且互斥（本轮只产生一个终态）
            assert len(opened) + len(replaced) == 1
    finally:
        server.should_exit = True
        thread.join(timeout=5)
