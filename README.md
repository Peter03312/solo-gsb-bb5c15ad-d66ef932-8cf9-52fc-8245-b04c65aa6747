# 双人见证资料封存服务

本地账号、密钥操作的资料封存/开封服务。封存后正文不可修改、不可（在开封前）被读取；
开封必须由封存人发起请求（理由 + 截止时刻，可选**最早开封时刻**），并在截止前取得
**两名不同指定见证人**分别同意；同意必须绑定本次请求与封存时的摘要。
资料未开封时，封存人可在见证人无法履职后**替换指定见证人**，新见证人可继续为
进行中的开封请求补票；封存人也可就未开封资料向另一名本地账号**发起交接邀请**，
受让人在截止前确认后成为新封存人。封存人还可为资料**安排到期清除时刻**：
到点清空正文（保留摘要、参与方与审计元数据），到期前可改期或取消，
交接后由新封存人管理。技术栈：Python 3.11 + FastAPI + SQLite。

## 安全与业务规则

| 规则 | 实现方式 |
| --- | --- |
| 本地账号，凭密钥操作 | `POST /register` 返回一次性 API Key，请求头 `X-API-Key` 携带；服务端只存 SHA-256 哈希 |
| 封存需三名不同见证人 | 封存时校验三人互异、均已注册、且不含封存人本人 |
| 保存正文及摘要 | 正文原文与 SHA-256 摘要同表保存；封存后无任何更新接口，内容不可改 |
| 开封前不泄露正文 | 封存/待开封状态下查询/列表/错误响应均不含 `body`；非参与方一律 `403`，不区分资料是否存在 |
| 开封请求 | 封存人提交 `reason` 与带时区的 `deadline`（必须晚于当前时间） |
| 最早开封时刻（可选） | `earliest_open_at`：带时区，不早于提交时刻且严格早于 `deadline`；不填时行为不变（两票集齐即开封） |
| 两票同意 | 截止前两名**不同**见证人各同意一次才开封；`UNIQUE(request_id, witness_id)` 保证同一人重试只计一次 |
| 同意绑定摘要 | 同意时须提交封存摘要，服务端比对一致才记录，防止对被篡改内容背书 |
| 定时开封 | 填了 `earliest_open_at` 时，两票在该时刻前集齐只进入 `waiting`（待开封）；到点自动开封。到点时不足两票的，截止前补齐第二票时立即开封 |
| 到点必开封 | 到点前未撤销且已集齐两票的请求，自到点起只能开封：后台扫描器与每个写事务都先做确定性到点判定，即使后台处理延迟，之后的撤销或到期扫描也不能使其失效 |
| 未开封可替换见证人 | 封存人在资料未开封时可把一名指定见证人换为新人：新人须已注册、非封存人、不在当前名单，替换后仍为三名不同见证人；旧见证人随即失去访问与同意权限 |
| 替换不破坏进行中请求 | 被替换者对本次请求的同意立即失效、其他同意保留；有效票不足两票时请求由 `waiting` 退回 `pending`，新见证人凭封存摘要补票；已到最早开封时刻且两票已齐则先开封并拒绝替换；过截止时刻的请求按原规则失效后再完成替换 |
| 撤销/到期作废 | 到点**之前**封存人可撤销；过截止时刻始终不足两票则置为 `revoked`/`expired`，其同意永久失效；新请求从 0 票开始 |
| 未开封可交接封存人 | 封存人可就未开封资料向另一已注册本地账号发起交接邀请（受让人不能是本人或当前见证人），填写带时区确认截止时刻；每份资料同时至多一份待确认且未过期邀请；封存人可撤回；受让人须在截止前凭本人密钥确认 |
| 交接确认结算顺序 | 确认临界区先做确定性到点开封与开封请求到期判定：资料若已开封则拒绝交接；否则进行中的开封请求置 `invalidated`（过截止时刻的按原规则置 `expired`）并删除全部旧同意，再转移封存人身份；见证名单、正文、摘要不变 |
| 交接后权限 | 原封存人立即失去该资料的查看/操作权限（统一 403，不区分资料是否存在）；新封存人可重新申请开封，新请求从 0 票开始；开封前任何接口与错误体都不含正文 |
| 邀请生命周期 | `pending` 邀请可撤回（`revoked`）或逾期（`expired`，后台扫描与每个交接写事务都会确定性判定）；已撤回/逾期/资料开封后均不可确认；撤回或逾期后封存人可重新邀请；资料开封会连带使待确认邀请失效 |
| 到期清除正文 | 封存人凭密钥提交带时区的未来清除时刻（`PUT /documents/{id}/purge`），到期前可改期（再次 `PUT`）或取消（`DELETE`）；只有当前封存人可操作，交接后清除安排保留并改由新封存人管理 |
| 到期必清除 | 到点无论扫描器是否准时运行，后续查询与操作都呈现已清除结果：后台扫描器、每个写事务与资料查询都先做确定性到点清除判定——清空正文（库中 `body` 置空）、状态置 `purged`、保留摘要/参与方/审计元数据、任何接口不再返回正文；`purged_at` 记为约定的清除时刻 |
| 清除的连带结算 | 已开封资料仍按期清除；未开封资料清除时：进行中开封请求置 `invalidated`（过截止时刻的按原规则 `expired`）并删除全部旧同意、待确认交接邀请置 `expired` |
| 清除与开封的时序 | 清除与最早开封同刻时**清除优先**（不开封，请求失效）；最早开封时刻严格更早时**先开封**，到清除时刻再清正文；延迟处理也按时刻比较得出同一历史 |
| 已清除不可再操作 | 已清除资料不能再开封、替换见证人、发起/确认交接或再安排清除；已到期的改期或取消一律 `409`；一切拒绝路径不泄露正文 |
| 拒绝且状态不变 | 失效凭据(401)、非见证人(403)、无效时限(400/422)、重复开封(409) 等任何拒绝路径都不写数据（到点自动开封/清除的提交除外：状态变更先落库，拒绝在提交后返回） |
| 并发只产生一个结果 | 全部“检查 + 写入”在 SQLite `BEGIN IMMEDIATE` 短事务内完成；并发同意/撤销/定时开封/到期/替换见证人/交接发起、撤回、确认/到期清除只有一个已提交终态（同意在锁内以“当前名单”复核见证人身份；交接与第二票同意赛跑时要么开封要么交接，互斥且唯一） |
| 操作留痕、重启一致 | 每次成功操作按自增序号追加 `audit_log`（含 `handover_invited/confirmed/revoked/expired`、`request_invalidated`、`purge_scheduled/rescheduled/canceled`、`purged`）；WAL 持久化，重启时先跑一轮恢复扫描，状态、票数、审计顺序完全一致 |

### 请求状态机

```
pending  ──(两票集齐，未设最早开封时刻 或 已到最早开封时刻)──▶ opened
pending  ──(两票集齐但未到 earliest_open_at)───────────────▶ waiting
waiting  ──(到达 earliest_open_at；扫描器或任意写事务触发)──▶ opened（不可再撤销/过期）
waiting  ──(封存人替换投票见证人，有效票不足两票)───────────▶ pending（新见证人补票）
pending  ──(封存人在截止前撤销)─────────────────────────────▶ revoked
pending  ──(过 deadline 仍不足两票)─────────────────────────▶ expired
pending/waiting ──(受让人在交接邀请截止前确认交接)──────────▶ invalidated（旧同意删除）
pending/waiting ──(到达清除时刻)────────────────────────────▶ invalidated（旧同意删除）
```

> 替换见证人不改变资料的 `sealed` 状态，只改变当前见证人名单，并按上表调整
> 进行中请求的票数与状态；交接封存人也不改变资料状态，只把进行中的开封请求
> 置为 `invalidated`（过截止时刻的仍为 `expired`）并转移封存人；`sealed/opened`
> 是资料生命周期，`pending/waiting` 等是开封请求的状态，两者互不混淆。

### 资料生命周期

```
sealed ──(两名见证人同意，且已到最早开封时刻)──▶ opened
sealed ──(到达清除时刻 purge_at)───────────────▶ purged（正文清空，不再返回）
opened ──(到达清除时刻 purge_at)───────────────▶ purged（正文清空，不再返回）
```

> `purged` 是终态：正文从库中清空，摘要、参与方与审计元数据保留，
> 任何接口不再返回正文；已清除资料不能再开封、替换见证人、交接或再安排清除。
> 清除与最早开封同刻时清除优先（不开封）；最早开封严格更早时先 `opened`
> 再于清除时刻 `purged`。

后台扫描器（默认每秒一次）在一轮内先把到点的 `waiting` 请求确定性开封
（清除优先的不开），再把到清除时刻的资料清除正文，然后把已到截止时刻仍
`pending` 的请求置为 `expired`，最后把过确认截止时刻仍 `pending` 的交接
邀请置为 `expired`；所有涉时刻判定在写事务内（资料查询对清除判定同理）
再做一次，杜绝临界时间竞态。

## 一键启动（Docker Compose）

```bash
docker compose up --build -d
curl http://127.0.0.1:8000/health
```

镜像构建时安装依赖；容器入口 `entrypoint.sh` 会幂等初始化库表
（`/data/seal.db`，位于 Docker 卷 `seal-data`，重启数据不丢）。

停止：`docker compose down`（加 `-v` 同时删除数据卷）。

## 本地启动（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SEAL_DB_PATH=./data/seal.db   # SQLite 文件路径（默认 /data/seal.db）
export PORT=8000                      # 监听端口（默认 8000）
export SWEEP_INTERVAL_SECONDS=1       # 到期扫描间隔（默认 1 秒）
python init_db.py                     # 可选：单独初始化库表（应用启动也会初始化）
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- 服务地址：`http://127.0.0.1:8000`
- 交互式文档：`http://127.0.0.1:8000/docs`
- 健康检查：`GET /health`

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SEAL_DB_PATH` | `/data/seal.db` | SQLite 数据库文件路径（自动创建，WAL 模式） |
| `PORT` | `8000` | 监听端口（容器入口使用） |
| `SWEEP_INTERVAL_SECONDS` | `1` | 到期请求后台扫描间隔秒数 |
| `HOST_PORT` | `8000` | 仅 Compose 使用：宿主机映射端口 |

## HTTP API

所有接口除 `/register`、`/health` 外均需请求头 `X-API-Key: <注册时返回的密钥>`。
时间字段使用带时区 ISO-8601，例如 `2026-10-01T12:00:00Z` 或 `2026-10-01T20:00:00+08:00`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/register` | 注册，返回一次性 `api_key` |
| GET | `/me` | 查询当前密钥对应的账号 |
| POST | `/documents` | 封存资料：`{body, witnesses:[3人]}`，返回 `digest`（无正文） |
| GET | `/documents` | 列出本人封存/见证的资料（仅元数据，含 `purge_at`/`purged_at`） |
| GET | `/documents/{id}` | 详情；开封后才含 `body`、`reason`、`witness_approvals`；已清除返回 `purged` 与清除时刻 |
| POST | `/documents/{id}/open-requests` | 封存人提交开封请求：`{reason, deadline, earliest_open_at?}` |
| POST | `/open-requests/{id}/approve` | 见证人同意：`{digest}`（须等于封存摘要）；第二票可能返回 `waiting` 或 `opened` |
| POST | `/open-requests/{id}/revoke` | 封存人撤销：`{reason?}` |
| POST | `/documents/{id}/witness-replacements` | 封存人替换一名指定见证人（资料未开封）：`{old_witness, new_witness}` |
| POST | `/documents/{id}/handover-invitations` | 封存人就未开封资料发起交接邀请：`{to_username, confirm_deadline}` |
| POST | `/handover-invitations/{id}/confirm` | 受让人在截止前凭本人密钥确认；返回新封存人与旧请求结果 |
| POST | `/handover-invitations/{id}/revoke` | 封存人撤回待确认邀请 |
| GET | `/handover-invitations/{id}` | 查询邀请状态（邀请双方或资料参与方可见，仅元数据） |
| PUT | `/documents/{id}/purge` | 封存人安排/改期到期清除时刻：`{purge_at}`（带时区、未来时刻） |
| DELETE | `/documents/{id}/purge` | 封存人在清除时刻到来前取消已安排的清除 |
| GET | `/documents/{id}/audit` | 按顺序返回该资料的全部操作记录 |

状态码：成功 `200/201`；参数校验失败 `422`（如时间不带时区、`earliest_open_at`
不早于 `deadline`）；业务校验失败 `400`（如三人不互异、截止时刻已过、最早开封时刻
早于当前、替换时新见证人未注册或为封存人、交接受让人未注册/为本人/为见证人、
确认截止时刻已过、清除时刻已过）；未认证 `401`；无权/不可见 `403`
（含被替换掉的旧见证人再访问/再同意、交接后原封存人再访问/再管理清除、非受让人确认、
非封存人撤回/安排清除）；未找到 `404`（被替换者不在当前名单）；
冲突（重复同意、重复开封、请求已撤销/到期、waiting 下再同意、已有进行中请求、
新见证人已在名单、资料已开封仍替换、重复发起待确认交接邀请、确认已撤回/逾期/
已确认的邀请、交接时资料已开封、资料已清除仍开封/替换/交接/安排清除、
清除时刻已到仍改期/取消、无清除安排仍取消等）`409`。错误体统一为
`{"detail": "..."}`，不含任何正文内容。

### 最早开封时刻（earliest_open_at）

- 字段可选，带时区 ISO-8601；必须**不早于提交时刻**且**严格早于** `deadline`。
- 不填写时一切照旧：两票集齐立即开封，响应里 `earliest_open_at` 为 `null`。
- 填写后，两票在该时刻前集齐只把请求置为 `waiting`（待开封）：同意响应与
  `GET /documents/{id}` 都会返回 `status: "waiting"` 与 `earliest_open_at`，
  但仍不返回 `body` 与见证明细。
- 到达 `earliest_open_at` 后由后台扫描器（或任意并发写事务，二者等价且互斥）
  自动开封；若到点时只有一票，则保持 `pending`，截止前第二票到达的**同一请求**
  立即开封。
- 到点前可以撤销；一旦到点且两票已齐，撤销与到期扫描都不再生效。

```bash
# 提交带最早开封时刻的请求：最早 1 小时后、截止 24 小时后
EARLIEST=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=1)).isoformat())")
DEADLINE=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=24)).isoformat())")
curl -sS -X POST $BASE/documents/1/open-requests \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d "{\"reason\":\"家庭会议\",\"deadline\":\"$DEADLINE\",\"earliest_open_at\":\"$EARLIEST\"}"
# -> {"request_id":1,"status":"pending",...,"earliest_open_at":"…"}

# 两票在时刻前集齐 -> waiting（而非 opened）
curl -sS -X POST $BASE/open-requests/1/approve -H "X-API-Key: $W1_KEY" \
  -H 'Content-Type: application/json' -d '{"digest":"ab12…"}'
# -> {"approval_count":1,"status":"pending",...}
curl -sS -X POST $BASE/open-requests/1/approve -H "X-API-Key: $W2_KEY" \
  -H 'Content-Type: application/json' -d '{"digest":"ab12…"}'
# -> {"approval_count":2,"status":"waiting","earliest_open_at":"…"}

# 待开封期间查询：有 earliest_open_at 与 waiting，仍无 body
curl -sS $BASE/documents/1 -H "X-API-Key: $OWNER_KEY"
# 到达最早开封时刻后（扫描间隔内）再查 -> status=opened，可见 body
```

### 替换指定见证人（witness replacement）

资料**未开封**时，封存人可提交被替换者与新见证人账号，把一名指定见证人换成新人。

- `POST /documents/{id}/witness-replacements`，请求体
  `{"old_witness": "<被替换者用户名>", "new_witness": "<新见证人用户名>"}`。
- 新见证人必须**已注册**、**不是封存人本人**、且**不在当前三名名单中**
  （含不能与被替换者相同）；替换后仍是三名互不相同的见证人。
- 只有**封存人本人**可调用；资料已开封（含替换事务内确定性到点开封）后再替换一律 `409`。
- **无进行中请求**（或上一轮已 `revoked/expired`）：直接更新名单，`open_request` 为 `null`。
- **有进行中的 `pending` 请求**：被替换者对本次请求的同意立即删除（失效），其他见证人
  的同意保留；若有效票因此不足两票（`pending` 下本就少于两票），新见证人可调用现有
  同意接口 `POST /open-requests/{id}/approve`，提交**封存摘要**为本次请求补票。
- **请求处于 `waiting`（两票已齐待到点）**：
  - 替换的是**投票者** → 有效票不足两票，请求**由 `waiting` 退回 `pending`**，
    保留另一票，新见证人补票后两票在最早时刻前再次进入 `waiting`；
  - 替换的是**未投票的第三名见证人** → 两票不受影响，维持 `waiting`。
- **到达最早开封时刻且两票已齐**：替换请求进入写临界区时请求**先开封**，
  替换随即 `409` 被拒（名单不变），即使后台扫描器尚未运行也一样。
- **已过截止时刻**：进行中的请求按原规则置 `expired`（旧票作废），替换随后照常完成，
  返回的 `open_request.status` 为 `expired`、`approval_count` 为 `0`。
- 替换成功后旧见证人**立即失去资料访问权与同意权**（查询/同意返回 `403`，
  名单/列表同步更新）；本次变更追加一条 `witness_replaced` 审计记录（必要时其前
  追加一条 `request_expired`）。

成功响应 `200`：

```json
{
  "document_id": 1,
  "status": "sealed",
  "witnesses": ["bob", "erin", "dave"],
  "open_request": {"request_id": 7, "status": "pending", "approval_count": 1}
}
```

`open_request` 为 `null` 表示当前无进行中请求；否则给出该请求的 `request_id`、
当前 `status`（`pending`/`waiting`/`expired`）与**有效票数** `approval_count`。
响应与其他未开封接口一样不含正文 `body`。

```bash
# 无进行中请求：直接换人（dave 无法履职 -> erin 接替）
curl -sS -X POST $BASE/documents/1/witness-replacements \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d '{"old_witness":"dave","new_witness":"erin"}'
# -> {"document_id":1,"status":"sealed","witnesses":["bob","carol","erin"],"open_request":null}

# 进行中请求里替换已投票的 carol：其同意失效，有效票清零，请求退回待同意
curl -sS -X POST $BASE/documents/1/witness-replacements \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d '{"old_witness":"carol","new_witness":"erin"}'
# -> {"open_request":{"request_id":7,"status":"pending","approval_count":0}, ...}

# 新见证人 erin 复用现有同意接口、凭封存摘要为本次请求补票
curl -sS -X POST $BASE/open-requests/7/approve -H "X-API-Key: $ERIN_KEY" \
  -H 'Content-Type: application/json' -d '{"digest":"ab12…"}'
# -> {"approval_count":1,"status":"pending"}

# 被替换的 carol 立即无权再同意
curl -sS -o /dev/null -w '%{http_code}\n' -X POST $BASE/open-requests/7/approve \
  -H "X-API-Key: $CAROL_KEY" -H 'Content-Type: application/json' -d '{"digest":"ab12…"}'
# -> 403

# 拒绝示例（均不改名单/票数，且不泄露正文）：
#   新见证人未注册 400；新见证人是封存人 400；新见证人已在名单 409；
#   被替换者不在当前名单 404；非封存人调用 403；资料已开封 409。
```

### 封存人交接（handover）

资料**未开封**时，封存人可向另一名本地账号发起交接邀请，受让人在确认截止前
凭本人密钥确认后成为新封存人。

- `POST /documents/{id}/handover-invitations`，请求体
  `{"to_username": "<受让人用户名>", "confirm_deadline": "<带时区 ISO-8601 时刻>"}`。
- 受让人必须**已注册**、**不是封存人本人**、且**不在当前见证人名单中**；
  `confirm_deadline` 必须带时区且晚于当前时间。邀请待确认期间，封存人也不能
  把受让人替换进见证人名单（`409`），保证确认后不会出现“新封存人兼见证人”。
- 每份资料同时**至多一份**待确认（`pending`）且未过期的邀请：重复发起 `409`；
  先撤回或等其逾期后可重新邀请。只有**封存人本人**可发起与撤回。
- 封存人可 `POST /handover-invitations/{id}/revoke` 撤回待确认邀请；
  受让人可 `POST /handover-invitations/{id}/confirm` 在截止前确认；
  `GET /handover-invitations/{id}` 查询状态（邀请双方或该资料当前参与方可见，仅元数据）。
- **已撤回、已逾期、已确认的邀请不能确认**；资料在邀请期间开封的，邀请连带
  失效（`expired`），同样不能确认。逾期判定既由后台扫描器执行，也在每个交接
  写事务内确定性再做一次，不依赖扫描及时性。
- 确认在同一写临界区内按顺序结算：
  1. 确定性处理到点开封与开封请求到期；
  2. 资料若**已开封**（含临界区内刚触发的到点开封）则拒绝交接（`409`），身份不变；
  3. 否则把进行中的开封请求置为 `invalidated`（若其已过截止时刻则按原规则
     `expired`），并**删除其全部旧同意**（旧同意永久失效，不能用于后续请求）；
  4. 转移封存人身份；**见证名单、正文、摘要均不变**。
- 交接成功响应（`200`）在邀请状态之外还返回：

  ```json
  {
    "invitation_id": 4,
    "status": "confirmed",
    "from_username": "alice",
    "to_username": "erin",
    "document_id": 2,
    "document_status": "sealed",
    "current_owner": "erin",
    "previous_owner": "alice",
    "witnesses": ["bob", "carol", "dave"],
    "old_open_request": {"request_id": 7, "status": "invalidated", "approval_count": 0}
  }
  ```

  `old_open_request` 为 `null` 表示无进行中开封请求；否则给出旧请求 `request_id`、
  终态（`invalidated`/`expired`）与清零后的票数。响应不含正文 `body`。
- 交接后**原封存人立即失去**该资料的查看/列表/审计与一切操作权限（统一 `403`，
  不区分资料是否存在）；**新封存人可重新申请开封**，从 0 票开始，由同三名
  见证人重新两票同意。见证人名单与权限不变。
- 成功操作返回邀请状态；每次成功发起/撤回/确认/逾期、旧请求失效与成功交接均
  追加审计（`handover_invited`、`handover_revoked`、`handover_confirmed`、
  `handover_invitation_expired`、`request_invalidated`、`request_expired`）。

```bash
# 发起交接邀请（确认截止时刻取 1 小时后，支持 +08:00 等带时区写法）
DEADLINE=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=1)).isoformat())")
curl -sS -X POST $BASE/documents/1/handover-invitations \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d "{\"to_username\":\"erin\",\"confirm_deadline\":\"$DEADLINE\"}"
# -> {"invitation_id":1,"status":"pending","to_username":"erin",...}

# 受让人 erin 凭本人 X-API-Key 在截止前确认
curl -sS -X POST $BASE/handover-invitations/1/confirm -H "X-API-Key: $ERIN_KEY"
# -> {"status":"confirmed","current_owner":"erin","previous_owner":"alice",
#     "witnesses":["bob","carol","dave"],
#     "old_open_request":{"request_id":7,"status":"invalidated","approval_count":0}}

# 原封存人随即无权访问；新封存人可重新申请开封
curl -sS -o /dev/null -w '%{http_code}\n' $BASE/documents/1 -H "X-API-Key: $OWNER_KEY"
# -> 403
curl -sS -X POST $BASE/documents/1/open-requests \
  -H "X-API-Key: $ERIN_KEY" -H 'Content-Type: application/json' \
  -d "{\"reason\":\"新封存人申请\",\"deadline\":\"$DEADLINE\"}"
# -> 201，新请求 approval_count 从 0 开始

# 撤回待确认邀请（封存人）；撤回/逾期后均返回 409 不能确认，可重新邀请
curl -sS -X POST $BASE/handover-invitations/1/revoke -H "X-API-Key: $OWNER_KEY"
# -> {"invitation_id":1,"status":"revoked",...}

# 拒绝示例（均不改状态、不转移身份、不泄露正文）：
#   受让人未注册/为本人/为见证人 400；确认截止时刻已过 400；时间不带时区 422；
#   非封存人发起/撤回 403；非受让人确认 403；无/错密钥 401；
#   重复发起待确认邀请 409；确认已撤回/逾期/已确认邀请 409；资料已开封 409。
```

### 到期清除正文（scheduled purge）

封存人可为资料安排一个带时区的未来清除时刻：到点清空数据库业务记录中的正文，
资料状态置为 `purged`，保留摘要、参与方（封存人/见证人）与审计元数据，
此后任何接口不再返回正文。到期前可改期或取消；交接后清除安排保留，
由新封存人管理，原封存人无权操作。

- `PUT /documents/{id}/purge`，请求体 `{"purge_at": "<带时区 ISO-8601 时刻>"}`：
  未安排过时为**安排**，已安排过时为**改期**；`purge_at` 必须带时区且**晚于当前时间**
  （不带时区 `422`、已过 `400`）。已开封资料同样可安排（到点照常清除正文）。
- `DELETE /documents/{id}/purge`：**取消**已安排的清除；没有安排时 `409`。
- 只有**当前封存人**可安排/改期/取消（其他人 `403`，不区分资料是否存在）；
  交接确认后原封存人立即失去该权限。
- **到期必清除**：后台扫描器、每个写事务与资料查询都会先做确定性到点清除判定，
  即使扫描器延迟或停机（重启时恢复扫描也会补清），后续查询与操作都呈现已清除结果。
  响应中的 `purged_at` 记为约定的清除时刻，与处理及时性无关。
- **连带结算**（与交接同一写临界区语义）：未开封资料的进行中开封请求置
  `invalidated`（已过截止时刻的按原规则 `expired`）并删除全部旧同意；
  待确认交接邀请置 `expired`。
- **时序规则**：清除与最早开封同刻时**清除优先**（不开封，请求失效）；
  最早开封时刻严格更早时**先开封**，到清除时刻再清正文（审计依次为
  `opened`、`purged`）；即使处理延迟到两个时刻之后，也按时刻比较得出同一历史。
- **已清除资料**不能再提交开封请求、同意、替换见证人、发起/确认交接或再安排
  清除（均 `409`）；已到期的改期或取消同样 `409`；一切拒绝路径不泄露正文。
- 成功响应 `200`：

  ```json
  {
    "document_id": 1,
    "status": "sealed",
    "purge_at": "2026-10-02T00:00:00+00:00",
    "previous_purge_at": null
  }
  ```

  改期时 `previous_purge_at` 为原时刻；取消响应为
  `{"document_id": 1, "status": "sealed", "purge_at": null}`。
  已清除资料的详情返回 `status: "purged"`、`purge_at`、`purged_at`、`digest`
  与见证名单等元数据（曾开封过的还保留 `opened_at`、`witness_approvals`），
  不含 `body`。
- 审计动作：`purge_scheduled`（安排）、`purge_rescheduled`（改期）、
  `purge_canceled`（取消）、`purged`（清除生效），连带结算复用
  `request_invalidated` / `request_expired` / `handover_invitation_expired`。

```bash
# 安排 24 小时后清除（支持 +08:00 等带时区写法）
PURGE_AT=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=24)).isoformat())")
curl -sS -X PUT $BASE/documents/1/purge \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d "{\"purge_at\":\"$PURGE_AT\"}"
# -> {"document_id":1,"status":"sealed","purge_at":"…","previous_purge_at":null}

# 改期到 48 小时后
PURGE_AT2=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=48)).isoformat())")
curl -sS -X PUT $BASE/documents/1/purge \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d "{\"purge_at\":\"$PURGE_AT2\"}"
# -> {"document_id":1,"status":"sealed","purge_at":"…","previous_purge_at":"…"}

# 到期前取消
curl -sS -X DELETE $BASE/documents/1/purge -H "X-API-Key: $OWNER_KEY"
# -> {"document_id":1,"status":"sealed","purge_at":null}

# 到期后查询（扫描器或查询本身触发确定性清除）：
curl -sS $BASE/documents/1 -H "X-API-Key: $OWNER_KEY"
# -> {"document_id":1,"status":"purged","purge_at":"…","purged_at":"…",
#     "digest":"ab12…","witnesses":["bob","carol","dave"], ...}   # 无 body

# 拒绝示例（均不改状态、不泄露正文）：
#   非封存人安排/取消 403；无/错密钥 401；清除时刻已过 400；不带时区 422；
#   资料已清除仍安排/改期/取消 409；清除时刻已到仍改期/取消 409；
#   没有安排仍取消 409；已清除资料再开封/替换见证人/交接 409。
```

### 调用示例（curl）

```bash
BASE=http://127.0.0.1:8000

# 注册（api_key 只显示这一次，请保存）
curl -sS -X POST $BASE/register -H 'Content-Type: application/json' \
  -d '{"username":"alice"}'
curl -sS -X POST $BASE/register -H 'Content-Type: application/json' -d '{"username":"bob"}'
curl -sS -X POST $BASE/register -H 'Content-Type: application/json' -d '{"username":"carol"}'
curl -sS -X POST $BASE/register -H 'Content-Type: application/json' -d '{"username":"dave"}'

# 封存
curl -sS -X POST $BASE/documents \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d '{"body":"遗嘱正文……","witnesses":["bob","carol","dave"]}'
# -> {"document_id":1,"status":"sealed","digest":"ab12…","witnesses":[...]}

# 开封前查询（无 body）
curl -sS $BASE/documents/1 -H "X-API-Key: $OWNER_KEY"

# 提交开封请求（截止时刻取 24 小时后）
DEADLINE=$(python3 -c "import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=24)).isoformat())")
curl -sS -X POST $BASE/documents/1/open-requests \
  -H "X-API-Key: $OWNER_KEY" -H 'Content-Type: application/json' \
  -d "{\"reason\":\"家庭会议\",\"deadline\":\"$DEADLINE\"}"
# -> {"request_id":1,"status":"pending",...}

# 两名见证人分别同意，digest 必须是封存响应里的摘要
curl -sS -X POST $BASE/open-requests/1/approve \
  -H "X-API-Key: $W1_KEY" -H 'Content-Type: application/json' \
  -d '{"digest":"ab12…"}'
# -> {"approval_count":1,"status":"pending"}
curl -sS -X POST $BASE/open-requests/1/approve \
  -H "X-API-Key: $W2_KEY" -H 'Content-Type: application/json' \
  -d '{"digest":"ab12…"}'
# -> {"approval_count":2,"status":"opened"}

# 开封后查询：body / digest / reason / witness_approvals 全部可见
curl -sS $BASE/documents/1 -H "X-API-Key: $OWNER_KEY"

# 操作记录（按时间顺序）
curl -sS $BASE/documents/1/audit -H "X-API-Key: $OWNER_KEY"
```

可直接运行的端到端示例（服务启动后）：

```bash
bash examples/demo.sh                    # curl 版（未设最早开封时刻的基础流程）
python3 examples/demo.py                 # 纯标准库 Python 版
python3 examples/demo_scheduled.py       # 最早开封时刻：waiting -> 自动开封；补票立即开封
python3 examples/demo_replace_witness.py # 见证人替换：直接换人 / 退票补票 / waiting 退回待同意
python3 examples/demo_handover.py        # 封存人交接：邀请/撤回/逾期/确认失效旧请求/转移身份/开封后邀请失效
python3 examples/demo_purge.py           # 到期清除：安排/改期/取消/到点清除/连带失效/时序规则/交接后管理权
```

### 典型拒绝场景

```bash
# 错误密钥 -> 401，不产生任何记录
curl -sS -o /dev/null -w '%{http_code}\n' $BASE/me -H 'X-API-Key: wrong'

# 非见证人同意 -> 403；摘要不一致 -> 409；截止后同意 -> 409；
# 同一见证人再次同意 -> 409（只计一次）；开封后再提请求 -> 409（重复开封）
```

## 运行测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

测试覆盖：注册与密钥、封存校验、两票开封全流程、重复同意、撤销后旧同意失效、
到期失效、最早开封时刻校验、`waiting` 待开封与到点自动开封、到点时票不足补齐
立即开封、迟到撤销/迟到扫描不能作废已到点开封、停机期间到点的重启恢复、
开封前正文零泄露、审计顺序，以及基于真实 uvicorn + 多线程的
**并发同意/同意撤销赛跑**（验证只产生一个已提交终态）和**重启一致性**；
另含见证人替换：直接换人后旧见证人失去权限、进行中请求退票（被替换者同意失效、
其他同意保留）、新见证人凭摘要补票开封、`waiting` 替换投票者退回 `pending`
（替换未投票者维持 `waiting`）、到点两票已齐先开封并拒绝替换、过截止请求先
按原规则失效再换人、替换后冷读库重启一致，以及**并发替换 vs 第二票同意赛跑**
（验证锁内以当前名单复核身份、只产生一个终态）；
再含封存人交接：发起校验（受让人已注册/非本人/非见证人、截止时刻带时区且在未来）、
每份资料唯一待确认邀请、撤回与重新邀请、逾期不能确认且可重新邀请、确认转移身份
（原封存人随即 403、见证人名单/正文/摘要不变）、旧开封请求 `invalidated` 与旧同意
清零、过截止旧请求按原规则 `expired`、`waiting` 请求随交接失效且不开封、到点两票
已齐先开封并拒绝交接、资料开封连带邀请失效、交接结果冷读库一致，以及真实 uvicorn +
多线程的**并发确认 vs 撤回、并发确认 vs 第二票同意赛跑**（两种终态互斥且唯一，
成功交接与请求失效均留审计）；
最后含到期清除：安排/改期/取消与权限校验（仅当前封存人、时刻带时区且在未来）、
扫描器到点清除与无扫描器时查询/写事务的确定性清除、清除后任何接口不返回正文且
库中正文已清空、未开封资料清除连带（进行中请求 `invalidated`、旧同意删除、待确认
邀请 `expired`）、已开封资料按期清除（摘要/开封时刻/见证记录保留）、同刻清除优先、
开封更早先开后清（含延迟处理补开）、清除更早则不开封、已清除资料全部写操作 `409`、
已到期的改期/取消被拒且清除先生效、交接后清除管理权转移、停机期间到点的重启恢复、
冷读库一致、旧库（无 purge 列/旧 CHECK 约束）幂等迁移，以及真实 uvicorn + 多线程的
**并发确认 + 同意 vs 到点清除**（两者均被拒，唯一 `purged` 终态）。

## 项目结构

```
app/
  config.py     # 环境变量配置
  db.py         # SQLite 连接、建表与旧库幂等迁移、BEGIN IMMEDIATE 写事务、时间规范
  schemas.py    # 请求模型（含带时区 deadline / earliest_open_at、替换见证人、交接邀请、清除时刻入参校验）
  main.py       # FastAPI 路由、认证、定时开封+到期清除+过期扫描器、全部业务规则（见证人替换、封存人交接、到期清除）
init_db.py      # 幂等初始化入口
entrypoint.sh   # 容器入口（初始化 + uvicorn）
examples/       # demo.sh / demo.py / demo_scheduled.py / demo_replace_witness.py / demo_handover.py / demo_purge.py 端到端示例
tests/          # pytest 测试（功能、定时、替换见证人、交接、到期清除、并发、重启）
Dockerfile
docker-compose.yml
```
