# 需求响应结算重算编排平台

省级负荷聚合商每月向电网公司提交客户级需求响应结算结果。本平台把「来源登记 → 计算图 → 运行版本 → 账本发布 → 调整凭证」串成一条可追溯、可重算、不可静默覆写的流水线。

- 前端：Vue 3 + Vite（运行看板、来源登记、差异与追溯、账本与凭证、调整台账、联调验收）
- 后端：FastAPI + SQLAlchemy 2 + PostgreSQL 18（OpenAPI 3.x，Swagger UI 在 `/docs`）
- 交付：Dockerfile / docker-compose 一键启动、可回放虚构数据、pytest 与前端单测、联调验收场景

## 1. 业务规则如何落到系统里

| 需求 | 实现 |
| --- | --- |
| 来源登记保留批次与适用版本 | `meter_batches`（同计量点 version 递增、`supersedes_batch_id`、checksum 幂等）、`baseline_rules`（rule_code+version）、`contracts`（customer+version），全部只追加不更新 |
| 计算图按客户×活动窗口串联 | 每个窗口 4 个节点：`ingest 补数 → baseline 基线 → response 响应电量 → settlement 结算`，节点间写 `lineage_edges`，来源写 `node_sources` |
| 每次运行固定输入截止批次与规则快照 | `runs.cutoff_at`、`runs.batch_snapshot`、`runs.rule_snapshot`、`runs.contract_snapshot`；所有取值查询都带 `submitted_at <= cutoff` 与版本生效日判断 |
| 账本发布只接收完整成功的运行 | `POST /runs/{id}/publish`：运行状态必须 `success` 且所有窗口成功，否则 409；重复发布 409 |
| 从一笔调整追到参与计算的来源 | `GET /adjustments/{id}/trace` 返回账本链、凭证链、运行快照、每个结算节点的批次/规则/签约来源 |
| 迟到批次 → 定向重算 或 整期不可变重跑 | `strategy=targeted`（按血缘+覆盖推断定位受影响窗口，其余窗口复用）与 `strategy=full_period`（全部重算）；两者都生成新运行版本 |
| 新版本不得覆写已发布账本 | 旧账本行状态置为 `superseded` 并链接 `superseded_by_id`，金额/电量不变；差额写 `adjustments`（待审批） |
| 获批差额记为调整 | 审批后生成独立 `ADJV-*` 调整凭证，引用原 `STV-*` 凭证号；原凭证金额不变 |
| 同一客户窗口只能一个运行取得输入快照 | `run_window_claims` + 唯一部分索引 `(customer_id,event_id) WHERE status='active'`；冲突方 409 并回滚，运行结束释放；进程崩溃由启动时 `release_abandoned_claims` 兜底 |
| 基线失败/样本不足 → 下游待核 | baseline 置 `needs_review`，response/settlement 一律 `blocked` 且无输出、无账本行；整次运行 `needs_review`，发布被拒 |
| 签约修订晚于付款入账 → 关联调整 | 付款走 `vouchers(kind=statement)`；修订只需新增签约版本 + 重算 + 发布，差额进入待批调整，批准后独立出 `kind=adjustment` 凭证 |

### 定向重算 vs 整期重跑（选择依据）

样例规模为 6 客户 × 3 活动 = **18 个窗口/结算期**，日常修订以「单客户签约调整」「零星补传批次」为主，因此：

- **默认用定向重算**：`find_affected_windows` 先查血缘（`node_sources`）再按批次覆盖区间/签约生效日/规则生效区间推断，避免新来源漏算；未受影响窗口克隆最近一次成功运行的四个节点（保留 `reused_from_node_id` 与来源行），因此血缘仍完整。
- **规则修订、批量补传、对账前**用整期重跑：一致性核对最直接，代价是窗口数线性增长。
- 两种方式都生成新运行版本并保留已发布账本；已用埋点对比：定向重算典型 `computed=1 / reused=17`，整期重跑 `computed=18 / reused=0`（看板「重算策略效率」实时统计）。

## 2. 一键启动（Docker Compose）

```bash
docker compose up --build
```

- 前端控制台：http://localhost:5173 （nginx 托管，`/api` 反代到后端）
- API 与 Swagger UI：http://localhost:8000/docs （OpenAPI JSON：`/openapi.json`）
- 首次启动自动建表并灌入虚构数据（6 客户 / 3 活动 / 31 天 × 15 分钟批次 / 规则 v1 / 签约 v1）

> 说明：本次交付环境未安装 Docker CLI，Dockerfile / compose 已按上述结构编写但未在本容器内实际构建；本地开发方式（§3）已实际启动并验证。

## 3. 本地开发（容器内已预装运行时）

```bash
# 1) 后端
cd backend
/opt/python-3.12/bin/python3 -m venv .venv            # 若尚未创建
PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
  .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

# 2) 前端
cd frontend
/opt/node-22/bin/npm install --registry=https://registry.npmmirror.com
/opt/node-22/bin/npm run dev            # http://localhost:5173 ，/api 代理到 8000

# 可选：重置演示数据
backend/scripts/seed.sh
```

环境变量：`DATABASE_URL`（默认 `postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev`）、`DB_SCHEMA`（默认 `drs`）、`SECRET_KEY`、`AUTO_SEED`。

## 4. 测试账号

| 用户名 | 密码 | 角色 | 权限 |
| --- | --- | --- | --- |
| planner | planner123 | 计划员 | 登记批次/规则/签约、发起运行、登记付款 |
| approver | approver123 | 审批员 | 计划员全部 + 发布账本、审批调整、执行验收场景 |
| viewer | viewer123 | 只读 | 查看所有看板与追溯 |

## 5. 验证方式

### 5.1 自动化测试（已实际执行）

```bash
cd backend && .venv/bin/python -m pytest -q        # 9 passed
cd frontend && /opt/node-22/bin/npm test           # 3 passed (vitest + happy-dom)
```

后端用例覆盖：快照冻结与正数结算、发布只追加/重复发布 409、迟到批次定向重算的复用与血缘、基线样本不足导致下游 blocked 且无账本、并发窗口抢占、付款后签约修订生成关联调整凭证、规则修订整期重跑差异、批次重复/缺口校验、新批次无血缘时的影响面推断。

### 5.2 联调验收（看板「联调验收」页，或命令行）

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"approver","password":"approver123"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')
curl -s -X POST localhost:8000/api/demo/scenarios/run -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{}'
```

每个场景都会重置并重灌虚构数据后真实执行，返回逐步 `ok` 明细：

1. `baseline_shortfall`：样本不足 → baseline 待核、下游 blocked、发布 409
2. `concurrency_claim`：双线程抢同一窗口 → 恰好一个成功、一个 409，锁释放
3. `late_batch_targeted`：发布 v1 + 付款 → 补传批次 → 定向重算（1 重算 / 17 复用）→ 发布 v2 生成调整 → 原账本与 STV 凭证不变 → 批准生成 ADJV
4. `late_contract_adjustment`：付款后缩小签约容量 → 定向重算 → 关联调整 → 原凭证金额不变
5. `rule_revision_full_period`：规则 v2 → 整期重跑 → 逐窗口差异与总差额

本次交付中上述 5 个场景通过 HTTP API 实际执行结果：`overall ok: true`（12–14 s），4+3+7+6+4 步全部通过；`pytest` 9/9、`vitest` 3/3 通过。

### 5.3 手工验证要点

- **并发**：两个标签页同时点「整期不可变重跑」，一个成功、一个收到 409「窗口输入快照已被其它运行占用」。
- **失败传播**：来源登记 → 基线规则 → 新增「最少样本 6 天」版本 → 运行看板发起整期重跑 → 窗口全部待核；点「发布」被拒。
- **迟到修订**：账本页对现行账本「登记付款」→ 来源登记补传批次或签约修订 → 差异与追溯「来源影响面」→ 定向重算 → 发布 → 调整台账批准 → 凭证页出现 ADJV，STV 金额不变。

## 6. 目录结构

```
backend/app
  models.py            # 版本化来源、运行、节点、血缘、账本、调整、凭证、窗口锁
  services/engine.py   # 四节点计算图与失败传播
  services/snapshot.py # 截止时间/版本生效日解析
  services/registry.py # 批次、规则、签约登记
  services/orchestrator.py # 运行编排、窗口锁、发布、调整
  services/demo.py     # 可回放联调场景
  routers/             # auth/registry/lineage/runs/ledger/adjustments/demo
frontend/src/views     # 总览、来源登记、运行看板、运行详情、差异与追溯、账本、调整、联调验收
demo/shots             # 实际运行截图（headless Chrome 驱动真实页面）
```

## 7. 界面截图（实际运行）

| 页面 | 文件 |
| --- | --- |
| 登录 | `demo/shots/01-login.png` |
| 总览 | `demo/shots/02-overview.png` |
| 来源登记 | `demo/shots/03-registry.png` |
| 运行看板 | `demo/shots/04-runs.png` |
| 运行详情与血缘（定向重算 1 重算 / 17 复用） | `demo/shots/04b-run-detail.png`、`04c-run-lineage.png` |
| 差异对比 | `demo/shots/05-diff.png` |
| 来源影响面 | `demo/shots/05b-impact.png` |
| 调整追溯（STV → ADJV 凭证链） | `demo/shots/05c-trace.png` |
| 账本与凭证 | `demo/shots/06-ledger.png` |
| 调整台账 | `demo/shots/07-adjustments.png` |
| 联调验收 | `demo/shots/08-acceptance.png` |

## 8. 已知取舍

- 运行采用同步执行（单进程内完成），窗口锁与失败传播因此可以直接用 HTTP 并发复现；真实规模下应改为任务队列，但锁语义与发布门禁保持不变。
- 基线方法固定为「活动前 N 天同 15 分钟窗口均值 × 调整系数」，规则版本仅调整参数与样本门槛；方法扩展点已在 `BaselineRuleVersion.method`/`config` 预留。
- 结算电量按「签约容量 × 活动时长」封顶，负响应按 0 计结算电量但仍保留原始响应电量供追溯。
