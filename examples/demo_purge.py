#!/usr/bin/env python3
"""到期清除正文（scheduled purge）端到端示例（纯标准库，无需第三方依赖）。

覆盖：
1) 安排清除时刻（PUT /documents/{id}/purge）-> 改期 -> 取消 -> 重新安排；
2) 到点清除：状态 purged、返回清除时刻，摘要/参与方/审计保留，正文不再返回；
3) 已开封资料仍按期清除；
4) 未开封资料清除时：进行中开封请求失效、旧同意作废、待确认交接邀请失效；
5) 清除与最早开封同刻时清除优先；开封更早则先开封再清除；
6) 已清除资料不能再开封/替换见证人/交接/再安排清除；到期的改期与取消被拒；
7) 交接后由新封存人管理清除安排，原封存人无权操作。

用法: python examples/demo_purge.py [base_url]
"""
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method: str, path: str, key: str | None = None, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", **({"X-API-Key": key} if key else {})},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def in_hours(h: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=h)).isoformat()


def in_seconds(s: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=s)).isoformat()


def show(title: str, status: int, body) -> None:
    print(f"-- {title} -> {status}")
    print("   " + json.dumps(body, ensure_ascii=False))


def wait_purged(key, doc_id, timeout=10):
    """轮询直到资料呈现已清除状态（后台扫描器默认每秒处理一轮）。"""
    end = time.time() + timeout
    while time.time() < end:
        status, got = call("GET", f"/documents/{doc_id}", key)
        if got.get("status") == "purged":
            return got
        time.sleep(0.2)
    raise AssertionError("资料未在预期时间内清除")


def main() -> None:
    users = {}
    for name in ["p-alice", "p-bob", "p-carol", "p-dave", "p-erin"]:
        _, body = call("POST", "/register", payload={"username": name})
        users[name] = body["api_key"]
    alice, bob, carol, dave, erin = (
        users[n] for n in ["p-alice", "p-bob", "p-carol", "p-dave", "p-erin"]
    )

    # ------------------------------------------------------------------
    # 场景 1：安排 -> 改期 -> 取消 -> 重新安排
    # ------------------------------------------------------------------
    print("== 场景 1：安排清除时刻、改期、取消、重新安排 ==")
    _, sealed = call("POST", "/documents", alice,
                     {"body": "清除示例正文：PURGE-DEMO-SECRET",
                      "witnesses": ["p-bob", "p-carol", "p-dave"]})
    doc1, digest1 = sealed["document_id"], sealed["digest"]

    status, resp = call("PUT", f"/documents/{doc1}/purge", alice, {"purge_at": in_hours(1)})
    show("安排 1 小时后清除", status, resp)
    assert status == 200 and resp["purge_at"] and resp["previous_purge_at"] is None

    status, resp = call("PUT", f"/documents/{doc1}/purge", alice, {"purge_at": in_hours(2)})
    show("改期到 2 小时后", status, resp)
    assert status == 200 and resp["previous_purge_at"] is not None

    status, resp = call("DELETE", f"/documents/{doc1}/purge", alice)
    show("取消清除", status, resp)
    assert status == 200 and resp["purge_at"] is None
    assert call("DELETE", f"/documents/{doc1}/purge", alice)[0] == 409  # 无安排可取消

    # 拒绝示例：非封存人 403；过去时刻 400；不带时区 422
    assert call("PUT", f"/documents/{doc1}/purge", bob, {"purge_at": in_hours(1)})[0] == 403
    assert call("PUT", f"/documents/{doc1}/purge", alice,
                {"purge_at": "2020-01-01T00:00:00Z"})[0] == 400
    assert call("PUT", f"/documents/{doc1}/purge", alice,
                {"purge_at": "2030-01-01T00:00:00"})[0] == 422

    # 重新安排一个 2 秒后清除的时刻，观察到点清除
    status, resp = call("PUT", f"/documents/{doc1}/purge", alice, {"purge_at": in_seconds(2)})
    assert status == 200
    purge_at = resp["purge_at"]
    print("== 等待清除时刻到达（扫描器自动处理）…… ==")
    got = wait_purged(alice, doc1)
    show("到期后查询", 200, got)
    assert got["status"] == "purged"
    assert got["purged_at"] == purge_at          # 清除时刻 = 约定时刻
    assert got["digest"] == digest1              # 摘要保留
    assert got["witnesses"] == ["p-bob", "p-carol", "p-dave"]  # 参与方保留
    assert "body" not in got                     # 任何接口不再返回正文

    # 已清除资料：一切写操作 409，且响应不含正文
    assert call("POST", f"/documents/{doc1}/open-requests", alice,
                {"reason": "x", "deadline": in_hours(1)})[0] == 409
    assert call("POST", f"/documents/{doc1}/witness-replacements", alice,
                {"old_witness": "p-dave", "new_witness": "p-erin"})[0] == 409
    assert call("POST", f"/documents/{doc1}/handover-invitations", alice,
                {"to_username": "p-erin", "confirm_deadline": in_hours(1)})[0] == 409
    assert call("PUT", f"/documents/{doc1}/purge", alice, {"purge_at": in_hours(1)})[0] == 409
    assert call("DELETE", f"/documents/{doc1}/purge", alice)[0] == 409

    # ------------------------------------------------------------------
    # 场景 2：未开封资料清除 -> 进行中请求失效、旧同意作废、待确认邀请失效
    # ------------------------------------------------------------------
    print("== 场景 2：未开封资料清除的连带结算 ==")
    _, sealed = call("POST", "/documents", alice,
                     {"body": "清除示例正文 2", "witnesses": ["p-bob", "p-carol", "p-dave"]})
    doc2, digest2 = sealed["document_id"], sealed["digest"]
    _, req = call("POST", f"/documents/{doc2}/open-requests", alice,
                  {"reason": "清除前申请", "deadline": in_hours(2)})
    req2 = req["request_id"]
    call("POST", f"/open-requests/{req2}/approve", bob, {"digest": digest2})
    _, inv = call("POST", f"/documents/{doc2}/handover-invitations", alice,
                  {"to_username": "p-erin", "confirm_deadline": in_hours(1)})
    inv2 = inv["invitation_id"]

    call("PUT", f"/documents/{doc2}/purge", alice, {"purge_at": in_seconds(2)})
    got = wait_purged(alice, doc2)
    assert got["open_request"]["status"] == "invalidated"   # 进行中请求失效
    assert got["witness_approvals"] == []                   # 旧同意作废
    _, inv_view = call("GET", f"/handover-invitations/{inv2}", alice)
    assert inv_view["status"] == "expired"                  # 待确认邀请失效
    assert call("POST", f"/open-requests/{req2}/approve", carol, {"digest": digest2})[0] == 409
    assert call("POST", f"/handover-invitations/{inv2}/confirm", erin)[0] == 409
    print("-- 进行中请求 invalidated、旧同意清零、邀请 expired，均不可再用 -> 通过")

    # ------------------------------------------------------------------
    # 场景 3：已开封资料仍按期清除
    # ------------------------------------------------------------------
    print("== 场景 3：已开封资料到点照样清除正文 ==")
    _, sealed = call("POST", "/documents", alice,
                     {"body": "清除示例正文 3", "witnesses": ["p-bob", "p-carol", "p-dave"]})
    doc3, digest3 = sealed["document_id"], sealed["digest"]
    _, req = call("POST", f"/documents/{doc3}/open-requests", alice,
                  {"reason": "先开封", "deadline": in_hours(2)})
    req3 = req["request_id"]
    call("POST", f"/open-requests/{req3}/approve", bob, {"digest": digest3})
    _, opened = call("POST", f"/open-requests/{req3}/approve", carol, {"digest": digest3})
    assert opened["status"] == "opened"
    _, got = call("GET", f"/documents/{doc3}", alice)
    assert got["body"] == "清除示例正文 3"       # 开封后可见正文

    call("PUT", f"/documents/{doc3}/purge", alice, {"purge_at": in_seconds(2)})
    got = wait_purged(alice, doc3)
    show("已开封资料清除后查询", 200, got)
    assert got["status"] == "purged" and "body" not in got
    assert got["opened_at"] is not None and got["digest"] == digest3
    assert [a["username"] for a in got["witness_approvals"]] == ["p-bob", "p-carol"]

    # ------------------------------------------------------------------
    # 场景 4：清除与最早开封同刻 -> 清除优先；开封更早 -> 先开封再清除
    # ------------------------------------------------------------------
    print("== 场景 4：同刻清除优先；开封更早则先开封 ==")
    _, sealed = call("POST", "/documents", alice,
                     {"body": "清除示例正文 4", "witnesses": ["p-bob", "p-carol", "p-dave"]})
    doc4, digest4 = sealed["document_id"], sealed["digest"]
    _, req = call("POST", f"/documents/{doc4}/open-requests", alice,
                  {"reason": "同刻赛跑", "deadline": in_hours(2),
                   "earliest_open_at": in_seconds(2)})
    req4, earliest4 = req["request_id"], req["earliest_open_at"]
    call("POST", f"/open-requests/{req4}/approve", bob, {"digest": digest4})
    _, resp = call("POST", f"/open-requests/{req4}/approve", carol, {"digest": digest4})
    assert resp["status"] == "waiting"
    # 清除时刻与最早开封时刻完全相同 -> 清除优先，不开封
    call("PUT", f"/documents/{doc4}/purge", alice, {"purge_at": earliest4})
    got = wait_purged(alice, doc4)
    assert got["status"] == "purged" and got["open_request"]["status"] == "invalidated"
    _, audit = call("GET", f"/documents/{doc4}/audit", alice)
    actions = [r["action"] for r in audit["records"]]
    assert "opened" not in actions and "purged" in actions
    print("-- 同刻清除优先：无 opened 记录，请求 invalidated -> 通过")

    _, sealed = call("POST", "/documents", alice,
                     {"body": "清除示例正文 5", "witnesses": ["p-bob", "p-carol", "p-dave"]})
    doc5, digest5 = sealed["document_id"], sealed["digest"]
    _, req = call("POST", f"/documents/{doc5}/open-requests", alice,
                  {"reason": "先开后清", "deadline": in_hours(2),
                   "earliest_open_at": in_seconds(1)})
    req5 = req["request_id"]
    call("POST", f"/open-requests/{req5}/approve", bob, {"digest": digest5})
    call("POST", f"/open-requests/{req5}/approve", carol, {"digest": digest5})
    call("PUT", f"/documents/{doc5}/purge", alice, {"purge_at": in_seconds(3)})
    got = wait_purged(alice, doc5)
    _, audit = call("GET", f"/documents/{doc5}/audit", alice)
    actions = [r["action"] for r in audit["records"]]
    assert actions.index("opened") < actions.index("purged")   # 先开封后清除
    print("-- 开封更早：审计顺序 opened -> purged -> 通过")

    # ------------------------------------------------------------------
    # 场景 5：交接后由新封存人管理清除安排
    # ------------------------------------------------------------------
    print("== 场景 5：交接后清除管理权转移 ==")
    _, sealed = call("POST", "/documents", alice,
                     {"body": "清除示例正文 6", "witnesses": ["p-bob", "p-carol", "p-dave"]})
    doc6 = sealed["document_id"]
    call("PUT", f"/documents/{doc6}/purge", alice, {"purge_at": in_hours(1)})
    _, inv = call("POST", f"/documents/{doc6}/handover-invitations", alice,
                  {"to_username": "p-erin", "confirm_deadline": in_hours(1)})
    status, resp = call("POST", f"/handover-invitations/{inv['invitation_id']}/confirm", erin)
    assert status == 200
    # 原封存人无权改期/取消；新封存人可以
    assert call("PUT", f"/documents/{doc6}/purge", alice, {"purge_at": in_hours(2)})[0] == 403
    assert call("DELETE", f"/documents/{doc6}/purge", alice)[0] == 403
    assert call("PUT", f"/documents/{doc6}/purge", erin, {"purge_at": in_hours(2)})[0] == 200
    assert call("DELETE", f"/documents/{doc6}/purge", erin)[0] == 200
    print("-- 原封存人 403；新封存人改期/取消成功 -> 通过")

    print("\n全部到期清除示例断言通过。资料 1 的审计动作：")
    _, audit = call("GET", f"/documents/{doc1}/audit", alice)
    print("  " + ", ".join(r["action"] for r in audit["records"]))


if __name__ == "__main__":
    main()
