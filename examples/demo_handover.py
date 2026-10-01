#!/usr/bin/env python3
"""封存人交接（handover）端到端示例（纯标准库，无需第三方依赖）。

覆盖：
1) 发起交接邀请：受让人已注册、非封存人、非当前见证人；每份资料至多一份
   待确认邀请；
2) 撤回邀请后受让人不能确认，撤回后可重新邀请；
3) 逾期邀请不能确认，逾期后可重新邀请；
4) 确认交接：进行中的开封请求与其旧同意失效（invalidated），封存人身份
   转移，见证名单/正文/摘要不变；原封存人立即失去权限，新封存人重新申请
   开封并由原见证人两票开封；
5) 邀请待确认期间资料被两票开封：邀请随之失效、不能确认；
6) 常见拒绝：受让人未注册/是封存人/是见证人、截止时刻已过、非受让人确认、
   非封存人撤回、重复邀请，均不改状态且不泄露正文。

用法: python examples/demo_handover.py [base_url]
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


def invite(owner, doc_id, to_name, deadline=None):
    return call("POST", f"/documents/{doc_id}/handover-invitations", owner,
                {"to_username": to_name, "confirm_deadline": deadline or in_hours(1)})


def main() -> None:
    # 注册：封存人 alice、三名见证人 bob/carol/dave、受让人 erin、另一名用户 frank
    users = {}
    for name in ["h-alice", "h-bob", "h-carol", "h-dave", "h-erin", "h-frank"]:
        _, body = call("POST", "/register", payload={"username": name})
        users[name] = body["api_key"]
    alice, bob, carol, dave, erin, frank = (
        users[n] for n in ["h-alice", "h-bob", "h-carol", "h-dave", "h-erin", "h-frank"]
    )

    # ------------------------------------------------------------------
    # 场景 1：邀请校验 + 撤回 + 重新邀请
    # ------------------------------------------------------------------
    print("== 场景 1：发起邀请、拒绝校验、撤回后重新邀请 ==")
    _, sealed = call("POST", "/documents", alice,
                     {"body": "交接示例正文：HANDOVER-DEMO-SECRET",
                      "witnesses": ["h-bob", "h-carol", "h-dave"]})
    doc1, digest1 = sealed["document_id"], sealed["digest"]

    status, resp = invite(alice, doc1, "h-erin")
    show("邀请 erin 交接", status, resp)
    assert status == 201 and resp["status"] == "pending"
    inv1 = resp["invitation_id"]

    # 每份资料同时只允许一份待确认邀请
    assert invite(alice, doc1, "h-frank")[0] == 409
    # 受让人未注册 / 是封存人本人 / 是当前见证人
    assert invite(alice, doc1, "h-ghost")[0] == 400
    assert invite(alice, doc1, "h-alice")[0] == 400
    assert invite(alice, doc1, "h-bob")[0] == 400
    # 邀请待确认期间不能把受让人替换进见证人名单
    assert call("POST", f"/documents/{doc1}/witness-replacements", alice,
                {"old_witness": "h-dave", "new_witness": "h-erin"})[0] == 409
    # 截止时刻已过 / 不带时区
    assert invite(alice, doc1, "h-erin", "2020-01-01T00:00:00Z")[0] == 400
    # 只有封存人可以发起；只有受让人本人可以确认；只有封存人可以撤回
    assert invite(bob, doc1, "h-frank")[0] == 403
    assert call("POST", f"/handover-invitations/{inv1}/confirm", alice)[0] == 403
    assert call("POST", f"/handover-invitations/{inv1}/confirm", bob)[0] == 403

    # 封存人撤回 -> 受让人不能确认；撤回后可重新邀请
    status, resp = call("POST", f"/handover-invitations/{inv1}/revoke", alice)
    show("撤回邀请", status, resp)
    assert status == 200 and resp["status"] == "revoked"
    assert call("POST", f"/handover-invitations/{inv1}/confirm", erin)[0] == 409
    status, resp = invite(alice, doc1, "h-erin", in_seconds(2))
    assert status == 201
    inv_short = resp["invitation_id"]

    # ------------------------------------------------------------------
    # 场景 2：逾期不能确认，逾期后可重新邀请（后台扫描器默认每秒处理）
    # ------------------------------------------------------------------
    print("== 场景 2：邀请逾期后不能确认，可重新邀请 ==")
    time.sleep(2.5)
    status, resp = call("POST", f"/handover-invitations/{inv_short}/confirm", erin)
    show("逾期确认", status, resp)
    assert status == 409
    assert call("GET", f"/handover-invitations/{inv_short}", erin)[1]["status"] == "expired"
    status, resp = invite(alice, doc1, "h-erin")
    assert status == 201 and resp["status"] == "pending"
    inv2 = resp["invitation_id"]
    # 这一份场景 4 再用，先撤回保持干净
    call("POST", f"/handover-invitations/{inv2}/revoke", alice)

    # ------------------------------------------------------------------
    # 场景 3：确认交接 —— 进行中的开封请求与旧同意失效，身份转移
    # ------------------------------------------------------------------
    print("== 场景 3：确认交接，旧开封请求失效，新封存人重新申请开封 ==")
    _, sealed = call("POST", "/documents", alice,
                     {"body": "交接示例正文 2", "witnesses": ["h-bob", "h-carol", "h-dave"]})
    doc2, digest2 = sealed["document_id"], sealed["digest"]
    inv3 = invite(alice, doc2, "h-erin")[1]["invitation_id"]

    # 旧封存人发起开封请求，bob 已投一票
    _, req = call("POST", f"/documents/{doc2}/open-requests", alice,
                  {"reason": "交接前申请", "deadline": in_hours(24)})
    old_req = req["request_id"]
    call("POST", f"/open-requests/{old_req}/approve", bob, {"digest": digest2})

    # 受让人在截止前凭本人密钥确认
    status, resp = call("POST", f"/handover-invitations/{inv3}/confirm", erin)
    show("erin 确认交接", status, resp)
    assert status == 200
    assert resp["status"] == "confirmed"
    assert resp["current_owner"] == "h-erin" and resp["previous_owner"] == "h-alice"
    assert resp["witnesses"] == ["h-bob", "h-carol", "h-dave"]
    # 旧开封请求失效、旧票清零
    assert resp["old_open_request"] == {
        "request_id": old_req, "status": "invalidated", "approval_count": 0
    }
    assert "body" not in json.dumps(resp)

    # 原封存人立即失去查看/操作权限
    s, _ = call("GET", f"/documents/{doc2}", alice)
    print(f"-- 原封存人 alice 访问资料 -> {s}（403 表示权限已收回）")
    assert s == 403
    # 新封存人可见（开封前无正文），旧请求不能再同意
    _, got = call("GET", f"/documents/{doc2}", erin)
    assert got["status"] == "sealed" and "body" not in got
    assert call("POST", f"/open-requests/{old_req}/approve", carol,
                {"digest": digest2})[0] == 409

    # 新封存人重新申请开封，从 0 票开始，原见证人两票开封
    _, req = call("POST", f"/documents/{doc2}/open-requests", erin,
                  {"reason": "新封存人申请", "deadline": in_hours(24)})
    new_req = req["request_id"]
    call("POST", f"/open-requests/{new_req}/approve", bob, {"digest": digest2})
    status, opened = call("POST", f"/open-requests/{new_req}/approve", carol,
                          {"digest": digest2})
    show("新封存人的开封请求两票集齐", status, opened)
    assert opened["status"] == "opened"
    _, got = call("GET", f"/documents/{doc2}", erin)
    assert got["body"] == "交接示例正文 2"

    # ------------------------------------------------------------------
    # 场景 4：邀请待确认期间资料被开封 -> 邀请失效，不能确认
    # ------------------------------------------------------------------
    print("== 场景 4：邀请待确认期间资料开封，邀请随之失效 ==")
    _, sealed = call("POST", "/documents", alice,
                     {"body": "交接示例正文 3", "witnesses": ["h-bob", "h-carol", "h-dave"]})
    doc3, digest3 = sealed["document_id"], sealed["digest"]
    inv4 = invite(alice, doc3, "h-erin")[1]["invitation_id"]
    req4 = call("POST", f"/documents/{doc3}/open-requests", alice,
                {"reason": "直接开封", "deadline": in_hours(24)})[1]["request_id"]
    call("POST", f"/open-requests/{req4}/approve", bob, {"digest": digest3})
    call("POST", f"/open-requests/{req4}/approve", carol, {"digest": digest3})

    assert call("GET", f"/handover-invitations/{inv4}", erin)[1]["status"] == "expired"
    assert call("POST", f"/handover-invitations/{inv4}/confirm", erin)[0] == 409
    assert invite(alice, doc3, "h-erin")[0] == 409   # 已开封不能再发起交接

    print("\n全部交接示例断言通过。资料 2 的审计动作：")
    _, audit = call("GET", f"/documents/{doc2}/audit", erin)
    print("  " + ", ".join(r["action"] for r in audit["records"]))


if __name__ == "__main__":
    main()
