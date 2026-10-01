#!/usr/bin/env python3
"""完整流程调用示例（纯标准库，无需第三方依赖）。

用法: python examples/demo.py [base_url]
"""
import datetime as dt
import hashlib
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


def main() -> None:
    # 1. 注册
    users = {}
    for name in ["alice", "bob", "carol", "dave"]:
        _, body = call("POST", "/register", payload={"username": name})
        users[name] = body["api_key"]
    owner, w1, w2 = users["alice"], users["bob"], users["carol"]

    # 2. 封存
    text = "遗嘱正文：Python 示例中的秘密内容"
    status, sealed = call("POST", "/documents", owner,
                          {"body": text, "witnesses": ["bob", "carol", "dave"]})
    doc_id, digest = sealed["document_id"], sealed["digest"]
    print("sealed:", sealed)

    # 3. 开封前查询：注意没有 body
    _, before = call("GET", f"/documents/{doc_id}", owner)
    assert "body" not in before, "开封前泄露了正文！"
    print("before open:", before)

    # 4. 提交开封请求（理由 + 截止时刻）
    deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)).isoformat()
    status, req = call("POST", f"/documents/{doc_id}/open-requests", owner,
                       {"reason": "家庭会议开封", "deadline": deadline})
    req_id = req["request_id"]

    # 5. 两名不同见证人分别同意，且必须绑定原摘要
    print("approve w1:", call("POST", f"/open-requests/{req_id}/approve", w1, {"digest": digest})[1])
    # 同一人重试 -> 409，只计一次
    print("approve w1 retry:", call("POST", f"/open-requests/{req_id}/approve", w1, {"digest": digest}))
    print("approve w2:", call("POST", f"/open-requests/{req_id}/approve", w2, {"digest": digest})[1])

    # 6. 开封后查询：正文/摘要/理由/见证记录
    _, after = call("GET", f"/documents/{doc_id}", owner)
    assert after["body"] == text
    print("after open:", json.dumps(after, ensure_ascii=False, indent=2))

    # 7. 操作记录
    _, audit = call("GET", f"/documents/{doc_id}/audit", owner)
    print("audit:", [r["action"] for r in audit["records"]])

    # 8. 摘要必须自行计算并一致
    assert hashlib.sha256(text.encode()).hexdigest() == digest


if __name__ == "__main__":
    main()
