#!/usr/bin/env python3
"""见证人替换功能调用示例（纯标准库，无需第三方依赖）。

覆盖：
1) 无进行中请求时直接替换：旧见证人立即失去访问权，新见证人进入名单；
2) 进行中（pending）请求：被替换者已投的票失效、其他票保留，新见证人
   凭封存摘要补票，与保留的票凑齐两票开封；
3) waiting（两票集齐、待到点）期间替换投票者：请求由 waiting 退回
   pending，新见证人补票后重新进入 waiting；
4) 常见拒绝：资料已开封、新见证人未注册/是封存人/已在名单、被替换者
   不在名单、非封存人调用，均不改状态且不泄露正文。

用法: python examples/demo_replace_witness.py [base_url]
"""
import datetime as dt
import json
import sys
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


def show(title: str, status: int, body) -> None:
    print(f"-- {title} -> {status}")
    print("   " + json.dumps(body, ensure_ascii=False))


def replace(owner, doc_id, old, new):
    return call("POST", f"/documents/{doc_id}/witness-replacements", owner,
                {"old_witness": old, "new_witness": new})


def main() -> None:
    # 注册：封存人 alice + 四名候选见证人
    users = {}
    for name in ["alice", "bob", "carol", "dave", "erin"]:
        _, body = call("POST", "/register", payload={"username": name})
        users[name] = body["api_key"]
    owner, bob, carol, dave, erin = (users[n] for n in
                                     ["alice", "bob", "carol", "dave", "erin"])

    # ------------------------------------------------------------------
    # 场景 1：无进行中请求，直接把 dave 换成 erin
    # ------------------------------------------------------------------
    print("== 场景 1：未开封且无进行中请求时替换见证人 ==")
    _, sealed = call("POST", "/documents", owner,
                     {"body": "替换示例正文：REPLACE-DEMO-SECRET",
                      "witnesses": ["bob", "carol", "dave"]})
    doc1, digest1 = sealed["document_id"], sealed["digest"]

    status, resp = replace(owner, doc1, "dave", "erin")
    show("替换 dave -> erin", status, resp)
    assert status == 200
    assert resp["witnesses"] == ["bob", "carol", "erin"]
    assert resp["open_request"] is None

    # 旧见证人 dave 立即失去访问权（403），新见证人 erin 可见
    s, _ = call("GET", f"/documents/{doc1}", dave)
    print(f"-- 旧见证人 dave 访问资料 -> {s}（403 表示权限已收回）")
    assert s == 403
    s, got = call("GET", f"/documents/{doc1}", erin)
    assert s == 200 and "body" not in got  # 开封前仍无正文

    # 常见拒绝（状态不变、不泄露正文）
    assert replace(owner, doc1, "bob", "ghost")[0] == 400      # 新见证人未注册
    assert replace(owner, doc1, "ghost", "bob")[0] == 404      # 被替换者不在名单
    assert replace(owner, doc1, "bob", "alice")[0] == 400      # 新见证人是封存人
    assert replace(owner, doc1, "bob", "carol")[0] == 409      # 新见证人已在名单
    assert replace(bob, doc1, "bob", "dave")[0] == 403         # 只有封存人可替换

    # ------------------------------------------------------------------
    # 场景 2：pending 请求中替换已投票的见证人 -> 票失效、退回 0 票、补票开封
    # ------------------------------------------------------------------
    print("== 场景 2：进行中请求里替换已投票见证人，新见证人凭摘要补票 ==")
    _, sealed = call("POST", "/documents", owner,
                     {"body": "替换示例正文 2", "witnesses": ["bob", "carol", "dave"]})
    doc2, digest2 = sealed["document_id"], sealed["digest"]
    _, req = call("POST", f"/documents/{doc2}/open-requests", owner,
                  {"reason": "家庭会议", "deadline": in_hours(24)})
    req2 = req["request_id"]
    call("POST", f"/open-requests/{req2}/approve", carol, {"digest": digest2})  # carol 投票

    # 把已投票的 carol 换成 erin：该票失效，请求退回待同意
    status, resp = replace(owner, doc2, "carol", "erin")
    show("替换已投票的 carol -> erin", status, resp)
    assert status == 200
    assert resp["open_request"] == {"request_id": req2, "status": "pending", "approval_count": 0}

    # carol 已被替换，无权再同意（403）
    assert call("POST", f"/open-requests/{req2}/approve", carol, {"digest": digest2})[0] == 403

    # 新见证人 erin 凭【封存摘要】投第一票，保留的 bob 投第二票 -> 开封
    show("erin 凭摘要投票", *call("POST", f"/open-requests/{req2}/approve", erin,
                                  {"digest": digest2}))
    status, opened = call("POST", f"/open-requests/{req2}/approve", bob,
                          {"digest": digest2})
    show("bob 投票后", status, opened)
    assert opened["status"] == "opened"
    # 已开封资料不能再替换见证人 -> 409
    assert replace(owner, doc2, "bob", "dave")[0] == 409

    # ------------------------------------------------------------------
    # 场景 3：waiting（两票集齐待到点）替换投票者 -> 退回 pending
    # ------------------------------------------------------------------
    print("== 场景 3：waiting 期间替换投票者，请求退回待同意 ==")
    _, sealed = call("POST", "/documents", owner,
                     {"body": "替换示例正文 3", "witnesses": ["bob", "carol", "dave"]})
    doc3, digest3 = sealed["document_id"], sealed["digest"]
    _, req = call("POST", f"/documents/{doc3}/open-requests", owner,
                  {"reason": "定时开封", "deadline": in_hours(2),
                   "earliest_open_at": in_hours(1)})
    req3 = req["request_id"]
    call("POST", f"/open-requests/{req3}/approve", bob, {"digest": digest3})
    call("POST", f"/open-requests/{req3}/approve", carol, {"digest": digest3})
    # 两票在最早开封时刻前集齐 -> waiting
    s, got = call("GET", f"/documents/{doc3}", owner)
    assert got["latest_open_request"]["status"] == "waiting"

    # 替换投票者 carol：带走一票 -> waiting 退回 pending，bob 的票保留
    status, resp = replace(owner, doc3, "carol", "erin")
    show("waiting 期间替换 carol -> erin", status, resp)
    assert resp["open_request"]["status"] == "pending"
    assert resp["open_request"]["approval_count"] == 1

    # 新见证人 erin 补票 -> 重新 waiting（最早开封时刻未到）
    status, resp = call("POST", f"/open-requests/{req3}/approve", erin,
                        {"digest": digest3})
    show("erin 补票后", status, resp)
    assert resp["status"] == "waiting" and resp["approval_count"] == 2

    print("\n全部替换示例断言通过。审计可查：")
    _, audit = call("GET", f"/documents/{doc3}/audit", owner)
    print("  actions:", [r["action"] for r in audit["records"]])


if __name__ == "__main__":
    main()
