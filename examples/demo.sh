#!/usr/bin/env bash
# 完整流程调用示例：注册 -> 封存 -> 申请开封 -> 两名见证人同意 -> 开封查询
# 依赖: curl, python3（用于生成截止时刻与摘要）
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"

jget() { python3 -c "import sys,json; print(json.load(sys.stdin)$1)"; }

echo "== 1. 注册封存人与三名见证人 =="
OWNER_KEY=$(curl -sS -X POST "$BASE/register" -H 'Content-Type: application/json' \
  -d '{"username":"alice"}' | jget "['api_key']")
W1_KEY=$(curl -sS -X POST "$BASE/register" -H 'Content-Type: application/json' -d '{"username":"bob"}'   | jget "['api_key']")
W2_KEY=$(curl -sS -X POST "$BASE/register" -H 'Content-Type: application/json' -d '{"username":"carol"}' | jget "['api_key']")
W3_KEY=$(curl -sS -X POST "$BASE/register" -H 'Content-Type: application/json' -d '{"username":"dave"}'  | jget "['api_key']")
echo "owner key=$OWNER_KEY"

echo "== 2. 封存资料（响应只有摘要，不含正文）=="
BODY='遗嘱正文：全部存款由……（此处省略）'
SEAL_RESP=$(curl -sS -X POST "$BASE/documents" \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d "$(python3 - "$BODY" <<'PY'
import json, sys
print(json.dumps({"body": sys.argv[1], "witnesses": ["bob", "carol", "dave"]}))
PY
)")
echo "$SEAL_RESP"
DOC_ID=$(echo "$SEAL_RESP" | jget "['document_id']")
DIGEST=$(echo "$SEAL_RESP" | jget "['digest']")

echo "== 3. 开封前查询（无 body 字段）=="
curl -sS "$BASE/documents/$DOC_ID" -H "X-API-Key: $OWNER_KEY" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=4))"

echo "== 4. 封存人提交带理由与截止时刻的开封请求 =="
DEADLINE=$(python3 -c "import datetime; print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=24)).isoformat())")
REQ_ID=$(curl -sS -X POST "$BASE/documents/$DOC_ID/open-requests" \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d "$(python3 - "$DEADLINE" <<'PY'
import json, sys
print(json.dumps({"reason": "家庭会议一致同意开封", "deadline": sys.argv[1]}))
PY
)" | jget "['request_id']")
echo "request_id=$REQ_ID"

echo "== 5. 见证人同意（绑定原摘要）；bob、carol 两票即开封 =="
curl -sS -X POST "$BASE/open-requests/$REQ_ID/approve" \
  -H "X-API-Key: $W1_KEY" -H 'Content-Type: application/json' \
  -d "{\"digest\":\"$DIGEST\"}" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=4))"
curl -sS -X POST "$BASE/open-requests/$REQ_ID/approve" \
  -H "X-API-Key: $W2_KEY" -H 'Content-Type: application/json' \
  -d "{\"digest\":\"$DIGEST\"}" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=4))"

echo "== 6. 开封后查询：正文、摘要、理由、见证记录全部返回 =="
curl -sS "$BASE/documents/$DOC_ID" -H "X-API-Key: $OWNER_KEY" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=4))"

echo "== 7. 按顺序持久保存的操作记录 =="
curl -sS "$BASE/documents/$DOC_ID/audit" -H "X-API-Key: $OWNER_KEY" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=4))"
