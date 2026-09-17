# Finance Tracker 全局代码审查 V9

审查日期：2026-09-14（Europe/Berlin）  
代码基线：`8fadc38`；开始审查时工作区干净。  
本轮只新增审查报告、复现用例和结果记录，没有修改业务实现或生产数据。

## 结论与证据边界

**存在多项可以实际改变余额、收支、估值或历史期间的 bug，需要先修资金正确性，再做结构清理。现有测试全绿不足以证明资金链路正确。** 最集中的问题是：拆分和删除的组合、外币单位约束、自动配对、部分同步覆盖，以及多个写入入口之间的规则漂移。

项目规模为 141 个 Python/TypeScript/TSX 生产文件，约 37,243 行。审查覆盖后端模型、接口和核心资金服务，前端页面、表单及数据请求，MCP 读写路径、迁移和部署配置。核心资金链路进行了源码追踪与反例验证；其余组件主要做静态检查和调用关系检查。这不是对全部真实银行格式、外部接口或实际部署状态的认证。

所有下文金额均为构造案例，**不代表已经确认用户真实账本发生了对应损失或偏差**。未对生产账本做数据修复，也未调用真实银行、交易所或 LLM 执行复现。MCP 故障用例使用模拟解析结果注入数据库写入失败；它证明失败事务不回滚，不表示真实解析器必然返回同样的非法类型。

| 验证项目 | 结果及含义 |
|---|---|
| 现有后端 + MCP 测试 | **420 passed**；单独排除了依赖运行中 `:8199` 服务的 `test_api.py`，避免向未知服务写数据 |
| 新增资金/逻辑反例 | **22 failed**；断言表达正确行为，22 项均在相应业务断言处失败，无收集错误或夹具错误，说明反例成立 |
| 前端常规 TypeScript 检查 | 通过 |
| 前端生产构建 | 通过，当前实际安装 Next.js 15.5.15 |
| 更严格的未使用变量检查 | 11 处诊断，具体见后文 |
| 前端 lint | 尚未建立可直接执行的检查；`npm run lint` 进入 ESLint 初始配置提问，不能视为 lint 通过 |
| 全新数据库迁移 | 失败：空 baseline 后续直接读取不存在的 `categorization_rules` |
| 前端盈亏计算反例 | 执行真实页面的汇总函数：100 EUR + 100 USD，按 0.9 折算应为 190 EUR，实际汇总为 200，再作为本位币显示 |
| Decimal 存储边界 | SQLite + SQLAlchemy `Numeric(20,8)` 往返存在精度损失，见重构建议 |

复现文件：[repro_v9.py](</Users/jason/Project/finance-tracker/code review/repro_v9.py>)。原始断言记录：[repro_v9_results.txt](</Users/jason/Project/finance-tracker/code review/repro_v9_results.txt>)。复现用例放在审查目录，未接入常规测试收集；应单独运行，修复后再迁入正式回归套件。运行方式见文末。

优先级说明：P1 = 应优先处理的资金错误、数据一致性或条件性访问风险；P2 = 局部报表、可靠性及维护问题。没有把条件性问题列为无条件 P0，也没有把普通格式问题算作金融 bug。

## 已确认的资金与逻辑问题

### F01 · P1 · 外币拆分使用了已经修改过的折算总额

位置：[transactions.py:636](/Users/jason/Project/finance-tracker/backend/app/api/v1/transactions.py:636)，同文件 660、687 行。

`_line_base()` 闭包每次读取 `tx.base_amount`；第一条明细把它改小后，后续明细继续拿这个较小金额当原总额。100 USD、原本折合 90 EUR，拆成 20/80 USD 后得到 **18 + 14.4 = 32.4 EUR**，正确结果应为 18 + 72 = 90 EUR。原币余额看起来正常，因此这个错误容易只在现金流汇总中暴露。

修复：在修改原交易前保存不可变的原始 `base_amount`，全部明细据此分摊；统一小数位和舍入规则，最后一条分配余数，确保原币及本位币总额都守恒。复现：`test_split_preserves_foreign_base_amount`。

### F02 · P1 · 拆分子明细脱离账单，撤销或重解析后残留

位置：[transactions.py:676](/Users/jason/Project/finance-tracker/backend/app/api/v1/transactions.py:676)、[statements.py:927](/Users/jason/Project/finance-tracker/backend/app/api/v1/statements.py:927)。

新建拆分明细未继承 `pdf_import_id`，删除和重解析只按该字段寻找原账单交易。100 EUR 拆成 20/80 后撤销账单，20 被删除，**80 仍留在账本，余额为 -80**。重新导入原始 100 还会与残留 80 重复计入。复现已验证撤销路径；重解析使用同样的关联筛选。

修复：保留导入来源与拆分组关联，删除/重解析按完整来源图处理，并同步处理配对关系。不能只改新建行：已有缺少 `pdf_import_id` 的拆分兄弟行也需要识别和回填。复现：`test_statement_delete_removes_split_children`。

### F03 · P1 · 拆分只校验绝对金额，未校验资金方向

位置：[transactions.py:617](/Users/jason/Project/finance-tracker/backend/app/api/v1/transactions.py:617)、[schemas/__init__.py:273](/Users/jason/Project/finance-tracker/backend/app/schemas/__init__.py:273)。

接口允许一笔 -100 的支出拆成 20 支出 + 80 收入，因为绝对值之和为 100。结果余额变为 **+60，相比原交易增加 160**。拆分行还允许 `adjustment`，同样可以绕过预期方向。前端也提供收入/支出类型选择。

修复：校验拆分前后的**签名金额总和**；普通拆分限定同方向的收入/支出及转账腿，补偿或退款另建明确的业务操作。复现：`test_split_preserves_signed_balance`。

### F04 · P1 · 交易币种与账户币种不一致时，余额直接混加

位置：[transactions.py:244](/Users/jason/Project/finance-tracker/backend/app/api/v1/transactions.py:244)、[main.py:104](/Users/jason/Project/finance-tracker/backend/app/main.py:104)、[statements.py:168](/Users/jason/Project/finance-tracker/backend/app/api/v1/statements.py:168)。

账户余额视图直接累加 `amount`，但交易创建/更新、PDF/CSV 导入没有一致地保证 `Transaction.currency == Account.currency`。1000 EUR 账户写入 100 USD 支出，USD→EUR=0.9，现金流可以折为 90 EUR，**账户却直接扣 100 EUR，余额 900 而不是 910**。前端手工表单通常跟随账户币种，但 API 和导入入口仍可到达。

修复：当前单币种账户模型下，在全部写入入口拒绝不一致币种；若要支持真实多币种账户，应存明确的账户记账金额或按币种分账。`base_amount` 是全局本位币金额，不能不加区分地拿来当账户币种金额。也应限制已有流水账户直接改币种。复现：`test_foreign_transaction_does_not_pollute_account_unit`。

### F05 · P1 · 同链多个钱包地址相互覆盖

位置：[orchestrator.py:185](/Users/jason/Project/finance-tracker/backend/app/services/wallet_sync/orchestrator.py:185)、[upsert.py:206](/Users/jason/Project/finance-tracker/backend/app/services/wallet_sync/upsert.py:206)。

地址模型和 API 允许同一账户同一条链配置多个不同地址，但同步逐地址调用以 `(account, chain)` 为范围的替换快照。两个 Ethereum 地址分别有 1 ETH 和 2 ETH，最终只有 **2 ETH，而不是 3 ETH**；后一个地址没有的 token 还会把前一个地址的 token 清零。两个地址的状态均可显示成功。

修复：按地址保存来源快照后聚合，或至少先完整获取同链全部地址，按资产身份求和，再执行一次链级替换。部分地址失败时不能用剩余地址覆盖全链。复现：`test_same_chain_addresses_are_aggregated`。

### F06 · P1 · 部分上游失败被当成完整快照，清零真实持仓

位置：[bitget.py:111](/Users/jason/Project/finance-tracker/backend/app/services/exchange_sync/bitget.py:111)、[orchestrator.py:246](/Users/jason/Project/finance-tracker/backend/app/services/wallet_sync/orchestrator.py:246)。

Bitget 现货/合约/资金钱包接口失败时，部分路径捕获错误后仍返回剩余列表；调用方无法区分部分结果与完整快照，随后清零未出现的持仓。复现中现货接口返回 503，合约返回 10 USDT，**原有 1 BTC 被改成 0，状态却为 `ok`、错误数为 0**。统一账户的 funding 分支也存在相同语义。

Trade Republic 的 [traderepublic.py:286](/Users/jason/Project/finance-tracker/backend/app/services/broker_sync/traderepublic.py:286) 在现金查询失败时返回空列表，而 broker upsert 会清零未出现的现金持仓，属于同类静态确认的风险。

修复：provider 返回带来源范围及完整性标记的结果；只在完整成功的范围内做替换。小规模系统也可以先采用“任何必要子接口失败，则保留上次完整快照”的简单方案。复现：`test_partial_exchange_response_preserves_previous_holdings`。

### F07 · P1 · 自动转账判定证据不足，真实收支会消失

位置：[transfer_matcher/engine.py:325](/Users/jason/Project/finance-tracker/backend/app/services/transfer_matcher/engine.py:325)、[pdf_parser/engine.py:358](/Users/jason/Project/finance-tracker/backend/app/services/pdf_parser/engine.py:358)。

有两个独立反例：

- 不同账户中同日同额的 `Groceries` 支出与 `Savings interest` 收入，没有账户关系证据，也因金额 50 分 + 同日 30 分超过 75 阈值而自动配对，双双从收支中剔除。
- 普通 `SEPA Direct Debit Insurance` 被通用关键词 `sepa direct debit` 直接识别为转账，保险支出无需找到对手就退出支出统计。

另一个设计问题是候选生成过程中先把首个达阈值的对象标记为已使用，最后再排序，后面的更强候选没有机会参与比较。

修复：金额和日期相同只能生成建议；自动改写需账户 IBAN、真实对手关系等积极证据。通用付款方式不等于本人账户划转。先收集候选再处理冲突和歧义。复现：`test_amount_and_date_alone_do_not_auto_erase_income_expense`、`test_sepa_direct_debit_is_not_automatically_an_own_account_transfer`。

### F08 · P1 · 镜像替换缺少方向校验，可以把支出改成入账

位置：[transfer_matcher/engine.py:414](/Users/jason/Project/finance-tracker/backend/app/services/transfer_matcher/engine.py:414)。

`replace_synthetic_with_real()` 只比较账户、币种、绝对金额和日期，未验证真实交易方向与镜像一致。目的账户本有 +100 镜像，随后导入一笔无关 -100 消费，消费被用来替换镜像并强改成 transfer-in：**应为 0 的账户余额变成 +100**。`find_existing_counter_leg()` 的实际筛选也没有兑现注释中的相反方向要求。

修复：替换要求同方向，寻找对手要求相反方向；同时尊重人工确认及业务描述，存在多个候选时保持待确认。替换后统一校正类别、pending 状态和所有受影响月份。复现：`test_synthetic_replacement_requires_matching_direction`。

### F09 · P1 · 修改已配对交易保留旧指针，月度统计漏掉收入

位置：[transactions.py:399](/Users/jason/Project/finance-tracker/backend/app/api/v1/transactions.py:399)、[cashflow/engine.py:75](/Users/jason/Project/finance-tracker/backend/app/services/cashflow/engine.py:75)。

普通 PATCH 改类型、金额、账户等字段时没有维护配对关系。将较大 ID 的已配对转账改为收入，旧 `paired_with_tx_id` 仍存在；去重谓词没有限定双方仍为合法转账，收入被排除。复现中 **monthly income=0，timeseries income=100**。

修复：修改配对关键字段时明确拒绝、解除或重建双方关系；去重只作用于合法、对称的转账对。不可依赖用户另点一次全局重新匹配补救。拆分、删除和撤销也应复用同一关系维护服务。复现：`test_editing_transfer_does_not_hide_income_from_monthly`。

### F10 · P1 · 收件箱确认遗漏了收入转转账的方向补全

位置：[transactions.py:1773](/Users/jason/Project/finance-tracker/backend/app/api/v1/transactions.py:1773)。

普通 PATCH 已补上“原收入改为 transfer 时设置方向 in”，但 `/inbox/{id}/confirm` 的重复实现没有这段逻辑。将 +100 的待确认收入改成 transfer 且不显式传 metadata 后，默认方向 out，**余额从 +100 变为 -100**。这是明确的入口不一致，API 可以直接触发。

修复：确认入口调用统一交易更新服务；如果允许改类型，应从旧签名推导资金方向。复现：`test_inbox_income_to_transfer_preserves_direction`。

### F11 · P1 · 资产构成把大额负债当作微量资产过滤

位置：[composition.py:160](/Users/jason/Project/finance-tracker/backend/app/services/valuation/composition.py:160)。

`if b.value < 0.1` 对所有负数成立，包括大额信用卡债务或负现金。10,000 EUR 投资、-1,000 EUR 净现金时，净值服务为 9,000，**构成报表为 10,000，并把 -1,000 算作 1 项 dust**。

修复：微量判断至少使用绝对值；更重要的是明确负债的展示和合计口径，不能为了正值饼图静默丢掉负债。允许的微量剔除金额也应单独返回，以便与净值对账。复现：`test_composition_keeps_large_negative_liabilities`。

### F12 · P1 · PayPal 重复 ID 的序号不是跨导出稳定身份

位置：[paypal.py:189](/Users/jason/Project/finance-tracker/backend/app/services/csv_parser/paypal.py:189)。

同一导出中的第二个相同 Transaction ID 被加 `#2`，但每次解析重新从 1 计数。已知提现与其冲正可能复用 ID：分别导出两个月时，两条都会成为裸 ID，后一次冲正被当成重复跳过；改变窗口和顺序还可能重复计入或跳过另一条。复现中第二个月的真实 +100 冲正 **imported=0**。

修复：用来源 ID 加稳定事件字段，例如时间、事件类型、币种、签名金额建立身份；保留原始 provider ID。迁移时要兼容已有裸 ID/`#N` 数据，不能直接切新键后重新导入全部数据。复现：`test_paypal_reused_id_is_stable_across_export_windows`。

### F13 · P1 · 只修改汇率时，旧 base_amount 继续覆盖新汇率

位置：[transactions.py:390](/Users/jason/Project/finance-tracker/backend/app/api/v1/transactions.py:390)。

失效重算只由 amount/currency 字段触发。100 USD 原汇率 0.9、base_amount=90，把汇率改为 0.8 后 `base_amount` 仍是 90，汇总公式又优先读取它，**实际继续统计 90 而不是 80**。

修复：定义金额、汇率和折算结果的依赖关系；编辑汇率必须使派生金额失效或重算。对同时提供但互相矛盾的 rate/base_amount 明确验证，不能默默挑一个。复现：`test_fx_only_patch_updates_base_amount`。

### F14 · P1 · 修改记账本位币会把旧折算金额换标签

位置：[system.py:46](/Users/jason/Project/finance-tracker/backend/app/api/v1/system.py:46)、[models/__init__.py:215](/Users/jason/Project/finance-tracker/backend/app/models/__init__.py:215)。

交易保存 `base_amount` 和 `fx_rate_to_base`，却没有保存它们对应的本位币。设置接口允许直接改 `base_currency`。复现：GBP 交易原折为 90 EUR，EUR→CNY=10，切换本位币后 **被显示为 90 CNY，而不是 900 CNY**。这是后端记账设置，区别于前端安全的显示币种切换；重启时改环境变量也有同类问题。

修复：已有账本优先禁止直接切换记账本位币；若产品确需支持，必须保存折算币种/版本并执行完整迁移、校验和快照重建。复现：`test_base_currency_change_does_not_relabel_historical_amounts`。

### F15 · P2 · 未分类收入被归并成支出

位置：[cashflow.py:152](/Users/jason/Project/finance-tracker/backend/app/api/v1/cashflow.py:152)。

`by-category` 以 `category_id` 分组，并把缺失 category 的 kind 默认设为 expense。未分类收入 100 与支出 20 全部进入同一个 NULL 分组，返回 **expense=120**；月度收支仍是正确的 100/20，因此分析页出现互相矛盾的图表。adjustment 也可能进入这组。

修复：未分类也按交易类型分组，以 `t.type` 作为回退；明确排除或单列 adjustment。复现：`test_uncategorized_income_and_expense_are_not_merged`。

### F16 · P1 · 人工确认并未在所有自动化入口受到保护

位置：[classifier.py:284](/Users/jason/Project/finance-tracker/backend/app/services/llm/classifier.py:284)、[refresh_matching/__init__.py:250](/Users/jason/Project/finance-tracker/backend/app/services/refresh_matching/__init__.py:250)。

已复现两个入口：

- LLM 请求发出后，用户先把交易确认成 Travel；结果返回后分类器没有重新检查版本/确认状态，直接覆盖成 Food，`categorization_method` 从 user 变成 llm。
- 全局重新匹配只通过 `source='manual'` 或 `user_note` 识别人工处理，没有排除新加的 `categorization_method='user'`。导入交易被用户明确确认为支出，仍可在重匹配中变回 transfer 并退出支出统计。

修复：定义统一的“用户已锁定分类”规则，让导入、重匹配、规则分类、LLM 写回共用；LLM 返回后采用带版本/状态条件的更新，避免覆盖请求发出后的新操作。只检查一次或只在前端禁用按钮都不够。复现：`test_llm_response_does_not_overwrite_later_manual_confirmation`、`test_refresh_matching_respects_manual_category_confirmation`。

### F17 · P1 · MCP 账单导入失败后仍提交部分交易

位置：[server.py:598](/Users/jason/Project/finance-tracker/mcp-server/src/finance_mcp/server.py:598)。

MCP 的内部异常分支把 import 标为 failed，然后在**未回滚已插入交易**的连接上 `commit()`。模拟第二行触发数据库约束失败，调用返回失败，但第一笔 100 支出已永久提交。重传还会被该文件 hash 的导入记录拦住，用户难以判断真实入库范围。

修复：整次交易写入必须原子提交；失败先回滚，再用独立短事务记录 failed。更直接的结构修复是 MCP 调用后端同一导入服务。旧 V8 已记录的 MCP 缺少完整 FX、跨账户匹配等镜像问题依然存在，本项额外验证了异常提交风险。复现：`test_mcp_import_failure_rolls_back_inserted_rows`。

### F18 · P1 · AMEX 跨年账单把上一年 12 月写成下一年 12 月

位置：[pdf_parser/engine.py:594](/Users/jason/Project/finance-tracker/backend/app/services/pdf_parser/engine.py:594)、同文件 628 行。

解析器提取账单年份后，将其用于每条仅有日/月的记录。账期 `09.12.25–08.01.26` 中的 `28.12` 被写成 **2026-12-28，而不是 2025-12-28**，整笔交易偏移一年，影响月度收支、现金历史和对账。

修复：根据完整账期及每条月日推导年份，校验记录位于账期合理窗口内；历史账单不应在年份缺失时直接使用机器当前年。复现：`test_amex_january_statement_places_december_in_previous_year`。

### F19 · P1 · 前端总浮动盈亏直接混加不同币种

位置：[assets/page.tsx:172](/Users/jason/Project/finance-tracker/frontend/src/app/assets/page.tsx:172)、同文件 380 行。

后端单项 `unrealized_pnl` 是持仓报价/成本币种金额；前端直接相加，再用 `display()` 当作本位币折算。执行真实页面计算片段，100 EUR 盈利 + 100 USD 盈利、USD→EUR=0.9，结果为 **200 EUR，正确为 190 EUR**。如果本位币为 CNY，量级可能差得更多。

修复：后端返回统一币种的组合盈亏；或前端逐行按明确的盈亏币种转换再求和。汇总范围也要与 `include_in_total`、账户软删除和 holding.is_active 一致：当前 `/holdings` 列表没有这些过滤，页面又对全列表求总和。该问题是展示计算错误，不代表原始持仓数量被修改。

### F20 · P2 · 快照账户禁交易的约束只覆盖部分入口

位置：[transactions.py:410](/Users/jason/Project/finance-tracker/backend/app/api/v1/transactions.py:410)、[statements.py:551](/Users/jason/Project/finance-tracker/backend/app/api/v1/statements.py:551)、[server.py:191](/Users/jason/Project/finance-tracker/mcp-server/src/finance_mcp/server.py:191)。

创建接口会拒绝 brokerage/crypto/exchange 的流水，但普通 PATCH 可把银行交易移到这些账户，返回 200；PDF/CSV 导入和 MCP 写入也缺少等价约束。净值和余额忽略这些账户的 ledger，现金流却统计这些交易，形成不一致。

修复：账户存在、未删除、可记流水及币种一致的校验集中在统一写入服务，所有入口调用；对已有脏流水做只读盘点后再决定迁移。复现覆盖 PATCH：`test_patch_cannot_move_transaction_into_snapshot_account`。净值已有“忽略快照账户现金腿”的兜底，本项不是旧版双计问题原封不动地重报。

### F21 · P2 · “最近自动改为转账”接口被动态路由遮蔽

位置：[transactions.py:335](/Users/jason/Project/finance-tracker/backend/app/api/v1/transactions.py:335)、同文件 1224 行。

`GET /{transaction_id}` 先注册，固定路径 `/recently-promoted-to-transfer` 后注册。请求先进入整数参数校验并返回 **422**，到不了实际处理函数。它本应帮助发现和撤销自动改类，失效后削弱审计能力。

修复：固定路径移到动态路径之前，或使用结构上不冲突的路径；补 HTTP 层路由测试。复现：`test_literal_promoted_route_is_reachable`。

### F22 · P2 · 行情刷新覆盖范围与“刷新成功”的含义不一致

位置：[market_data/engine.py:29](/Users/jason/Project/finance-tracker/backend/app/services/market_data/engine.py:29)、[scheduler.py:75](/Users/jason/Project/finance-tracker/backend/app/services/market_data/scheduler.py:75)。

定时 crypto job 和“刷新行情”调用的通用刷新显式跳过 `onchain/native`，它们恰好是钱包/CEX 同步创建的主要资产。实际价格刷新只在账户同步末尾执行，所以等待定时任务或点击刷新行情不会更新这类资产的价格。股票刷新还把所有非空 `data_source_id` 交给 yfinance，包含券商 conid/ISIN；这些身份字段不能统一当作 Yahoo ticker。

此外，净值计算遇到缺价/缺 FX 直接跳过，返回 `as_of=当前时间`，缺少缺价数量和报价时间说明，容易把部分或过期估值理解成完整最新净值。这里是调用链静态确认，未声称实测外部行情是否过期。

修复：资产身份与报价 provider/ticker 分开；按来源统一安排行情刷新。净值返回覆盖率、缺价/缺 FX 项和真实报价时间，刷新结果报告处理了哪些范围。

### F23 · P2 · 前端把数据失败或未就绪显示成零/空状态

位置：[dashboard/page.tsx:38](/Users/jason/Project/finance-tracker/frontend/src/app/dashboard/page.tsx:38)、同文件 67 行；[inbox-panel.tsx:88](/Users/jason/Project/finance-tracker/frontend/src/components/inbox-panel.tsx:88)。

Dashboard 没有处理 net-worth 自身的 loading/error，缺少结果时把总资产、现金、投资设为 0；页面重试只刷新 balances/breakdown。Inbox 未读取请求 error，items 缺失时走空收件箱提示。另一个口径问题是 dashboard 的“按币种”直接加全部账户余额，未应用 `include_in_total`，可能与旁边净值不一致。

修复：未知、失败、过期、已知为零必须区分；重试覆盖实际失败请求；总览同一组报表复用相同账户范围。以上为静态确认，未进行浏览器端故障注入。

### F24 · P1 · 实际监听地址与禁鉴权启动检查可能脱节

位置：[main.py:133](/Users/jason/Project/finance-tracker/backend/app/main.py:133)、[deploy/start.sh:24](/Users/jason/Project/finance-tracker/deploy/start.sh:24)。

启动检查依据 Settings 中的 `BACKEND_HOST`，部署脚本却通过命令行固定绑定 `0.0.0.0`。当保留配置 `BACKEND_HOST=127.0.0.1` 并启用 `AUTH_DISABLED=true` 时，检查会误以为仅本机监听，而实际服务对外监听，认证函数又直接放行。

修复：启动脚本强制外部监听时启用鉴权，或统一由同一配置决定 uvicorn 监听与校验。**默认 auth_disabled=False；本轮未检查用户线上配置，所以这是可达的条件性缺陷，不是已确认当前服务公开无鉴权。**

### F25 · P2 · README 的全新安装迁移路径不能成功执行

位置：[baseline migration:29](/Users/jason/Project/finance-tracker/backend/alembic/versions/1ed07e31cab5_baseline_2026_05_07.py:29)、[LLM migration:36](/Users/jason/Project/finance-tracker/backend/alembic/versions/e53bc1301436_add_llm_classification.py:36)。

baseline 是空操作，下一版直接读取已有表。README 要求先 `alembic upgrade head` 再启动应用，因此全新数据库尚未被 lifespan 建表，迁移就报 **NoSuchTableError: categorization_rules**。本轮已对独立空库执行并验证，不涉及现有数据库。

修复：建立可重建完整 schema 的正式 bootstrap/baseline 路径，并测试空库升级及旧库升级；对已有部署保留明确的兼容策略，不应直接重写已发布迁移后让旧库重跑。

## 重构价值与过度设计判断

### R1 · 最高价值：统一写入规则，而不是只统一金额查询

当前已有 `ingest_transactions` 和共享净值服务，这是可继续利用的基础。但 create、PATCH、inbox confirm、split/unsplit、mark/unbind、PDF delete/reparse、MCP 分别直接改 ORM 或 SQL。F01–F04、F09–F10、F13、F16–F17 都说明“有统一入口”的设计意图还没有覆盖实际写路径。

建议增加几个职责明确的服务函数：验证目标账户、更新交易并失效派生金额、维护转账对、拆分/还原、撤销导入。REST/MCP 负责输入输出，事务提交由一个明确的外层负责。不要为了复用复制一份同步版本；MCP 工具本身已经是 async，可以直接复用后端 async 服务。

资金方向、有效账户范围、配对去重、折算表达式也要统一。当前余额 SQL 在 `main.py` 与 cash history 各维护一份，cashflow 的 monthly/timeseries/recompute 又重复长 CASE，MCP 仍有镜像。共享函数需要基于独立财务算例测试，避免“两个接口使用同一份错误公式，所以 parity 测试通过”。

### R2 · 持仓同步必须有清晰的来源粒度与完整性

钱包数据的获取粒度是“地址”，数据库替换粒度却是“账户+链”；交易所获取粒度是多个钱包，替换粒度却是整个账户。F05/F06 是这种结构错配的直接结果。

推荐保留轻量 provider 接口，但让结果包含来源标识、成功/部分失败状态、观测时间及条目。持仓来源至少能区分同链地址和手工持仓。一次完整来源快照事务性替换，估值只读已提交数据。无需为个人应用额外引入消息中间件；结构化结果与短事务已经能解决主要问题。

### R3 · 关键关系不适合继续堆在自由格式 JSON 中

`paired_with_tx_id`、`transfer_direction`、`split_group_id`、`split_original`、`synthetic_counterleg`、`counter_account_hint` 共同形成了一个隐式交易状态机，但散落在字符串 JSON 和多个 helper 中，数据库无法约束配对对称、组关系及合法类型。于是出现大量“全局重新匹配先修指针”的补偿逻辑。

可逐步把配对 ID、拆分组、来源身份、确认状态、资金方向提升为类型明确的字段/对象；非关键解释性 metadata 继续保留 JSON。先封装关系维护函数，再按需要迁移字段，比一次性重建整个账务模型稳妥。当前没有充足理由上完整事件溯源或重写为通用会计平台。

### R4 · 大文件按业务职责拆，不按行数机械拆

| 当前文件 | 行数 | 建议边界 |
|---|---:|---|
| `backend/app/api/v1/transactions.py` | 1,838 | CRUD/确认路由、拆分服务、转账关系服务、查询服务 |
| `frontend/src/app/assets/page.tsx` | 1,578 | 账户管理、持仓表、估值汇总、分布图；共用明确的汇总数据 |
| `frontend/src/lib/api.ts` | 1,422 | 请求封装、账户、交易、资产、分类、导入等客户端模块 |
| `backend/app/services/transfer_matcher/engine.py` | 1,226 | 候选生成、评分与冲突处理、关系写入、镜像生命周期 |
| `backend/app/services/pdf_parser/engine.py` | 1,037 | 各银行 parser、检测器、共同金额/日期及身份处理 |
| `backend/app/schemas/__init__.py` | 967 | 按领域拆 schema，共用金额/币种/日期校验类型 |
| `backend/app/api/v1/statements.py` | 952 | 导入状态与文件处理、解析预览、提交/撤销服务 |
| `backend/app/main.py` | 500 | 应用生命周期、数据库升级、种子数据、后台任务启动 |

文件变小本身不能修 bug。第一步应使一条资金规则只有一个实现；第二步再拆文件和整理依赖。现有 provider 抽象、SWR、一个 paced LLM worker、ContextVar 隔离并发解析上下文都有具体用途，不应仅因“有抽象”就删掉。

### R5 · 迁移与启动修复同时存在，增加了状态组合

`create_all`、lifespan 中重建表/补列/补索引、每次启动时的数据清理，以及 Alembic 同时负责 schema/数据演化。F25 已证明新库路径不完整。升级失败时，部分索引创建异常还只是记录 warning，应用可能继续运行而缺少预期约束。

建议正式迁移负责版本化变更，启动仅验证预期版本并执行必要的幂等初始化；历史修复进入一次性、有计数结果的迁移。至少维护空库→head、上一受支持版本→head、包含拆分/配对/持仓的旧库→head 三类迁移测试。不要直接清除历史兼容代码后要求真实库碰碰运气。

### R6 · Decimal 使用不等于存储和 SQL 汇总已经精确

模型声明 `Numeric(20,8)`，业务大多使用 Decimal，是好的基础；但 SQLite 存储/驱动仍可能经过二进制浮点，raw SQL SUM/乘法也不是全程 Decimal。运行独立 SQLAlchemy 往返反例：

```text
输入：123456789012.12345678
读回：123456789012.12345886
差额：0.00000208
```

这证明不能把“精确保留 20 位、8 位小数”当作现有保证；**不表示普通小额欧元交易普遍会出现分级差额**。当前更严重的误差来自前文单位、符号、来源和生命周期 bug，应先处理它们。

随后明确金额范围、每币种/资产精度和舍入时机；输入应拒绝非有限数、无效日期、非法币种和超出策略精度的值。法币可考虑整数最小单位，加密数量考虑原始整数+scale，或者明确采用能提供所需精度的存储计算方案。无需为了本次审查立即换数据库，但需用往返、累计、比例分摊算例界定当前支持边界。

### R7 · 不值得继续叠加的设计

- **同步 MCP 写入镜像**：持续复制入口约束、FX 和分类逻辑，维护成本已经有多个真实反例支撑；应收敛。
- **每次启动/全局刷新都自动修历史关系**：补偿步骤越多，越难解释为何某条交易再次变化；应让合法操作本身维护不变量，修复历史数据单独执行并留记录。
- **分散的缓存刷新名单**：`invalidateTransactionGraph`、assets 的 `refreshAll`、dashboard 的 `refreshAll` 各自维护不同 key。应按交易、持仓、配置变更统一依赖失效，增加新报表时只补一个地方。不要把每个页面都发展成一个数据调度器。
- **大量 FIX/Sprint/历史版本解释夹在正常业务代码里**：已修问题的来龙去脉可保留在审查记录/决策文档；实现附近留下当前不变量和必要原因，避免注释描述已经过时的口径。

整体仍适合单体应用。当前主要是规则重复、隐式状态和局部补丁累积，未见必须用复杂分布式架构才能解决的问题。优先做局部收敛，暂不做整套重写。

## 死代码、兼容残留与质量检查

### 可以先清理的未使用符号

下面 11 项来自实际运行 TypeScript 的 `--noUnusedLocals --noUnusedParameters`，属于明确的局部未使用声明：

| 位置 | 符号 |
|---|---|
| [assets/page.tsx:297](/Users/jason/Project/finance-tracker/frontend/src/app/assets/page.tsx:297) | `hasAccounts` |
| [transactions/page.tsx:50](/Users/jason/Project/finance-tracker/frontend/src/app/transactions/page.tsx:50) | `ACCOUNT_TYPE_LABELS` |
| [charts.tsx:3](/Users/jason/Project/finance-tracker/frontend/src/components/charts.tsx:3) | `useCallback`；22 行 `formatPercent` |
| [subaccount-list-editor.tsx:196](/Users/jason/Project/finance-tracker/frontend/src/components/subaccount-list-editor.tsx:196) | `arrayEqual` |
| [transaction-detail.tsx:3](/Users/jason/Project/finance-tracker/frontend/src/components/transaction-detail.tsx:3) | `useCallback`；8 行 `updateTransaction`；12 行 `ApiError` |
| [hooks.ts:4](/Users/jason/Project/finance-tracker/frontend/src/lib/hooks.ts:4) | `useSWRInfinite`；70 行 `TransactionOut`；73 行 `swrFetcher` |

后端全局定义/引用检查发现以下无生产调用或残留候选：

- [models/__init__.py:31](/Users/jason/Project/finance-tracker/backend/app/models/__init__.py:31) 的 `_utcnow()`；测试中出现的同名函数是测试自己定义的函数。
- [pdf_parser/engine.py:126](/Users/jason/Project/finance-tracker/backend/app/services/pdf_parser/engine.py:126) 的 `_extract_text_sync()`；生产已走 text+words 的新提取路径。
- [holdings.py:417](/Users/jason/Project/finance-tracker/backend/app/api/v1/holdings.py:417) 的 `_latest_fx_rate()` 兼容 wrapper；生产调用已转移。
- [valuation/engine.py](/Users/jason/Project/finance-tracker/backend/app/services/valuation/engine.py) 只剩已删除实现的说明，属于空占位模块。
- [pdf_parser/engine.py:351](/Users/jason/Project/finance-tracker/backend/app/services/pdf_parser/engine.py:351) 的 `_SUBACCOUNT_USER_NAMES` 保留为兼容空列表，不再反映当前解析上下文。
- `fetch_native_price` 的 orchestrator 导入已未使用，实际同步走批量 `fetch_native_prices`；删除导入与删除公共 helper 是两回事。

这些候选要区分内部死代码与外部兼容接口。比如 `fetchRecentlyPromoted()` 当前前端无调用，可视为未接线功能，但后端 API 有明确审计用途，应先修 F21 再决定接入或移除；不能单凭“没搜到组件调用”把公开路由删掉。`useStatementsPage` 正在被 PDF 面板使用，不能当作分页残留删除。

### 工程检查的实际缺口

- 前端 strict type check 通过，但额外未使用检查未启用，且 ESLint 配置尚未完成。建立非交互、可在 CI 重复执行的检查比一次清掉所有格式告警更有价值。
- 当前环境没有 ruff/mypy 可执行文件，本轮没有宣称通过这些检查。Python 依赖多为无上限的 `>=`，缺少与当前部署对应的完整锁定，应补可重复安装/升级验证。
- Docker Compose 明确仍为占位，所引用 Dockerfile 不存在，不应作为可用部署路径交付。其浏览器侧 API 地址 `http://backend:8000` 也需要在正式容器化时改为浏览器可访问地址，并在构建时注入。
- 文档存在过时行数/测试数、旧职责及“完全一致”的过度表述；应以实际验收场景替代“共享代码因此必然正确”的承诺。

## 测试应如何补强

现有测试覆盖了不少单功能和旧版本修复，缺口主要在**操作组合、拒绝错误输入、失败恢复以及多个入口的同义行为**。建议把本轮反例转成修复后的回归测试，并优先建立以下不变量：

| 不变量 | 必测场景 |
|---|---|
| 资金守恒 | split/unsplit 的原币、签名金额、本位币；舍入余数；禁止反向明细 |
| 导入生命周期完整 | 导入→拆分→删除→重导；导入→配对→重解析；第二行/最后一步失败不得半入库 |
| 幂等且顺序无关 | CSV 改导出窗口/顺序、复用 provider ID；相同来源同步重复执行 |
| 部分失败不是零 | 一个钱包/地址失败、现金接口失败、缺少报价时保留最后有效状态并报告缺口 |
| 配对维护一致 | 改金额/日期/类型/账户、删除任一腿、替换镜像；指针对称；不匹配同向交易 |
| 人工操作优先 | 已确认数据重匹配；用户确认发生在 LLM 请求与响应之间 |
| 单位与期间正确 | 交易/账户币种、本位币变更、跨年账期、历史日期校验 |
| 报表一致且不隐藏缺失 | monthly/timeseries/by-category；负债；多币种盈亏；缺价/缺 FX/接口错误 |
| 路由与部署可执行 | 固定路径不被捕获；空库和旧库迁移；真实监听配置与鉴权一致 |

共享实现之间的 parity 测试继续保留，但需要独立手工算例作为参照。前端金融计算不应只靠构建和类型检查，应对带币种金额的求和建立直接单元测试。

## 建议处理顺序

1. **先控制新增错误**：修拆分计算/删除、同链覆盖、部分快照清零、自动误配及跨年日期（F01–F08、F18）。这些问题通过正常导入/同步流程就可到达。
2. **收敛写入并维护状态**：修配对编辑、确认方向、派生 FX、人工保护、MCP 原子性和本位币约束；把对应服务抽出来，而非再复制条件到每个路由。
3. **修报表与可见性**：负债构成、多币种盈亏、未分类分组、缺价/错误状态、审计路由。核对条件性部署鉴权缺陷。
4. **另做已有数据核对**：以真实来源账单/快照为参照检查拆分组、来源关联、同链钱包、被误判转账、跨年 AMEX、重复 PayPal ID。代码修好不会自动修复已经算错或覆盖过的历史记录。此步应先出差异清单，再执行可回滚修复。
5. **最后清理结构和残留**：按职责拆文件、合并金融表达式、移除未使用声明、完善迁移与 lint、更新文档。本轮没有估计工期；财务数据回填范围需要真实差异清单才能确定。

## 复现与交付说明

常规验证（项目根目录）：

```sh
DATABASE_URL=sqlite:////private/tmp/finance-review-v9-tests.db PYTHONPATH=backend:mcp-server/src .venv/bin/pytest backend/tests mcp-server/tests --ignore=backend/tests/test_api.py -q --disable-warnings
```

本轮反例单独运行：

```sh
PYTHONPATH=backend:mcp-server/src .venv/bin/pytest 'code review/repro_v9.py' -q --tb=short -o asyncio_mode=auto
```

反例文件在导入应用前覆盖测试环境配置，使用独立内存库；MCP 故障用例使用 pytest 临时目录。未启动应用 lifespan。**当前代码预期得到 22 个失败断言**；这些不是常规 420 项测试退化，而是本轮补充的正确性要求尚未满足。修复后应检查失败逐项消失，并把稳定用例移至正式套件；不要通过放宽断言或把错误数值作为期望值让它们变绿。

本轮交付的报告、反例与结果记录均在 `code review/`。没有实施修复、提交 Git、发布部署或变更真实金融数据。
