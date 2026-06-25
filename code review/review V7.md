# Finance Tracker Code Review V7

> 审查日期: 2026-06-25  
> 审查范围: 当前工作区完整代码、最新文档、V6 之后新增的券商同步 / PDF 预览入库 / LLM 队列 / 转账现金流修正，以及所有未提交代码状态。  
> 审查重点: 资金计算与记账逻辑、代码结构/架构膨胀、安全与隐私风险。本轮只新增此 review 文档，没有修改业务代码。

## 当前工作树与验证结果

- Git 状态: 当前没有 uncommitted / untracked 文件；本轮实际审查的是当前 HEAD 的完整代码。
- 后端测试: `../.venv/bin/pytest` 通过，结果为 `351 passed, 15 skipped, 1 warning`。warning 是 `tests/test_ingestion.py::test_adjustment_amount_keeps_sign` 结束时 aiosqlite worker thread 在 event loop close 后回调，暂未导致失败，但说明 async DB fixture / session teardown 仍有资源释放噪音。
- 前端构建: `npm run build` 通过，Next.js type/lint/build 全部成功。
- secret 检查: tracked files 中没有发现真实 API key / token / pem/key 文件；`.env` 和 `frontend/.env.local` 存在但均被 `.gitignore` 覆盖，未被 git 跟踪。
- 文档状态: `README.md` 仍写 `306 个单测全过`，`PROGRESS.md` 写 `351 passed`，当前实际收集 366 个测试、351 通过、15 跳过。README 的总体状态也还偏向 P1-4 加密钱包/CEX，未完整反映券商同步和 PDF 预览后入库。

## V6 后状态概览

- 已修复或明显改善: 交易 PATCH stale `base_amount/fx_rate_to_base` 清理已落地；余额视图 `metadata_json` malformed JSON 防护已落地；crypto/exchange 创建时 USDT invariant 已补；bank sync crypto key 已优先走 Settings；后端测试数量从 V6 的部分失败状态恢复到全绿。
- 部分修复但引入新风险: LLM dispatch race 通过 ingestion 中途 `commit()` 规避，但这个修法破坏了导入事务原子性；cashflow snapshot / by-category 做了 paired transfer 去重，但 `/cashflow/monthly`、`/cashflow/recompute` 和 MCP live cashflow 仍没同步。
- 仍未修复: GoCardless query string 凭据与 country bug、Notion asset summary 旧余额公式、MCP PDF import 不复用完整 ingestion。

## 修复记录（2026-06-25）

逐条核实——所有 P1 与 P2-1/2/3 均**确认存在并已修复**；纯结构治理（P2-4 余项 / P2-5 / P2-6）与 MCP 完整镜像（P1-6）评估后**延后**（见下）。全套测试 **360 passed**。

| 项 | 结论 | 处理 |
|---|---|---|
| P1-1 券商余额单位 | 确认 | `compute_brokerage_value_per_account` 折算到账户币种；回归 `test_review_v7::test_brokerage_balance_in_account_currency` |
| P1-2 快照账户现金腿双算 | 确认 | 前端隐藏初始/调整余额 + 后端 `adjust-balance` 拒绝 + `net_worth` cash 排除快照类；回归 `test_snapshot_account_rejects_adjust_balance` |
| P1-3 多入口 transfer 双计 | 确认 | `paired_dedup_predicate` 统一到 monthly/recompute/by-category/snapshot/MCP；回归 `test_monthly_counts_transfer_pair_once` |
| P1-4 ingestion 中途 commit | 确认 | 改 `after_commit` 钩子，单事务原子；重写 `test_llm_dispatch_race`（含回滚不派发） |
| P1-5 PATCH 绕过 USDT | 确认 | `update_account` 校验最终 `(type,currency)`；回归 `test_patch_cannot_break_crypto_usdt_invariant` |
| P1-6 MCP PDF 非完整镜像 | 确认 | **延后**（需抽共享 ingestion service，改动面大；现金流口径已与 REST 对齐） |
| P1-7 GoCardless 凭据 + country | 确认 | `/institutions` 改 POST + `BankConnectionCreate.country` |
| P1-8 Notion 旧余额公式 | 确认 | 改读 `v_account_balance` + `include_in_total` |
| P1-9 券商重置误删手工持仓 | 确认 | `asset_holdings.source` 列（迁移 `c3d4e5f6a7b8`）+ 同 source 才重置；回归 `test_resync_leaves_manual_holding_untouched` |
| P2-1 券商资产身份冲突 | 确认 | 同 symbol 不同 conid 用 `contract=conid` 消歧；回归 `test_same_symbol_different_conid_does_not_merge` |
| P2-2 Gemini 日志泄隐私 | 确认 | abstain 日志只记 len/empty/token，不记原始输出 |
| P2-3 TR 临时 cookie 残留 | 确认 | 登录失败 `unlink(missing_ok=True)` |
| P2-4 公式分散 | 部分 | paired 谓词已统一；amount/subaccount 谓词仍分散，**随 P2-5/6 一起重构** |
| P2-5 lifespan 过重 / P2-6 大文件 | 确认 | **延后**（纯结构重构，独立排期；见 ROADMAP「下一步」） |
| P2-7 文档漂移 | 确认 | 已更新 README / PROGRESS / ARCHITECTURE / API / ROADMAP |
| P2-8 测试缺口 | 确认 | 新增 `test_review_v7.py` + broker/dispatch 回归 |

## P0

本轮没有发现新的 P0。下面的 P1 都直接影响资金口径、资产估值、凭据暴露面或数据可恢复性，应优先处理。

## P1 高优先级

### P1-1 券商账户余额把 base-currency 持仓市值当作账户币种返回

`backend/app/api/v1/accounts.py:123-149` 从 `v_account_balance` 读取账户币种和交易余额，然后把 `compute_brokerage_value_per_account(..., get_settings().base_currency)` 的结果直接相加并仍返回 `currency=r[2]`。但 `backend/app/services/wallet_sync/holdings_value.py:83-95` 明确说明该函数返回的是 base-currency value，`backend/app/services/wallet_sync/holdings_value.py:126-130` 也会把每个持仓折算到 base currency。

影响: 如果 base currency 是 CNY，而 brokerage 账户 currency 是 EUR/USD，`/accounts/balances` 会把 10,000 CNY-equivalent 持仓显示成 10,000 EUR/USD。前端又在 `frontend/src/app/assets/page.tsx:1405-1422` 用账户币种格式化并再次按账户币种做 display-currency 换算，错误会被放大。现有 broker 测试 helper 在 `backend/tests/test_broker_sync.py:98-104` 固定创建 `currency="CNY"`，没有覆盖非 base 币种券商账户。

建议: 不要用 `BalanceOut(currency=account.currency)` 表达混合值。券商账户至少应拆成 `ledger_balance(account currency)`、`holdings_value_base(base currency)`、`total_value_base`；或者先把 holdings value 转回 account currency 后再相加。前端也要避免把 base-currency market value 当账户币种二次换算。

### P1-2 快照账户仍可写入初始余额/调整余额，容易和同步持仓或现金双算

券商同步现在会把现金也作为 cash-class holding 写入持仓，见 IBKR `backend/app/services/broker_sync/ibkr.py:212-259` 和 Trade Republic `backend/app/services/broker_sync/traderepublic.py:246-309`。但前端仍允许 brokerage 填写初始余额，`frontend/src/components/account-form.tsx:45-48` 只隐藏 crypto/exchange 的 initial balance，没有隐藏 brokerage。资产页也对所有有 balance row 的账户显示“调整余额”，`frontend/src/app/assets/page.tsx:1444-1450`，而后端调整接口只看 `v_account_balance` 并写 adjustment 交易，见 `backend/app/api/v1/accounts.py:382-410`。

影响: 对 brokerage/crypto/exchange 这类快照账户，真实资产来自 `asset_holdings × market_prices`。如果用户创建 brokerage 时填了现金初始余额，或在资产页把包含持仓的余额作为目标余额提交，net worth 会在 `backend/app/api/v1/holdings.py:446-525` 中同时加上 ledger cash 和 holdings value，形成双算。这个问题和 P1-1 叠加后，会同时出现单位错误和金额翻倍。

建议: 对 `brokerage/crypto_wallet/exchange` 禁用初始余额和调整余额，或把“调整余额”改成只调整明确的 cash ledger 子项，UI 文案必须区分“现金余额”和“持仓市值”。后端也应拒绝 snapshot account 调整接口，避免绕过前端。

### P1-3 配对转账在多个现金流入口仍会双计 transfer volume

正确的去重只存在于 snapshot service 和 by-category。`backend/app/services/cashflow/engine.py:92-104` 会通过 `metadata_json.paired_with_tx_id` 丢掉较大 id 的一条腿，`backend/app/api/v1/cashflow.py:142-155` 的 `/cashflow/by-category` 也做了同样逻辑。

但 `/cashflow/monthly` 的 SQL 在 `backend/app/api/v1/cashflow.py:47-66` 只排除 `metadata.subaccount=true`，没有排除 paired transfer 的第二条腿；其内部 category SQL `backend/app/api/v1/cashflow.py:74-88` 也没有 paired 去重。手动 recompute endpoint `backend/app/api/v1/cashflow.py:262-283` 仍用另一份旧 SQL，也没有 paired 去重。MCP `get_cashflow()` 在 `mcp-server/src/finance_mcp/server.py:951-958` 同样只排除 subaccount，不排除 paired leg。

影响: 同一个月的 transfer total 会因入口不同而不一致：snapshot/by-category 可能显示 500，monthly/recompute/MCP 可能显示 1000。Dashboard 和 analytics 主要消费 monthly endpoint，因此用户看到的月度转账规模仍可能翻倍。测试 `backend/tests/test_cashflow_by_category_fx.py` 只覆盖 by-category，没有覆盖 monthly/recompute/MCP。

建议: 把 amount expression、subaccount predicate、paired-leg dedup predicate 全部收敛到 `services/cashflow/engine.py`，monthly endpoint 和 recompute endpoint 直接读 snapshot 或复用同一 SQL builder；MCP 也要同步该条件。

### P1-4 ingestion 中途 commit 破坏 PDF 入库/重解析原子性

`backend/app/services/ingestion/__init__.py:180-203` 为了让 LLM background worker 能看到新交易，在 `ingest_transactions()` 中途执行 `await db.commit()`，随后才继续 synthetic upgrade、transfer matcher、cashflow recompute，见 `backend/app/services/ingestion/__init__.py:205-263`。

这会破坏调用方的事务边界。PDF commit route 在 `backend/app/api/v1/statements.py:456-487` 中设置 import 状态、解析、插入交易，再调用 ingestion；如果 ingestion 在中途 commit 后，后续 matcher/recompute/preview 查询失败，`_mark_failed()` 已无法回滚前半段插入。重解析路径 `backend/app/api/v1/statements.py:733-783` 更危险：旧交易删除后，新交易可能已经被中途提交，后续 stale period recompute 或状态更新失败时会留下部分新数据。

同时，LLM worker 在 `backend/app/services/llm/queue.py:79-98` 会立即消费队列，分类器在 `backend/app/services/llm/classifier.py:284-296` 可以把 tx 改成非 pending；但此时 transfer matcher 还没运行，后续又可能把同一 tx 改成 transfer，形成异步写入竞争。

建议: `ingest_transactions()` 不应 commit。改成 outbox / after-commit dispatch：先在同一事务内完成导入、分类规则、转账匹配、现金流重算，commit 后再把 tx ids 放入 LLM 队列。可以让 ingestion 返回 `llm_target_ids`，由 route 或 session hook 在 commit 后派发。

### P1-5 Account PATCH 可以绕过 crypto/exchange 的 USDT invariant

创建 schema 已在 `backend/app/schemas/__init__.py:88-104` 强制 `crypto_wallet/exchange` 账户必须使用 USDT。但 `AccountUpdate` 在 `backend/app/schemas/__init__.py:107-117` 没有同样的 validator，`backend/app/api/v1/accounts.py:316-323` 直接把 PATCH 字段 set 到 account。

影响: API 客户端可以 PATCH `{ "type": "crypto_wallet", "currency": "EUR" }`，或把已有 crypto/exchange 账户 currency 改成 EUR/CNY。这样会重新打开 V6 的单位错配问题：crypto/exchange holdings 仍按 USDT 计价，但 `/accounts/balances` 会以账户 currency 返回。

建议: 在 update route 校验最终 `(type, currency)` 组合，而不是只校验 patch 字段；最好禁止账户在 cash/bank/brokerage/crypto/exchange 大类之间随意变更，尤其是已经有关联 holdings / connections / transactions 的账户。

### P1-6 MCP PDF import 仍不是完整 ingestion mirror

MCP `parse_bank_statement` 现在补了一部分 metadata、规则分类、同账户金额配对和 snapshot recompute，见 `mcp-server/src/finance_mcp/server.py:706-889`，但核心仍是 SQLite 直写交易。插入列 `mcp-server/src/finance_mcp/server.py:784-805` 没有写 `fx_rate_to_base/base_amount`，外币 PDF 不会像 REST ingestion 那样通过 FX pipeline 折算或标记；它也没有跑完整的跨账户 transfer matcher，只做了同账户金额匹配 `mcp-server/src/finance_mcp/server.py:828-879`，更没有 LLM fallback 和 audit 字段。

影响: 同一张 PDF 通过 REST 和 MCP 导入，外币现金流、跨账户转账配对、pending 状态、分类审计字段都可能不同。当前 cashflow 已经对缺 FX 的外币返回 NULL，MCP PDF 外币交易会被现金流静默排除，而不是折算或给用户明确的 fx_missing 处理路径。

建议: MCP PDF import 不再手写 ingestion 逻辑，改为调用后端 API 或抽出可被 MCP 复用的同步 ingestion service。如果必须直写，至少补齐 FX、完整 transfer matcher、LLM/outbox、audit 字段和 regression tests。

### P1-7 GoCardless 凭据和 country bug 仍未修复

`backend/app/api/v1/bank_sync.py:70-85` 的 `GET /institutions` 仍把 `encrypted_credentials` 放在 query string。这个 blob 虽然是密文，但对本服务来说等同可重放凭据，会进入浏览器历史、reverse proxy/access log、APM 和 crash report。`backend/app/schemas/bank_sync.py:41-50` 的 `BankConnectionCreate` 没有 country 字段，导致 `backend/app/api/v1/bank_sync.py:106-111` 创建连接时仍把 `redirect_url` 当作 country 传给 institution lookup。

影响: 安全上扩大 GoCardless credential 暴露面；功能上 institution lookup 用 URL 当 country，会造成连接流程失败或 institution metadata 错误。该问题从 V3/V6 仍然存在。

建议: `/institutions` 改为 POST body 或服务端保存 setup id；`BankConnectionCreate` 增加显式 `country` 字段；不要在日志、query 或 URL 中传递 credential blob。

### P1-8 Notion asset summary 仍使用旧余额公式

`backend/app/services/notion_sync/engine.py:304-310` 拉 holdings 时只检查 `Account.is_active == True`，没有同步 `include_in_total` 口径。账户余额部分在 `backend/app/services/notion_sync/engine.py:355-359` 使用 `initial_balance + SUM(Transaction.amount)`。

影响: 系统内部 expense 金额以正数存储，这个公式会把支出加到账户余额里；transfer/subaccount/pending/deleted/FX/base_amount/include_in_total 等口径也都和 `v_account_balance` / net worth 不一致。启用 Notion asset sync 后，Notion 会同步错误余额和错误资产摘要。

建议: Notion summary 复用 `v_account_balance`、portfolio summary 和 net worth service，不要在 Notion service 内重写资金公式。

### P1-9 broker snapshot 会清零同账户所有非本轮出现的 `chain=''` 持仓，可能误删手工持仓

`backend/app/services/broker_sync/upsert.py:156-174` 在每次 broker sync 末尾把同一 account 下所有 `chain == ""` 且不在本轮 `present_asset_ids` 的 active holding 都设成 `quantity=0, is_active=False`。当前 `AssetHolding` 没有 `source/provider/managed_by_sync` 字段，因此无法区分“这个 holding 是 IBKR/TR 管理的”还是“用户手工添加在同一 brokerage account 下的”。

影响: 用户在 brokerage 账户中手工添加黄金、私募、不可同步资产，下一次 IBKR/TR sync 可能把这些持仓清零。多个 provider 共享同一 brokerage account 时也有互相清理的风险。这属于可见数据损坏，且不容易从 UI 发现原因。

建议: 给 holdings 增加 ownership/source 元数据，例如 `source_provider`, `source_connection_id`, `managed_by_sync`；reset 只作用于同一 provider/connection 上一次管理过的 holdings。短期至少在 UI/后端禁止把手工 holding 放进已连接 broker sync 的账户，或 sync 前提示。

## P2 中优先级

### P2-1 broker asset identity 忽略 provider/conid 冲突，可能合并不同证券

`backend/app/services/broker_sync/upsert.py:35-60` 查找资产时只用 `(asset_class, symbol, chain='', contract='')`，即使 `BrokerPosition.conid` / ISIN / provider 不同，也会命中已有资产；只有当已有 row 没有 `data_source_id` 时才回填。证券世界里同 symbol 跨市场/跨 provider 冲突并不少见，ETF/基金/ADR 更容易出现。

影响: 两个不同 conid/ISIN 但相同 symbol/asset_class 的资产会被合并，价格会互相覆盖，持仓市值错误。V5 已经为 crypto 修过 `(chain, contract)` identity，broker 现在有类似风险。

建议: broker-managed assets 的 identity 至少纳入 provider-specific id；如果缺 id，遇到同 symbol 但 `data_source_id` 冲突时应拒绝合并并记录需要人工确认。

### P2-2 Gemini abstain 日志会写入 raw model output，可能泄漏交易隐私

`backend/app/services/llm/gemini.py:200-212` 在无法解析分类时记录 `raw_preview=text[:400]`。LLM 输出可能复述 prompt 中的交易描述、counterparty、备注或用户知识库片段。

影响: 服务器日志可能长期保存个人财务流水信息。虽然不是 token 泄漏，但属于隐私数据外泄面。

建议: 不记录 raw output；只记录 parse status、reason code、token count、model、grounding 开关。调试需要 raw 输出时应使用显式 debug flag，并默认脱敏/禁用。

### P2-3 Trade Republic 登录失败时临时 cookie 文件可能遗留

`backend/app/services/broker_sync/traderepublic.py:129-138` 创建 `NamedTemporaryFile(delete=False)` 后调用 `_new_api()` / `initiate_weblogin()`。如果这里抛异常，函数直接 raise `BrokerSyncError`，没有 unlink 临时文件。正常 pending/verify 路径会在 `backend/app/api/v1/wallet_sync.py:422-428` 和 `backend/app/api/v1/wallet_sync.py:486-492` cleanup，但失败发生在 pending 存入之前时不会走这些 cleanup。

影响: 临时文件可能为空，也可能包含 pytr 写入的部分 WAF/session cookie，属于本机隐私残留。

建议: 在 `initiate_login()` 内部用 `try/except` 包住 `_new_api` 和 `initiate_weblogin`，失败时 `Path(fd.name).unlink(missing_ok=True)`。

### P2-4 资金公式分散在多处，已经造成现金流口径漂移

现金流 base amount CASE 和 transfer predicate 现在分散在 `backend/app/services/cashflow/engine.py`、`backend/app/api/v1/cashflow.py`、`mcp-server/src/finance_mcp/server.py` 和前端 category breakdown。`rg` 显示 `CASE WHEN currency = :base_currency` 在 `backend/app/api/v1/cashflow.py` 多处重复，`_AMOUNT_BASE_EXPR` 只被 by-category 部分复用。

影响: P1-3 就是重复 SQL 的直接后果。后续任何 FX、subaccount、paired transfer、pending 口径调整都需要同时改多个入口，漏一处就会再次出现资金报表不一致。

建议: 让 monthly、recompute、snapshot、by-category 全部调用同一 service；MCP 侧至少通过共享 SQL 片段或后端 API 获取结果，避免继续复制业务公式。

### P2-5 `main.py` lifespan 聚合太多 DDL、数据修复和后台启动逻辑

`backend/app/main.py:146-225` 在启动时做 `create_all`、重建 view、rebuild table、轻量 column migration、index migration；`backend/app/main.py:236-270` 做 Alembic drift check；`backend/app/main.py:272-420` 又做规则去重、seed、Gemini key 迁移、credential health、LLM worker、历史分类/转账修复；最后 `backend/app/main.py:422-424` 启动 scheduler。

影响: 应用启动副作用过多，测试和本地环境容易被真实 DB 状态影响；真正的 schema/data migration 与运行时 boot 混在一起，也让失败恢复困难。V6 里测试曾被默认 SQLite 写 DDL 打断，本轮虽然通过，但结构风险仍在。

建议: 把 schema migration/data repair 移到 Alembic 或显式 maintenance command；lifespan 只保留必要的 runtime boot（worker/scheduler）和只读 health check。历史数据修复要有独立版本号、幂等日志和可回滚策略。

### P2-6 大文件开始承担过多职责，结构需要拆分

当前几个文件已经明显偏大: `frontend/src/app/assets/page.tsx` 1505 行，`backend/app/api/v1/transactions.py` 1543 行，`mcp-server/src/finance_mcp/server.py` 1248 行，`backend/app/main.py` 494 行。尤其 assets page 同时承担账户卡片、余额调整、持仓管理、币种换算、连接入口等职责；transactions API 混合 CRUD、转账配对、手工绑定、刷新匹配相关逻辑；MCP server 复制了大量后端业务逻辑。

影响: 后续增量功能会继续在同一文件中堆叠，review 和测试定位成本上升，也更容易出现“某个入口修了、另一个入口没修”的情况。

建议: 前端把 assets page 拆成 account list / holdings table / dialogs / currency hooks；后端 transactions API 把 transfer binding、refresh matching、CRUD 拆到 service + smaller routers；MCP 只做 thin adapter，不持有核心资金逻辑。

### P2-7 文档与实现漂移

发现的明显漂移:

- `README.md:9` 仍写 P1-4 加密钱包全栈已合入和 `306 个单测全过`，没有把券商同步/PDF 预览后入库作为最新已交付主状态。
- `README.md:46` 写 MCP server 7 tools “6 轮回归测试 9 bug 全修”，但本轮仍发现 MCP PDF import 和 MCP cashflow 口径问题。
- `docs/ARCHITECTURE.md:4` 最后修订仍是 2026-05-18，但正文已经包含 2026-06 券商内容。
- `PROGRESS.md:19` 写 351 passed，和当前 pytest 输出一致；但 `PROGRESS.md:74` 仍保留 306 passed 的旧段落，容易误读。

影响: 后续开发者或 AI agent 会按旧状态判断系统边界，尤其是 MCP 已全修、测试数量、券商状态这些会影响 review 优先级。

建议: 修完 P1 后统一更新 README / ARCHITECTURE / API / ROADMAP，明确哪些功能是 production-ready、哪些是 scaffold / UAT pending。

### P2-8 测试覆盖缺口集中在本轮发现的资金入口

现有后端测试全绿，但缺少以下关键回归:

- 非 base currency brokerage 账户调用 `/accounts/balances` 的单位测试。
- brokerage/crypto/exchange 调整余额或 initial balance 与 holdings 叠加的测试。
- paired transfer 在 `/cashflow/monthly`、`/cashflow/recompute`、MCP `get_cashflow` 中不双计的测试。
- Account PATCH 后仍满足 crypto/exchange currency invariant 的测试。
- MCP PDF import 外币折算和跨账户 transfer matching 测试。
- GoCardless create_connection country 字段测试。

建议: 修 P1 时优先补这些 regression tests。否则当前 351 passed 很容易继续掩盖资金入口不一致。

## 安全结论

没有发现真实 token 或密钥进入 git。当前最需要处理的安全问题是 GoCardless credential blob 仍走 query string，其次是 Gemini raw output 日志和 Trade Republic 临时 cookie 文件清理。`AUTH_DISABLED` 与非 loopback bind 的保护仍存在，`.env` / `.env.local` 也被 ignore，基础安全边界没有明显倒退。

## 建议修复顺序

1. 先修资金口径: P1-1、P1-2、P1-3、P1-5、P1-6。
2. 再修导入事务和数据保护: P1-4、P1-9。
3. 同步修安全入口: P1-7、P2-2、P2-3。
4. 最后做结构治理: P2-4、P2-5、P2-6，并补 P2-8 的回归测试。
5. 完成后更新文档，避免 V8 继续重复确认 README/ARCHITECTURE/PROGRESS 漂移。
