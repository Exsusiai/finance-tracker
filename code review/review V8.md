# Finance Tracker Code Review V8

> 审查日期: 2026-06-25  
> 审查范围: 当前未提交的 V7 修复代码、相关新增 migration / tests / docs，以及 V7 review 中所有问题的回归确认。  
> 审查重点: 资金计算与记账逻辑、代码结构/架构风险、安全与隐私。本轮只新增此 review 文档，没有修改业务代码。

## 验证结果

- Git 状态: 当前存在未提交改动，包含 V7 修复代码、`backend/alembic/versions/c3d4e5f6a7b8_add_asset_holding_source.py`、`backend/tests/test_review_v7.py`、`code review/review V7.md` 等。
- 后端测试: `../.venv/bin/pytest` 通过，结果为 `360 passed, 15 skipped`。
- 前端构建: `npm run build` 通过。
- Alembic head: `../.venv/bin/alembic heads` 返回单一 head `c3d4e5f6a7b8`。
- secret 扫描: tracked files 中没有发现真实 API key / token / private key；命中项只有 `.env.example`、测试 fixture 名称和 `package-lock` 中的 `queue-microtask` 字符串。`.env` 与 `frontend/.env.local` 仍未被 git 跟踪。

## V7 修复状态

- 已修复: 券商 `/accounts/balances` 的 base-currency 误标问题已改为折算到账户币种；REST `/cashflow/monthly`、`/cashflow/recompute`、`by-category` 已接入 paired transfer 去重；Account PATCH 已守住 crypto/exchange -> USDT；GoCardless `/institutions` 已改 POST body，`create_connection` 已使用显式 country；Notion asset summary 已改读 `v_account_balance`；Gemini raw output 日志与 TR 临时 cookie 清理已修；V7 regression tests 已新增并通过。
- 部分修复: 快照账户禁现金腿仍主要覆盖 UI、`adjust-balance` 和 `net_worth`，但直接 API 创建账户/交易仍能写入 ledger；`asset_holdings.source` 能保护明确的手工持仓，但历史 broker 持仓和多 provider 同资产仍有口径问题；LLM dispatch 去掉了中途 commit，但 after-commit hook 的 rollback/session-reuse 边界仍不够干净。
- 未修复/已延后: MCP PDF import 仍不是完整 ingestion mirror；MCP snapshot recompute 仍没接 paired transfer 去重；`main.py` lifespan 拆分、大文件拆分、现金流 amount/subaccount 公式完全统一仍在 roadmap 中。

## 修复记录（2026-06-25）

逐条核实 V8 全部确认存在并已修复（P2-4 维持延后）。全套 **365 passed**。

| 项 | 结论 | 处理 |
|---|---|---|
| P1-1 历史券商持仓永不清零 | 确认 | `apply_broker_snapshot` 同步时**回收**历史已同步行（`source='manual'` + `last_synced_at` 非空 + `Asset.data_source==provider` → 认领为该 provider），随后正常 reset 清零卖出仓；回归 `test_resync_reclaims_and_zeroes_legacy_synced_holding` |
| P1-2 多 provider 同资产覆盖 | 确认（实际不可达） | broker-connection API 是**一账户一连接**（get/upsert/delete 按 account_id 唯一），不会出现两 provider 共账户；改正 model/migration 过度表述，不动 holding identity |
| P1-3 快照账户仍可写现金 ledger | 确认 | `AccountCreate` 拒绝 snapshot 非零初始余额；`POST /transactions` + batch 拒绝落到 snapshot 账户；`/accounts/balances` 与单账户 balance 对 snapshot **忽略 ledger**（只取持仓）；回归见 `test_review_v8.py` |
| P1-4 MCP snapshot recompute 双计 | 确认 | `_recompute_snapshot_sql` 加 paired dedup（与 REST/`get_cashflow` 一致） |
| P2-1 after_commit 无 rollback 清理 | 确认 | 增 `after_rollback` 取消 enqueue（rolled-back 不入队，含同 session 后续 commit）；回归 `test_rollback_then_commit_enqueues_nothing` |
| P2-2 PDF 失败状态可能丢失 | 确认 | `_mark_failed` 改用**独立 session** 提交 `failed`+error_message，不被外层 rollback 吞掉 |
| P2-3 GoCardless 写死 DE/EUR | 确认 | 落库用 `body.country` + 匹配到的 institution name/bic/country/logo |
| P2-4 MCP PDF 非完整镜像 | 确认 | **维持延后**（README/API 已注明不与 REST 等价） |
| P2-5 文档过度表述 | 确认 | 修正 ROADMAP（MCP live vs snapshot）+ migration/model 注释（source 不支持同账户多 provider 同资产） |

## P0

本轮没有发现新的 P0。

## P1 高优先级

### P1-1 历史券商持仓迁移后可能永远不会被清零

新增 migration 把已有 `asset_holdings.source` 统一默认成 `manual`，见 `backend/alembic/versions/c3d4e5f6a7b8_add_asset_holding_source.py:15-16`。broker sync 的清零逻辑现在只清 `AssetHolding.source == source` 的持仓，见 `backend/app/services/broker_sync/upsert.py:217-224`。现有持仓只有在本轮 broker fetch 里再次出现时才会在 `backend/app/services/broker_sync/upsert.py:171-178` 被认领成 `ibkr` / `traderepublic`。

影响: 对已经同步过的旧库，如果用户在券商里卖掉了某只股票，它不会出现在下一次 fetch 中，因此不会被“认领”，仍保持 `source='manual'`，也不会被 reset filter 清零。资产页和 net worth 会继续把这只已卖出的历史持仓算进去。

建议: migration 或启动修复需要回填历史 broker-owned holdings。可按 `Asset.data_source in ('ibkr','traderepublic')`、`MarketPrice.source`、`last_synced_at`、账户的 `broker_connections` 做保守回填；或者在 reset 里把 `source='manual'` 但 `asset.data_source == source` / `last_synced_at IS NOT NULL` 的旧同步持仓纳入一次性迁移。

### P1-2 `source` 字段没有进入 holding identity，多 provider 同资产仍会互相覆盖

`AssetHolding` 的唯一键仍是 `(account_id, asset_id, chain)`，见 `backend/app/models/__init__.py:366-370`。broker upsert 查找 holding 也只按这三列，见 `backend/app/services/broker_sync/upsert.py:148-156`；命中后直接覆盖 quantity / cost / source，见 `backend/app/services/broker_sync/upsert.py:171-180`。

影响: 一个 brokerage account 可以有多个 broker connection（模型允许 `(account_id, provider)` 唯一，见 `backend/app/models/__init__.py:682-684`），但如果 IBKR 和 TR 在同一账户下都持有同一资产，或者都上报 EUR/USD 现金，后同步的 provider 会覆盖前一个 provider 的 holding，而不是并存。V7 注释说“two providers sharing one account don't clobber each other”，但当前唯一键和 upsert 条件并不支持这个承诺。

建议: 如果允许一个账户多 provider，holding identity 需要包含 `source` 或更好的 `source_connection_id`，唯一键应变成 `(account_id, asset_id, chain, source_connection_id)`；如果产品上不允许多 provider 同账户，应在 API 层限制一个 brokerage account 只能有一个 broker connection，并调整文档。

### P1-3 快照账户仍可通过 API 写入现金 ledger，`/accounts/balances` 会继续叠加显示

V7 修复隐藏了前端 initial balance、后端拒绝 `adjust-balance`，并在 `net_worth` cash 腿排除了 snapshot accounts，见 `backend/app/api/v1/holdings.py:448-455`。但 `POST /accounts` 仍直接接受并保存 `initial_balance`，见 `backend/app/api/v1/accounts.py:98-106`；`POST /transactions` 也没有拒绝 `brokerage/crypto_wallet/exchange` 账户上的普通交易，见 `backend/app/api/v1/transactions.py:216-244`。

同时 `/accounts/balances` 对所有账户仍先加 `v_account_balance`，再加 holdings value，见 `backend/app/api/v1/accounts.py:144-153`。因此直接 API 创建 `brokerage` 且 `initial_balance=1000`，再同步出 200 的持仓，账户余额列表会显示 1200；crypto/exchange 也类似。

影响: `net_worth` 已有兜底，不会把这类 ledger 加进总资产；但资产页账户卡片、单账户 balance API、任何消费 `/accounts/balances` 的客户端仍会显示错误的 snapshot 账户余额。资金口径仍不是“快照账户没有现金腿”。

建议: 后端在 `AccountCreate` / create route 强制 snapshot account `initial_balance=0`；交易创建/批量创建/PDF import 应拒绝把普通交易落到 snapshot account，除非引入明确的“broker cash ledger”模型并与 holdings cash 分离。`/accounts/balances` 对 snapshot accounts 也应忽略 `v.balance` 或拆出 `ledger_balance` 字段。

### P1-4 MCP snapshot recompute 仍会双计 paired transfer

REST 现金流入口已经接入 `paired_dedup_predicate`，但 MCP 的 `_recompute_snapshot_sql()` 仍只排除 subaccount，没有 paired transfer 去重，见 `mcp-server/src/finance_mcp/server.py:121-137`。本轮只修了 MCP live `get_cashflow()` 的 main/category query，见 `mcp-server/src/finance_mcp/server.py:956-1000`。

影响: 通过 MCP `add_transaction` 或 `parse_bank_statement` 写入后会调用 `_recompute_period_sync()`，它刷新出的 `cash_flow_snapshots.transfer_total` 仍可能把一笔转账的两条腿都算进去。REST live monthly 和 snapshot service 会是正确值，MCP recompute 产生的 snapshot 可能是错误值。

建议: 在 MCP `_recompute_snapshot_sql()` 中同步 REST 的 paired dedup predicate；最好补一个 MCP 层 paired transfer recompute regression。

## P2 中优先级

### P2-1 after_commit listener 没有 rollback 清理，session 复用时可能入队已回滚交易

`_enqueue_llm_after_commit()` 在 `backend/app/services/ingestion/__init__.py:319-329` 注册 `after_commit` listener，但没有在 rollback 后移除。正常 FastAPI request 一般会丢弃 session，风险不大；但 service/test/script 如果同一个 session 先 rollback，再继续 commit 其他变更，listener 会在后续 commit 时把已回滚的 tx ids 入 LLM 队列。

相关测试 `backend/tests/test_llm_dispatch_race.py` 只验证 rollback 后“立刻没有入队”，没有验证 rollback 后同 session 再 commit 的情况。

建议: 同时监听 `after_rollback` / `after_soft_rollback` 取消 listener，或改成事务 outbox 表。更稳妥的做法是 ingestion 返回 `llm_target_ids`，由 route 的 after-success path 显式 enqueue。

### P2-2 PDF 失败状态可能不会持久化

`_mark_failed()` 先 `rollback()`，再设置 `PdfImport.status='failed'` 并 `flush()`，见 `backend/app/api/v1/statements.py:67-89`。但调用方随后继续 `raise ParserError`，例如 `backend/app/api/v1/statements.py:483-487` 和 `backend/app/api/v1/statements.py:781-783`；`get_db` 在异常路径会 rollback，见 `backend/app/db/session.py:50-58`。

影响: 解析/commit/reparse 失败时，失败状态和 error_message 可能被最终 rollback 掉，导入记录停留在旧状态（例如 awaiting_review / parsing），用户看不到真实失败原因。这不一定造成资金错误，但会影响失败恢复和用户判断。

建议: `_mark_failed()` 使用独立 session 提交失败状态，或 route 捕获后返回错误响应前显式 commit 失败状态并避免外层依赖回滚它。需要补失败路径 regression。

### P2-3 GoCardless 连接记录仍写死 `DE/EUR`

`create_connection` 已经用 `body.country` 做 institution lookup，但落库时仍写 `institution_country="DE"`、`currency="EUR"`，见 `backend/app/api/v1/bank_sync.py:133-138`。callback 只会根据账户更新 currency，见 `backend/app/api/v1/bank_sync.py:306-312`，不会更新 institution_country。

影响: 非德国银行连接会在连接列表/元数据里长期显示错误国家；如果后续逻辑按 connection country 分组或筛选，会产生功能错误。安全问题已修，这里是功能与数据质量问题。

建议: 落库时用 `body.country`；如果 institution list 返回了匹配 institution，也应保存 institution name / country / logo，而不是先写 `institution_id` 和 `DE/EUR` 默认值。

### P2-4 MCP PDF import 仍是延期项，资金语义仍不等价 REST

V7-P1-6 仍未修复，ROADMAP 已明确延期。当前 `mcp-server/src/finance_mcp/server.py:784-805` 仍直接插入 PDF 交易，没有 `fx_rate_to_base/base_amount`，也没有完整跨账户 transfer matcher、LLM fallback 或 audit 字段。

影响: 同一张 PDF 通过 REST 和 MCP 导入，外币现金流、跨账户转账、pending/分类审计仍可能不同。外币 PDF 在 MCP 路径下尤其容易被 cashflow 静默排除。

建议: 继续保留为独立修复项，但 README/API 文档应避免暗示 MCP 与 REST ingestion 已完全等价。

### P2-5 文档对 V7 修复状态略有过度表述

`docx/ROADMAP.md` 的 Sprint 9 表格写 paired-transfer 去重 “monthly/recompute/by-category/snapshot/MCP 全一致”，但当前 MCP snapshot recompute 未修（见 P1-4）。另外 `backend/alembic/versions/c3d4e5f6a7b8_add_asset_holding_source.py:11-13` 注释写 “two providers sharing one account don't clobber each other”，但当前 holding unique/upsert 仍会在同一资产上互相覆盖（见 P1-2）。

影响: 后续开发会按“已全修”的假设继续叠功能，容易漏掉 MCP snapshot 和多 provider 同资产这两个实际残留。

建议: 文档改成精确状态：MCP live `get_cashflow` 已修，MCP snapshot recompute 未修；`source` 只保护不同 asset 的手工/他 provider 持仓，不能支持同账户多 provider 同 asset 并存。

## 安全结论

本轮没有发现真实密钥进入 git。V7 的主要安全项中，GoCardless query string 凭据、Gemini raw output 日志、TR 登录失败临时 cookie 清理都已修复。当前安全风险主要是数据一致性/恢复路径，不是 token 泄漏。

## 建议修复顺序

1. 先修 P1-1 和 P1-2：`asset_holdings.source` 需要历史回填，并决定 holding identity 是否纳入 provider/source。
2. 再修 P1-3：从后端 API 层彻底禁止 snapshot account 写现金 ledger，或显式拆出 cash ledger 与 holdings cash。
3. 同步修 P1-4：MCP snapshot recompute 接入 paired dedup。
4. 然后修 P2-1 / P2-2：把 LLM enqueue 与 PDF failed 状态做成可靠事务边界。
5. 最后更新文档，避免把“部分修复”写成“全一致”。
