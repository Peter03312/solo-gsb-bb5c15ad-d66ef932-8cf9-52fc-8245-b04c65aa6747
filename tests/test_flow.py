"""覆盖封存服务的核心规则。"""
import datetime as dt

from conftest import auth, digest_of, register, seal

BODY = "绝密资料：2026 年度继承人安排 v1"


def _deadline(minutes: int = 30) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).isoformat()


def _setup_four_users(client):
    owner = register(client, "owner")
    w1 = register(client, "w1")
    w2 = register(client, "w2")
    w3 = register(client, "w3")
    return owner, w1, w2, w3


def test_register_and_invalid_key(client):
    r = client.post("/register", json={"username": "alice"})
    assert r.status_code == 201
    key = r.json()["api_key"]
    assert len(key) == 64

    assert client.get("/me", headers=auth(key)).status_code == 200
    assert client.get("/me", headers=auth("deadbeef")).status_code == 401
    assert client.get("/me").status_code == 401

    # 重复注册拒绝
    assert client.post("/register", json={"username": "alice"}).status_code == 409


def test_seal_validation(client):
    owner = register(client, "owner")
    w1 = register(client, "w1")
    w2 = register(client, "w2")

    # 见证人不足 / 重复 / 含封存人
    for witnesses, code in [
        (["w1", "w2"], 422),                      # 少于 3 个
        (["w1", "w1", "w2"], 400),                # 不互异
        (["owner", "w1", "w2"], 400),             # 封存人不能当见证人
        (["w1", "w2", "ghost"], 400),             # 未注册
    ]:
        r = client.post("/documents", json={"body": BODY, "witnesses": witnesses}, headers=auth(owner))
        assert r.status_code == code, (witnesses, r.text)


def test_full_open_flow_with_two_approvals(client):
    owner, w1, w2, w3 = _setup_four_users(client)
    d = seal(client, owner, BODY, ["w1", "w2", "w3"])
    doc_id, dg = d["document_id"], d["digest"]

    # 开封前：正文绝不出现
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert "body" not in got and got["status"] == "sealed"
    assert client.get(f"/documents/{doc_id}", headers=auth(w1)).json().get("body") is None

    # 非参与方看不到
    outsider = register(client, "outsider")
    assert client.get(f"/documents/{doc_id}", headers=auth(outsider)).status_code == 403

    # 非封存人不能提交开封请求
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "家庭会议", "deadline": _deadline()},
        headers=auth(w1),
    )
    assert r.status_code == 403

    # 过期截止时刻拒绝
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "家庭会议", "deadline": _deadline(-5)},
        headers=auth(owner),
    )
    assert r.status_code == 400

    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "家庭会议需要开封", "deadline": _deadline()},
        headers=auth(owner),
    )
    assert r.status_code == 201, r.text
    req_id = r.json()["request_id"]

    # 非见证人不能同意
    r = client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(owner))
    assert r.status_code == 403
    r = client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(outsider))
    assert r.status_code == 403

    # 摘要不匹配拒绝，状态不变
    r = client.post(f"/open-requests/{req_id}/approve", json={"digest": digest_of("fake")}, headers=auth(w1))
    assert r.status_code == 409
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["latest_open_request"]["approval_count"] == 0

    # 第一名见证人同意 -> 仍封存
    r = client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(w1))
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert r.json()["approval_count"] == 1

    # 同一见证人重试只计一次，状态不变
    r = client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(w1))
    assert r.status_code == 409
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "sealed"
    assert got["latest_open_request"]["approval_count"] == 1

    # 第二名不同见证人同意 -> 开封
    r = client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(w2))
    assert r.status_code == 200 and r.json()["status"] == "opened"

    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "opened"
    assert got["body"] == BODY and got["digest"] == dg
    assert got["open_request"]["reason"] == "家庭会议需要开封"
    names = [a["username"] for a in got["witness_approvals"]]
    assert names == ["w1", "w2"]
    assert all(a["bound_digest"] == dg for a in got["witness_approvals"])

    # 开封后：重复开封、再同意、再请求都拒绝，且正文不可改
    assert client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "再来一次", "deadline": _deadline()},
        headers=auth(owner),
    ).status_code == 409
    assert client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(w3)).status_code == 409

    # 错误响应中不得出现正文
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "再来一次", "deadline": _deadline()},
        headers=auth(owner),
    )
    assert BODY not in r.text


def test_revoke_invalidates_old_approvals(client):
    owner, w1, w2, w3 = _setup_four_users(client)
    d = seal(client, owner, BODY, ["w1", "w2", "w3"])
    doc_id, dg = d["document_id"], d["digest"]

    # 单独封存一份资料来测撤销
    d2 = seal(client, owner, BODY + "2", ["w1", "w2", "w3"])
    doc2, dg2 = d2["document_id"], d2["digest"]
    r = client.post(
        f"/documents/{doc2}/open-requests",
        json={"reason": "待撤销", "deadline": _deadline()},
        headers=auth(owner),
    )
    req2 = r.json()["request_id"]
    client.post(f"/open-requests/{req2}/approve", json={"digest": dg2}, headers=auth(w1))

    # 见证人不能撤销
    assert client.post(f"/open-requests/{req2}/revoke", json={"reason": "x"}, headers=auth(w2)).status_code == 403
    client.post(f"/open-requests/{req2}/revoke", json={"reason": "改主意了"}, headers=auth(owner))

    # 旧同意不能再用：对 req2 同意被拒（请求已 revoked）
    r = client.post(f"/open-requests/{req2}/approve", json={"digest": dg2}, headers=auth(w2))
    assert r.status_code == 409
    got = client.get(f"/documents/{doc2}", headers=auth(owner)).json()
    assert got["status"] == "sealed"

    # 再次撤销已撤销的请求 -> 拒绝，状态不变
    assert client.post(f"/open-requests/{req2}/revoke", json={}, headers=auth(owner)).status_code == 409

    # 新请求需要重新收集两名同意
    r = client.post(
        f"/documents/{doc2}/open-requests",
        json={"reason": "重新申请", "deadline": _deadline()},
        headers=auth(owner),
    )
    req3 = r.json()["request_id"]
    got = client.get(f"/documents/{doc2}", headers=auth(owner)).json()
    assert got["latest_open_request"]["approval_count"] == 0
    client.post(f"/open-requests/{req3}/approve", json={"digest": dg2}, headers=auth(w1))
    r = client.post(f"/open-requests/{req3}/approve", json={"digest": dg2}, headers=auth(w2))
    assert r.json()["status"] == "opened"
    got = client.get(f"/documents/{doc2}", headers=auth(owner)).json()
    assert got["body"] == BODY + "2"


def test_expiry_invalidates_approvals(client, sweep):
    owner, w1, w2, w3 = _setup_four_users(client)
    d = seal(client, owner, BODY, ["w1", "w2", "w3"])
    doc_id, dg = d["document_id"], d["digest"]

    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "快到期", "deadline": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()},
        headers=auth(owner),
    )
    req = r.json()["request_id"]
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w1))

    import time

    time.sleep(1.1)
    # 后台扫描器可能已先行处理，这里再扫一次做确定性兜底；以最终状态为准
    sweep()

    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "sealed"
    assert got["latest_open_request"]["status"] == "expired"

    # 截止后同意拒绝、状态不变
    r = client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w2))
    assert r.status_code == 409
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "sealed"

    # 旧同意失效：可以提新请求且从 0 开始
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "新的一轮", "deadline": _deadline()},
        headers=auth(owner),
    )
    assert r.status_code == 201
    req2 = r.json()["request_id"]
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["latest_open_request"]["approval_count"] == 0
    client.post(f"/open-requests/{req2}/approve", json={"digest": dg}, headers=auth(w1))
    r = client.post(f"/open-requests/{req2}/approve", json={"digest": dg}, headers=auth(w2))
    assert r.json()["status"] == "opened"


def test_sealed_body_never_in_error_or_listing(client):
    owner, w1, w2, w3 = _setup_four_users(client)
    secret = "MARKER-SECRET-XYZ"
    d = seal(client, owner, secret, ["w1", "w2", "w3"])
    doc_id = d["document_id"]

    # 列表不含正文
    r = client.get("/documents", headers=auth(owner))
    assert secret not in r.text
    r = client.get("/documents", headers=auth(w1))
    assert secret not in r.text

    # 各类错误响应不含正文
    outsider = register(client, "oops")
    for resp in [
        client.get(f"/documents/{doc_id}", headers=auth(outsider)),
        client.get(f"/documents/{doc_id}"),
        client.post(
            f"/documents/{doc_id}/open-requests",
            json={"reason": "x", "deadline": _deadline()},
            headers=auth(outsider),
        ),
    ]:
        assert secret not in resp.text, resp.text


def test_validation_errors_never_echo_body(client):
    owner = register(client, "owner-b")
    secret = "MARKER-IN-BODY-9999"
    # 见证人个数不合法 -> 422，响应不得回显正文
    r = client.post(
        "/documents",
        json={"body": secret, "witnesses": ["x"]},
        headers=auth(owner),
    )
    assert r.status_code == 422
    assert secret not in r.text

    # 先正常封存一份，再用非法 deadline -> 422，理由不回显
    w = [register(client, f"v{i}") for i in range(3)]
    d = seal(client, owner, secret + "2", [f"v{i}" for i in range(3)])
    r = client.post(
        f"/documents/{d['document_id']}/open-requests",
        json={"reason": "MARKER-REASON-9999", "deadline": "not-a-time"},
        headers=auth(owner),
    )
    assert r.status_code == 422
    assert "MARKER-REASON-9999" not in r.text
    assert "MARKER-IN-BODY" not in r.text


def test_audit_trail_ordered_and_persistent(client):
    owner, w1, w2, w3 = _setup_four_users(client)
    d = seal(client, owner, BODY, ["w1", "w2", "w3"])
    doc_id, dg = d["document_id"], d["digest"]
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "审计", "deadline": _deadline()},
        headers=auth(owner),
    )
    req = r.json()["request_id"]
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w1))
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w2))

    records = client.get(f"/documents/{doc_id}/audit", headers=auth(owner)).json()["records"]
    actions = [r["action"] for r in records]
    assert actions == ["seal", "open_request_created", "approval", "approval", "opened"]
    ids = [r["id"] for r in records]
    assert ids == sorted(ids)

    # 非参与方不可看审计
    outsider = register(client, "spy")
    assert client.get(f"/documents/{doc_id}/audit", headers=auth(outsider)).status_code == 403
