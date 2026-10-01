#!/usr/bin/env python3
"""最早开封时刻（earliest_open_at）端到端示例（纯标准库）。

演示：
1. 提交开封请求时指定最早开封时刻（不早于当前、早于截止时刻）；
2. 两票在该时刻前集齐 -> waiting（待开封），查询只显示时刻与状态，不含正文；
3. 到点由服务自动开封，随后查询可见正文；
4. 另起一份资料演示“到点时不足两票、截止前补齐第二票立即开封”。

用法: python examples/demo_scheduled.py [base_url]
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


def wait_for_status(owner, doc_id, target: str, timeout: float = 10.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        _, got = call("GET", f"/documents/{doc_id}", owner)
        if got["status"] == target:
            return got
        time.sleep(0.2)
    raise RuntimeError(f"等待状态 {target} 超时: {got}")


def main() -> None:
    users = {}
    for name in ["s-alice", "s-bob", "s-carol", "s-dave"]:
        _, body = call("POST", "/register", payload={"username": name})
        users[name] = body["api_key"]
    owner, w1, w2 = users["s-alice"], users["s-bob"], users["s-carol"]

    # ---------- 场景一：两票提前集齐，到点自动开封 ----------
    text = "定时开封示例正文：待会议时间到达后再开启"
    _, sealed = call("POST", "/documents", owner,
                     {"body": text, "witnesses": ["s-bob", "s-carol", "s-dave"]})
    doc_id, digest = sealed["document_id"], sealed["digest"]

    now = dt.datetime.now(dt.timezone.utc)
    earliest = (now + dt.timedelta(seconds=3)).isoformat()
    deadline = (now + dt.timedelta(hours=2)).isoformat()
    _, req = call("POST", f"/documents/{doc_id}/open-requests", owner,
                  {"reason": "明晚家庭会议时开封", "deadline": deadline,
                   "earliest_open_at": earliest})
    req_id = req["request_id"]
    print("open request:", json.dumps(req, ensure_ascii=False))

    print("approve w1:", call("POST", f"/open-requests/{req_id}/approve", w1, {"digest": digest})[1])
    _, second = call("POST", f"/open-requests/{req_id}/approve", w2, {"digest": digest})
    assert second["status"] == "waiting", second
    print("approve w2:", second)

    # 待开封期间查询：有 earliest_open_at 与 waiting 状态，但没有正文
    _, before = call("GET", f"/documents/{doc_id}", owner)
    info = before["latest_open_request"]
    assert before["status"] == "sealed" and info["status"] == "waiting"
    assert "body" not in before and info["earliest_open_at"] == earliest
    print("waiting query (no body):", json.dumps(info, ensure_ascii=False))

    # 此时撤销仍然有效（到点前未集齐的撤销规则不变；这里不真撤，仅演示到点自动开封）
    opened = wait_for_status(owner, doc_id, "opened")
    assert opened["body"] == text
    print("auto-opened at:", opened["opened_at"])

    # 到点后撤销无效
    code, _ = call("POST", f"/open-requests/{req_id}/revoke", owner, {"reason": "晚了"})
    assert code == 409
    print("revoke after open -> 409")

    # ---------- 场景二：到点时只有一票，截止前补齐第二票立即开封 ----------
    text2 = "补齐第二票立即开封示例正文"
    _, sealed2 = call("POST", "/documents", owner,
                      {"body": text2, "witnesses": ["s-bob", "s-carol", "s-dave"]})
    doc2, dg2 = sealed2["document_id"], sealed2["digest"]
    now = dt.datetime.now(dt.timezone.utc)
    earliest2 = (now + dt.timedelta(seconds=2)).isoformat()
    deadline2 = (now + dt.timedelta(hours=2)).isoformat()
    _, req2 = call("POST", f"/documents/{doc2}/open-requests", owner,
                   {"reason": "人到齐再补第二票", "deadline": deadline2,
                    "earliest_open_at": earliest2})
    rid2 = req2["request_id"]
    call("POST", f"/open-requests/{rid2}/approve", w1, {"digest": dg2})
    print("one vote collected; waiting past earliest_open_at...")
    time.sleep(2.5)  # 到点但仍不足两票：不开封、不过期
    _, still = call("GET", f"/documents/{doc2}", owner)
    assert still["status"] == "sealed" and "body" not in still

    _, late = call("POST", f"/open-requests/{rid2}/approve", w2, {"digest": dg2})
    assert late["status"] == "opened", late
    _, got2 = call("GET", f"/documents/{doc2}", owner)
    assert got2["body"] == text2
    print("second vote before deadline opened immediately:", late["status"])


if __name__ == "__main__":
    main()
