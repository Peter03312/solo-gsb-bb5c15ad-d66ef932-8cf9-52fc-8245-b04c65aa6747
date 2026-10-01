#!/usr/bin/env python3
"""到期清除（scheduled purge）端到端示例（纯标准库）。

演示：
1. 封存人凭现有密钥为资料安排带时区的未来清除时刻；
2. 到期前改期、取消（取消后可重新安排）；
3. 到点后服务确定性清除（不依赖扫描器准时）：查询/列表只返回 purged 与
   清除时刻，保留摘要、参与方与审计，任何接口不再返回正文；
4. 未开封资料清除时进行中的开封请求失效、旧同意作废，待确认交接邀请失效；
5. 已开封资料仍按期清除；
6. 交接后由新封存人管理清除安排，原封存人无权改期/取消；
7. 清除与最早开封同刻时清除优先。

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


def iso(seconds: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat()


def wait_purged(key, doc_id, timeout: float = 10.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        _, got = call("GET", f"/documents/{doc_id}", key)
        if got["status"] == "purged":
            return got
        time.sleep(0.2)
    raise RuntimeError(f"等待清除超时: {got}")


def main() -> None:
    users = {}
    for name in ["g-alice", "g-bob", "g-carol", "g-dave", "g-erin"]:
        _, body = call("POST", "/register", payload={"username": name})
        users[name] = body["api_key"]
    owner, w1, w2 = users["g-alice"], users["g-bob"], users["g-carol"]
    erin = users["g-erin"]

    # ---------- 场景一：安排 -> 改期 -> 取消 -> 重新安排 -> 到期清除 ----------
    text = "到期清除示例正文：若干年后自动销毁 PURGE-BODY"
    _, sealed = call("POST", "/documents", owner,
                     {"body": text, "witnesses": ["g-bob", "g-carol", "g-dave"]})
    doc_id, digest = sealed["document_id"], sealed["digest"]

    _, sch = call("POST", f"/documents/{doc_id}/purge-schedules", owner,
                  {"purge_at": iso(3600)})
    sid = sch["schedule_id"]
    print("scheduled:", json.dumps(sch, ensure_ascii=False))

    # 到期前改期
    _, moved = call("POST", f"/purge-schedules/{sid}/reschedule", owner,
                    {"purge_at": iso(7200)})
    assert moved["status"] == "scheduled"
    print("rescheduled to:", moved["purge_at"])

    # 取消；取消后改期会被 409，重新安排可以
    code, _ = call("POST", f"/purge-schedules/{sid}/cancel", owner)
    assert code == 200
    code, _ = call("POST", f"/purge-schedules/{sid}/reschedule", owner, {"purge_at": iso(9)})
    assert code == 409
    print("cancelled -> reschedule rejected with", code)

    _, sch2 = call("POST", f"/documents/{doc_id}/purge-schedules", owner,
                   {"purge_at": iso(3)})
    sid2 = sch2["schedule_id"]
    print("re-scheduled:", sid2, sch2["purge_at"])

    # 清除前：资料仍 sealed，无正文
    _, before = call("GET", f"/documents/{doc_id}", owner)
    assert before["status"] == "sealed" and "body" not in before
    assert before["purge_schedule"]["status"] == "scheduled"

    purged = wait_purged(owner, doc_id)
    assert "body" not in purged and purged["digest"] == digest
    assert purged["purged_at"] is not None
    assert purged["purge_schedule"]["status"] == "done"
    print("purged at:", purged["purged_at"], "| body absent, digest kept")

    # 清除后所有写操作拒绝，且错误体不含正文
    for method, path, payload in [
        ("POST", f"/documents/{doc_id}/open-requests",
         {"reason": "x", "deadline": iso(3600)}),
        ("POST", f"/documents/{doc_id}/witness-replacements",
         {"old_witness": "g-bob", "new_witness": "g-erin"}),
        ("POST", f"/documents/{doc_id}/handover-invitations",
         {"to_username": "g-erin", "confirm_deadline": iso(3600)}),
        ("POST", f"/purge-schedules/{sid2}/cancel", None),
    ]:
        code, err_body = call(method, path, owner, payload)
        assert code == 409 and text not in json.dumps(err_body, ensure_ascii=False)
    print("all post-purge writes rejected with 409, no body leaked")

    # ---------- 场景二：未开封资料清除，进行中开封请求与邀请一并失效 ----------
    text2 = "带进行中请求的清除正文 PURGE-WITH-REQUEST"
    _, sealed2 = call("POST", "/documents", owner,
                      {"body": text2, "witnesses": ["g-bob", "g-carol", "g-dave"]})
    doc2, dg2 = sealed2["document_id"], sealed2["digest"]

    _, req = call("POST", f"/documents/{doc2}/open-requests", owner,
                  {"reason": "清除前想开封", "deadline": iso(3600)})
    rid = req["request_id"]
    call("POST", f"/open-requests/{rid}/approve", w1, {"digest": dg2})  # 先攒一票

    _, inv = call("POST", f"/documents/{doc2}/handover-invitations", owner,
                  {"to_username": "g-erin", "confirm_deadline": iso(3600)})
    inv_id = inv["invitation_id"]

    call("POST", f"/documents/{doc2}/purge-schedules", owner, {"purge_at": iso(3)})
    got2 = wait_purged(owner, doc2)
    assert got2["latest_open_request"]["status"] == "invalidated"
    assert got2["latest_open_request"]["approval_count"] == 0
    _, inv_got = call("GET", f"/handover-invitations/{inv_id}", erin)
    assert inv_got["status"] == "expired"
    print("in-flight request invalidated, approvals wiped, invitation expired")

    # 见证人查询：只看到已清除元数据，无正文
    _, wview = call("GET", f"/documents/{doc2}", w1)
    assert wview["status"] == "purged" and "body" not in wview and wview["digest"] == dg2
    # 审计元数据完整
    _, audit = call("GET", f"/documents/{doc2}/audit", owner)
    assert audit["records"][-1]["action"] == "purged"
    assert all(text2 not in r["detail"] for r in audit["records"])
    print("witnesses/audit keep metadata only")

    # ---------- 场景三：已开封资料仍按期清除 ----------
    text3 = "开封后也要清除的正文 PURGE-OPENED"
    _, sealed3 = call("POST", "/documents", owner,
                      {"body": text3, "witnesses": ["g-bob", "g-carol", "g-dave"]})
    doc3, dg3 = sealed3["document_id"], sealed3["digest"]
    _, req3 = call("POST", f"/documents/{doc3}/open-requests", owner,
                   {"reason": "先开封", "deadline": iso(3600)})
    call("POST", f"/open-requests/{req3['request_id']}/approve", w1, {"digest": dg3})
    call("POST", f"/open-requests/{req3['request_id']}/approve", w2, {"digest": dg3})
    _, opened = call("GET", f"/documents/{doc3}", owner)
    assert opened["body"] == text3

    call("POST", f"/documents/{doc3}/purge-schedules", owner, {"purge_at": iso(3)})
    got3 = wait_purged(owner, doc3)
    assert "body" not in got3 and got3["latest_open_request"]["status"] == "opened"
    assert "witness_approvals" not in got3
    print("opened document purged on schedule, open history kept, body gone")

    # ---------- 场景四：交接后新封存人管理清除安排 ----------
    text4 = "交接后清除的正文 PURGE-HANDOVER"
    _, sealed4 = call("POST", "/documents", owner,
                      {"body": text4, "witnesses": ["g-bob", "g-carol", "g-dave"]})
    doc4 = sealed4["document_id"]
    _, sch4 = call("POST", f"/documents/{doc4}/purge-schedules", owner, {"purge_at": iso(3600)})
    sid4 = sch4["schedule_id"]
    _, inv4 = call("POST", f"/documents/{doc4}/handover-invitations", owner,
                   {"to_username": "g-erin", "confirm_deadline": iso(3600)})
    code, confirmed = call("POST", f"/handover-invitations/{inv4['invitation_id']}/confirm", erin)
    assert code == 200 and confirmed["current_owner"] == "g-erin"

    # 原封存人无权改期
    code, _ = call("POST", f"/purge-schedules/{sid4}/reschedule", owner, {"purge_at": iso(3600)})
    assert code == 403
    # 新封存人改期为 3 秒后，到期清除
    code, _ = call("POST", f"/purge-schedules/{sid4}/reschedule", erin, {"purge_at": iso(3)})
    assert code == 200
    got4 = wait_purged(erin, doc4)
    assert got4["status"] == "purged" and "body" not in got4
    print("new owner rescheduled and document purged; old owner locked out (403)")

    # ---------- 场景五：清除与最早开封同刻 -> 清除优先 ----------
    text5 = "同刻赛跑正文 PURGE-TIE"
    _, sealed5 = call("POST", "/documents", owner,
                      {"body": text5, "witnesses": ["g-bob", "g-carol", "g-dave"]})
    doc5, dg5 = sealed5["document_id"], sealed5["digest"]
    t = iso(3)
    call("POST", f"/documents/{doc5}/purge-schedules", owner, {"purge_at": t})
    _, req5 = call("POST", f"/documents/{doc5}/open-requests", owner,
                   {"reason": "同刻开封", "deadline": iso(3600), "earliest_open_at": t})
    call("POST", f"/open-requests/{req5['request_id']}/approve", w1, {"digest": dg5})
    time.sleep(3.2)
    # 第二票在清除时刻之后到达：资料已清除，同意被拒
    code, _ = call("POST", f"/open-requests/{req5['request_id']}/approve", w2, {"digest": dg5})
    assert code == 409
    got5 = wait_purged(owner, doc5)
    assert "body" not in got5 and got5["latest_open_request"]["status"] == "invalidated"
    print("tie at purge==earliest_open: purge wins, never opened")


if __name__ == "__main__":
    main()
