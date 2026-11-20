# 环评公示每日归档 · 续采与引文追溯 API

按 UTC 分钟区间对公示源做**有界续采**，把「已收齐的范围 + 公告」和「仍需续查的前沿区间」一起存进 SQLite。
每轮先核对日版号、最多检索 6 次，结果少于 5 条才算该段收齐；满额就二分缩区间，单分钟仍满则转人工补查。
发现更高版号会撤下当天旧版公告与覆盖结论，迟到的旧响应无法把它们恢复。

月报引文可按月报号一次登记清单（UTC 公示日、公告编号、标题、发布时间），整份清单与同一份已提交
现行归档逐条相符后才落库，并留存每条引文当时的日版号。换版续采时自动核对：同号公告改了标题或发布
时间记为**改稿**，新版全天收齐后仍找不到同号公告记为**撤下**；每条引文只留首次确认的证据，恢复原文
也不会覆盖。

## 快速开始

```bash
docker compose up --build
```

- 归档服务（含 Swagger）：http://localhost:8080/docs
- 本地公示源（含 Swagger）：http://localhost:8081/docs
- 续采演示脚本：`./scripts/demo.sh`
- 引文追溯演示脚本：`./scripts/demo-citations.sh`

切换公示源数据版本（无需重建镜像）：

```bash
SOURCE_DATASET=v2 docker compose up --build
# 或对运行中的源热切换：
curl -X POST localhost:8081/v1/admin/version -H 'content-type: application/json' -d '{"version":"v2"}'
```

本地不用 Docker 时：

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
SOURCE_DATA_DIR=./datasets .venv/bin/python -m uvicorn source.main:app --port 8081 &
SOURCE_BASE_URL=http://localhost:8081 ARCHIVE_DB_PATH=./archive.sqlite3 \
  .venv/bin/python -m uvicorn app.main:app --port 8080
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/collection/rounds` | 跑一轮续采：核对版号 → 检索至多 6 次 |
| GET | `/api/days/{day}` | 按日查询公告、已收齐范围、待查范围、需人工补查范围 |
| GET | `/api/days/{day}/version` | 归档中该日的日版号 |
| POST | `/api/citation-registrations` | 按月报号登记整份引文清单（201） |
| GET | `/api/citation-registrations/{report_no}` | 按月报号追溯原引文与变动证据 |
| GET | `/health` | 健康检查 |

公示源（模拟上游）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/days/{day}/version` | 当日版号 |
| GET | `/v1/days/{day}/notices?start_minute=&end_minute=` | 分钟区间检索，最多 5 条，**无总数** |
| GET/POST | `/v1/admin/version` | 查看/切换数据集版本 |
| GET/POST | `/v1/admin/delay` | 注入响应延迟，用于演示迟到响应 |

### 一轮续采的返回

```json
{
  "day": "2026-11-20",
  "status": "partial",
  "day_version": "2026-11-20.v1",
  "queries_used": 2,
  "version_reset": false,
  "collected_ranges": [{"start_minute": 540, "end_minute": 584, "from_time": "09:00", "to_time": "09:44"}],
  "manual_ranges": [],
  "pending_ranges": [{"start_minute": 585, "end_minute": 1439, "from_time": "09:45", "to_time": "23:59"}],
  "notices_collected": 4,
  "detail": null
}
```

`status` 取值：`collected`（当天已全部收齐）、`partial`（本轮预算用尽，仍有待查区间）、
`needs_manual_review`（只剩人工补查时段）、`source_error`（源不可用，可原样重试）、
`stale_response`（收到旧版迟到响应，已丢弃）、`version_changed`（检索中发现更高版号，已撤旧版）。

`POST /api/collection/rounds` 的 `max_queries` 可把单轮预算压到 1..6；服务上限固定为 6。

### 按日取数

`GET /api/days/2026-11-20` 返回 `day_version`、`complete`、`notices`、`completed_ranges`、
`pending_ranges`（待查）、`manual_ranges`（需人工补查，带 `reason`）。
`complete=true` 要求既无待查区间、也无人工补查区间。

## 引文登记与追溯

### 登记（POST `/api/citation-registrations`）

请求体按月报号给出整份清单，每条含 `day`（UTC 公示日）、`announcement_no`、`title`、`published_at`：

```json
{
  "report_no": "EIA-MONTHLY-2026-11",
  "citations": [
    {
      "day": "2026-11-19",
      "announcement_no": "HB-EIA-2026-1119-01",
      "title": "云岭风电场二期工程环境影响报告书公示",
      "published_at": "2026-11-19T08:00:00Z"
    }
  ]
}
```

- 整份清单必须与**同一份已提交的现行归档**逐条相符（同日同号，标题与发布时间一致）。
  任一条不符即整单拒绝（`409`，返回每条不符原因），不写入任何半截数据。
- 登记成功后每条引文留存登记时的日版号 `day_version`；同一月报号不可重复登记（`409`）。
- 清单为空、日期/时间格式非法、清单内重复引文返回 `422`。

### 追溯（GET `/api/citation-registrations/{report_no}`）

返回不可变的原引文 `citations` 与首次确认的变动证据 `evidence`；未登记的月报号返回 `404`。
证据规则：

1. **改稿（`revised`）**：换版续采时，同日同号公告一旦入档且标题或发布时间与引文有出入，
   在该次入档事务内立即记一条证据，附日版号、新标题 `new_title` 与新发布时间 `new_published_at`。
2. **撤下（`withdrawn`）**：只有新版**全天收齐**（无待查区间、无人工补查区间）后仍找不到同号
   公告才记证据；收集中的缺失不作数。该证据不带新标题/新时间（两个字段为 `null`）。
3. **只留首次**：每条引文最多一条证据（库表 UNIQUE 约束兜底）。后续即使公告恢复原文/原时间，
   首次证据仍然保留，原引文文本也永不被改写。
4. **同事务生效**：证据写入与归档公告/覆盖范围的写入在同一个 SQLite 事务内，任一失败整体回滚。
5. **重启可查**：引文与证据都在 SQLite，进程重启后按月报号原样查回。

### 演示数据集

`datasets/` 在 `v1`、`v2` 之外新增两版，供完整演示三类变动：

| 版本 | 相对上一版的变化 |
| --- | --- |
| `v3` | `1119-02` 发布时间 12:00 → 12:30；`1121-05` 被撤下 |
| `v4` | 标题/时间恢复为 v1 原文，`1121-05` 重新发布（验证首次证据不被覆盖） |

`./scripts/demo-citations.sh` 会依次演示：v1 登记 → v2 改标题 → v3 改时间/撤下（含未收齐不记撤下的
中间断言）→ v4 恢复原文后证据仍在。重跑前需重置归档库（`docker compose down -v` 或删除
`ARCHIVE_DB_PATH`），因为归档的日版号按设计不允许回退。

## 规则与状态机

1. **先核对版号**：每轮开始先查源上的日版号；与归档不同则先处理版本（见下），再决定检索范围。
2. **有界检索**：每轮最多 6 次检索，不论待查区间有多少。
3. **少于 5 条即收齐**：整个被检索区间（可能跨多分钟）一次性标记为收齐并落库。
4. **满额缩区间**：返回 5 条时把区间二分，两个半段进「前沿」队列；下一轮从队列头部继续。
5. **单分钟仍满 → 人工**：区间已是一分钟且返回 5 条，记入 `manual_ranges`，不再重复检索。
6. **失败可重试**：版号查询或检索失败时，已完成的进度保留，未完成区间仍在队列里，重跑即可。
7. **重启接着查**：前沿队列与已收齐范围都在 SQLite，进程重启后从队首继续，不会整天重扫。

### 版本升级与迟到响应

日版号按「后缀数字优先，否则字符串序」比较（本地源形如 `2026-11-20.1`）。

- 预检发现更高版号：删除当天 `notices` / `completed_ranges` / `manual_ranges` / `frontier_ranges`，换新版号后重新排空整天。
- 检索响应自带更高版号：同一次事务内撤下旧数据，结束本轮（`version_changed`）。
- 检索响应带更低版号（迟到旧响应）：整页丢弃（`stale_response`），不写公告、不改覆盖范围。
- 写入事务内还会再核对一次版号，所以「取数后、落库前」被别的轮次抢先升级也不会写脏数据。

## 目录结构

```
app/          归档服务：collector 状态机、SQLite 存储、引文登记/追溯、源客户端、FastAPI 路由
common/       版本比较、UTC 时间解析等共享工具
source/       本地可换版公示源（内存数据集 + 管理端点）
datasets/     v1..v4 演示数据
tests/        单元 + 端到端 + 迟到响应竞态 + 引文追溯测试
scripts/      demo.sh（续采）、demo-citations.sh（引文追溯）演示脚本
```

## 测试

```bash
python3 -m pytest tests/ -q
```

覆盖：稀疏日一轮收齐、全天空日、密集日二分收敛、单分钟转人工、每轮预算上限、
版号预检/检索中发现升级、迟到旧响应丢弃、事务内版号竞态、源故障后重试、
进程重启续查、API 契约与 Swagger、真实延迟下的迟到响应竞态，以及引文登记的归档相符校验、
改稿（标题/发布时间）、全天收齐后才记撤下、首次证据不可覆盖、证据与归档同事务回滚、
重启后按月报号查回原引文与证据。
