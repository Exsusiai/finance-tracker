# 加密钱包同步方案 (P1-4)

> 起源：用户 2026-05-03 提出"新建加密钱包账户时只填公钥即同步"
>
> **状态（2026-05-19）**：✅ **阶段 1 已实装** + Phase 1 价格自动 + Phase 2 include_in_total / Bitget 合约。可用、稳定、有 79 项单测覆盖。剩余可选扩展：Binance 合约钱包、持仓表 UI 加列、长尾链补全（P2）。
>
> 决策范围（用户已确认 2026-05-03）：
> - **覆盖**：主流 L1 + L2 全部纳入
> - **范围**：仅现货持仓 + CEX 合约钱包余额（按用户 2026-05-19 修订加入合约）
> - **多地址聚合**：同一"加密钱包账户"可包含跨链多个地址（ETH+BTC+SOL 视为同一钱包）
> - **预算**：以免费 tier 优先；不需要任何前置开销即可使用（解释见 §4）

---

## 1. 链覆盖目标

### L1（一级链）
| 链 | 资产 | 优先级 |
|---|---|---|
| Ethereum | ETH + ERC-20 | 必须 |
| Bitcoin | BTC (UTXO) | 必须 |
| Solana | SOL + SPL token | 必须 |
| BNB Smart Chain | BNB + BEP-20 | 高 |
| Avalanche C-Chain | AVAX + ERC-20 | 高 |
| Polygon PoS | MATIC + ERC-20 | 高 |
| Tron | TRX + TRC-20 (含 USDT-TRX) | 高 |
| Cosmos Hub | ATOM | 中 |
| Polkadot | DOT | 中 |
| Cardano | ADA | 中 |
| Sui | SUI | 中 |
| Aptos | APT | 中 |
| TON | TON | 中 |

### L2（二级链 / Rollup） — 全部 EVM
| 链 | 复用 EVM 工具 |
|---|---|
| Arbitrum One | ✅ |
| Optimism | ✅ |
| Base | ✅ |
| zkSync Era | ✅ |
| Polygon zkEVM | ✅ |
| Linea | ✅ |
| Scroll | ✅ |
| Mantle | ✅ |
| Blast | ✅ |
| StarkNet | ⚠️ 非 EVM，独立 indexer |

---

## 2. 推荐开源 / 免费数据源（首选**单家聚合**优先级最高）

### 选项 A — **直接借鉴/集成 rotki**（强烈推荐）

[`rotki/rotki`](https://github.com/rotki/rotki) — 3.8k★ AGPLv3 Python 项目，**与本项目栈完全匹配**。

- 100% 开源、本地优先（与本项目同哲学）
- Python 后端 + SQLite 存储
- 已支持上述几乎全部 L1/L2 的资产读取
- 内置 CoinGecko 行情、CryptoCompare 历史价
- 模块化：`rotkehlchen/chain/{ethereum,bitcoin,solana,...}` 可单独参考

**集成方式 3 选 1**：

1. **直接集成代码** — 把 `rotkehlchen/chain/` 模块拷过来，移除 GUI 依赖；遵守 AGPLv3 → 我们项目也得 AGPLv3 或保持源码可见
2. **作为子进程调用** — `rotki` daemon 跑在 localhost，我们 HTTP 调它；许可隔离，但部署变重
3. **只参考实现，自写代码** — 看它怎么处理每条链的边界情况，自己实现；许可上最干净

**建议**：先走 (3) 学其逻辑、用其依赖（`web3.py`, `bitcoin-utils`, `solana-py`），写自己的精简版。

### 选项 B — 多家公网 API 拼接（不引入开源资产管理项目）

| 链类别 | 提供商 | 免费 tier | 备注 |
|---|---|---|---|
| 所有 EVM (L1+L2) | **Alchemy** | 300M CU/月（够用到爆） | `alchemy_getTokenBalances` 一个调用拿原生 + ERC-20，覆盖 ETH/Polygon/Arbitrum/Optimism/Base/zkSync 等 |
| EVM 备选 | Moralis | 40k req/月 | `wallets/:address/tokens` 多链聚合 |
| EVM 备选 | Covalent | 100k req/月 | `wallets/balances_v2` 多链聚合 |
| Bitcoin | **Blockstream Esplora** | 完全免费公网 | `/address/{addr}/utxo` |
| Bitcoin 备选 | mempool.space | 完全免费公网 | 同样 REST |
| Solana | **Helius** | 100k req/月 | `getTokenAccountsByOwner` |
| Solana 备选 | QuickNode | 免费 tier | RPC |
| Tron | TronGrid | 完全免费 | 官方 |
| Cosmos / Polkadot 等 | 各自官方 RPC | 公链免费 | 数据稀疏，逐链对接 |

**建议组合**：Alchemy（一家覆盖所有 EVM L1+L2）+ Blockstream（BTC）+ Helius（Solana）+ TronGrid（TRX）= **~4 家、全免费**，已覆盖 90%+ 用户实际持仓。其它长尾链（Cosmos/Polkadot/Cardano/...）作为 P2 增量逐链补。

### 选项 C — 不直接读链，只挂"交易所"账户

许多用户实际把币放在 Binance / Coinbase / OKX 这类 CEX。这是另一类工作，与"链上同步"互补，可以独立排到 P2。

---

## 3. 实施分期

### 阶段 1（P1-4 第一波，约 2-3 天）
- `AccountForm` type=crypto_wallet 时显示**多地址输入框**（每行一个，前端按地址前缀提示链）
- 后端 `bank_connections` / 或扩展 `accounts.metadata_json` 存 `addresses: [{chain, address, label?}]`
- 链识别函数：
  - `0x` + 40 hex → EVM
  - `bc1` / `1` / `3` 开头 → BTC
  - base58 32 字节 → Solana
  - `T` 开头 → Tron
- 数据源：Alchemy + Blockstream + Helius + TronGrid
- 同步任务：手动触发 `POST /api/v1/accounts/{id}/sync-chain`，遍历所有 address → fetch token list → upsert holdings + assets
- ENS / SNS 解析放 P2

### 阶段 2（P1-4 第二波，1-2 天）
- 接入 APScheduler（依赖 P0-1）→ 日级别自动同步
- 行情接 CoinGecko 的 `simple/token_price` 用 contract address 取 token 价（避免维护 token→symbol 字典）
- 长尾链（BNB / AVAX / Cosmos 等）逐链补

### 阶段 3（P2，按需）
- 借鉴 rotki 把 Polkadot / Cardano / Cosmos / Sui / Aptos / TON / StarkNet 等非 EVM 长尾链补齐
- 域名解析：ENS（mainnet）/ SNS（Solana）

---

## 4. 关于"月度预算"的澄清

你问："我们需要先有开销才能查看钱包资产价值吗？"

**不需要**。"月度预算"只是**如果将来**用付费 tier 才会产生月费。具体到本项目：

- **现在 0 元起步即可**：上面所有推荐数据源（Alchemy / Blockstream / Helius / TronGrid / CoinGecko）都有免费 tier，对**单用户、日级同步**频率而言**远未触顶**
- 只有出现以下场景才**可能**需要付费：
  - 你想做秒级实时刷新（远超个人需要）
  - 你的钱包数量 + 链数 × 同步频率超出 Alchemy 300M CU / Helius 100k req 等额度（实际几乎不可能）
  - 想接入 token 价格历史回放（CoinGecko Pro $129/月起）
- **当前阶段不会触发任何付费**

---

## 5. 已确定的决策（用户答复 2026-05-03）

| # | 议题 | 决策 | 备注 |
|---|---|---|---|
| 1 | 集成方式 | **参考 rotki 自写精简版**（§2 选项 A.3） | 学其链识别与边界处理，依赖用 `web3.py` / `bitcoin-utils` / `solana-py`。许可上最干净，不会被 AGPL 传染 |
| 2 | CEX 账户接入范围 | **阶段 1 一并纳入** Binance + Bitget | 用户大量资产存放于此两家。新建账户 `type=brokerage`/新 type 添加 API key/secret（加密入库） |
| 3 | ENS / SNS 域名解析 | **砍掉**，不实现 | 个人钱包用例下零增量价值。详细解释见用户答复 |
| 4 | 同步频率 | **手动触发**（不接 scheduler） | 每个钱包账户卡片提供"立即同步"按钮；不依赖 P0-1 |

## 6. 修订后的实施分期

### ✅ 阶段 1 已交付（2026-05-18~19）
- ✅ `AccountForm` `type=crypto_wallet` 多地址输入（每行选链 + 地址 + 备注；创建后立刻进「添加地址」模式不关弹窗）
- ✅ `AccountForm` `type=exchange`（新增枚举，machine 选 Binance / Bitget）+ API key/secret/passphrase（AES-256-GCM 加密入 `exchange_connections.api_*_enc` 三列，复用 `FINANCE_BANK_ENCRYPTION_KEY`）
- ✅ 链同步 service `services/crypto_sync/`：Alchemy（11 EVM L1+L2）+ Blockstream（BTC）+ 公共 Solana RPC（**Helius 砍掉，公共 RPC 已足够**）+ TronGrid
- ✅ CEX 同步 `services/exchange_sync/`：
  - Binance `GET /api/v3/account`（仅现货）
  - Bitget spot + USDT-M + USDC-M + COIN-M 四端点聚合，按 coin 跨端点 `sum(available + locked)`（不计 unrealizedPL）
- ✅ Orchestrator `POST /api/v1/accounts/{id}/sync` 按 `account.type` 分发；per-source 失败不中断
- ✅ 账户卡 ↻ 立即同步 按钮 + 每错误源详情显示
- ✅ 价格自动发现 `services/market_data/coingecko.py`：同步后按 chain per-contract 拉 `token_price`（免费 tier 1-call 限制） + 按 ticker 拉 `simple/price` for natives
- ✅ Spam 过滤 `services/wallet_sync/spam_filter.py`（URL / CLAIM / VISIT 等关键词；symbol 长度 > 20 字符）
- ✅ Per-(chain, account) upsert：缺失 token 设 `quantity=0, is_active=False`；asset 按 symbol 去重
- ✅ 加密总值进 `net_worth`（USDT/USDC/DAI 别名 USD；CNY pivot 三角换算）
- ✅ `accounts.include_in_total` 字段 + AccountForm checkbox + 卡片 dim + 「不计入总资产」徽章

### 阶段 2（可选 / P2）
- ❌ **Binance 合约钱包**（USDT-M `/fapi/v2/account` + 币本位 `/dapi/v1/account`）— 与 Bitget 同 pattern，半天工作量；用户可选
- ❌ **持仓表 UI 加列**（数量 / 当前价 / 市值 / 成本价）— 成本价手工录入（链上拿不到买入价上下文）；用户明确想要
- ❌ 长尾链补全：BNB Smart Chain / AVAX C-Chain（Alchemy 支持，加映射）/ Cosmos / Polkadot / Cardano / Sui / Aptos / TON
- ❌ 接入 APScheduler 改自动日级同步（依赖 P0-1）
- ❌ 第二批 CEX：OKX / Coinbase / Kraken

### 阶段 3（暂不规划）
- 质押 / LP / 借贷头寸（用户已明确"仅现货"）
- ENS / SNS 域名解析（已砍）

---

## 7. 安全注意事项（CEX API 接入）

- **必须使用只读 API key**：在交易所后台创建 key 时勾选"仅查看"权限，禁止"交易""提币"
- key 一律走 `FINANCE_BANK_ENCRYPTION_KEY` 加密后入库，不进 `.env`
- 前端不显示已保存的 secret（仅显示掩码 `••••...••••`）
- 新建账户表单需在 UI 上提示用户"请确保 API key 只勾选 Read 权限"

