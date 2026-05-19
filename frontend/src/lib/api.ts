const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("finance_api_token");
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(options?.headers as Record<string, string> || {}),
  };

  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body?.error?.message || res.statusText, body?.error?.code);
  }

  const json = await res.json();
  return json.data as T;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public code?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

// ─── Cash Flow ──────────────────────────────────────────────────────────

export interface CashFlowMonthly {
  period: string;
  // Currency unit for ALL numeric fields below; matches backend BASE_CURRENCY.
  // Front-end book-keeping views should label numbers with this, not the
  // user's display-currency toggle.
  base_currency: string;
  income: string;
  expense: string;
  transfer: string;
  savings: string;
  fx_missing_count?: number;
  by_category: Record<string, string>;
  by_account: Record<string, string>;
}

export interface CashFlowByCategory {
  category_id: number | null;
  category_name: string;
  kind: string;
  total: string;
  count: number;
}

export interface CashFlowTimeseries {
  periods: string[];
  income: string[];
  expense: string[];
  savings: string[];
}

export async function fetchCashFlowMonthly(from?: string, to?: string): Promise<CashFlowMonthly[]> {
  const params = new URLSearchParams();
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  return request(`/api/v1/cashflow/monthly?${params}`);
}

export async function fetchCashFlowByCategory(period: string): Promise<CashFlowByCategory[]> {
  return request(`/api/v1/cashflow/by-category?period=${period}`);
}

export async function fetchCashFlowTimeseries(from?: string, to?: string): Promise<CashFlowTimeseries> {
  const params = new URLSearchParams();
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  return request(`/api/v1/cashflow/timeseries?${params}`);
}

// ─── Portfolio / Assets ────────────────────────────────────────────────

export interface PortfolioSummary {
  base_currency: string;
  total_value: string;
  as_of: string;
  by_class: Record<string, string>;
  by_currency: Record<string, string>;
}

export interface PortfolioBreakdown {
  by_class: Record<string, { value: string; count: number; assets: Array<{ symbol: string; name: string; value: string }> }>;
  by_currency: Record<string, { value: string; count: number }>;
}

export interface HoldingOut {
  id: number;
  account_id: number;
  account_name: string | null;
  asset_id: number;
  symbol: string | null;
  asset_name: string | null;
  asset_class: string | null;
  quantity: string;
  avg_cost: string | null;
  cost_currency: string | null;
  current_price: string | null;
  market_value: string | null;
  unrealized_pnl: string | null;
  last_synced_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface BalanceOut {
  account_id: number;
  account_name: string;
  currency: string;
  balance: string;
}

export async function fetchPortfolioSummary(): Promise<PortfolioSummary> {
  return request("/api/v1/holdings/portfolio/summary");
}

export interface NetWorthData {
  base_currency: string;
  cash_total: string;
  investment_total: string;
  net_worth: string;
  cash_by_currency: Record<string, { original: string; converted: string }>;
  investment_by_currency: Record<string, string>;
  as_of: string;
}

export async function fetchNetWorth(): Promise<NetWorthData> {
  return request("/api/v1/holdings/portfolio/net-worth");
}

export async function fetchPortfolioBreakdown(): Promise<PortfolioBreakdown> {
  return request("/api/v1/holdings/portfolio/breakdown");
}

export async function fetchHoldings(): Promise<HoldingOut[]> {
  return request("/api/v1/holdings");
}

export async function fetchBalances(): Promise<BalanceOut[]> {
  return request("/api/v1/accounts/balances");
}

// ─── Assets (CRUD) ─────────────────────────────────────────────────────

export interface AssetOut {
  id: number;
  symbol: string;
  name: string;
  asset_class: string;
  currency: string;
  market: string | null;
  data_source: string | null;
  data_source_id: string | null;
  decimals: number;
  notes: string | null;
  latest_price: string | null;
  latest_price_currency: string | null;
  created_at: string;
  updated_at: string;
}

export interface AssetCreateInput {
  symbol: string;
  name: string;
  asset_class: string;
  currency: string;
  market?: string;
  data_source?: string;
  data_source_id?: string;
  decimals?: number;
  notes?: string;
}

export interface AssetUpdateInput {
  name?: string;
  data_source?: string;
  data_source_id?: string;
  decimals?: number;
  notes?: string;
}

export async function fetchAssets(assetClass?: string): Promise<AssetOut[]> {
  const params = assetClass ? `?asset_class=${assetClass}` : "";
  return request(`/api/v1/assets${params}`);
}

export async function createAsset(data: AssetCreateInput): Promise<AssetOut> {
  return request("/api/v1/assets", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function updateAsset(id: number, data: AssetUpdateInput): Promise<AssetOut> {
  return request(`/api/v1/assets/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export async function deleteAsset(id: number): Promise<{ id: number; deleted: boolean }> {
  return request(`/api/v1/assets/${id}`, { method: "DELETE" });
}

export interface AssetSearchResult {
  symbol: string;
  name: string;
  asset_class: string;
  currency: string;
  data_source: string;
  data_source_id: string;
  market?: string | null;
  thumb?: string | null;
}

export async function searchAssets(
  query: string,
  assetClass?: string,
): Promise<AssetSearchResult[]> {
  const params = new URLSearchParams({ query });
  if (assetClass) params.set("asset_class", assetClass);
  return request(`/api/v1/assets/search?${params}`);
}

// ─── Holdings (CRUD) ───────────────────────────────────────────────────

export interface HoldingCreateInput {
  account_id: number;
  asset_id: number;
  quantity: string;
  avg_cost?: string;
  cost_currency?: string;
  notes?: string;
}

export interface HoldingUpdateInput {
  quantity?: string;
  avg_cost?: string;
  cost_currency?: string;
  notes?: string;
}

export async function createHolding(data: HoldingCreateInput): Promise<HoldingOut> {
  return request("/api/v1/holdings", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function updateHolding(id: number, data: HoldingUpdateInput): Promise<HoldingOut> {
  return request(`/api/v1/holdings/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export async function deleteHolding(id: number): Promise<{ id: number; deleted: boolean }> {
  return request(`/api/v1/holdings/${id}`, { method: "DELETE" });
}

// ─── Export (CSV) ──────────────────────────────────────────────────────

export function downloadCsv(data: Record<string, string>[], filename: string) {
  if (data.length === 0) return;
  const headers = Object.keys(data[0]);
  const csvRows = [
    headers.join(","),
    ...data.map((row) =>
      headers.map((h) => {
        const val = row[h] ?? "";
        return `"${val.replace(/"/g, '""')}"`;
      }).join(",")
    ),
  ];
  const blob = new Blob(["\uFEFF" + csvRows.join("\n")], { type: "text/csv;charset=utf-8" });
  downloadBlob(blob, filename);
}

export function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// ─── Transactions ──────────────────────────────────────────────────────

export interface TransactionFilters {
  account_id?: number;
  category_id?: number;
  type?: string;
  from_date?: string;
  to_date?: string;
  min_amount?: string;
  max_amount?: string;
  search?: string;
  tags?: string;
  source?: string;
  is_pending?: boolean;
  limit?: number;
  cursor?: string;
}

export interface TransactionListMeta {
  next_cursor: string | null;
  total: number;
}

export interface TransactionListResponse {
  data: TransactionOut[];
  meta: TransactionListMeta;
}

/** Mirrors backend TransactionType (expense/income/transfer/adjustment).
 *  Kept as a string-literal union so IDEs catch missing branches when
 *  switching on tx.type — the previous bare `string` typing meant any
 *  comparison was a free-for-all. */
export type TransactionType = "expense" | "income" | "transfer" | "adjustment";

export interface TransactionOut {
  id: number;
  account_id: number;
  account_name: string | null;
  counter_account_id: number | null;
  category_id: number | null;
  category_name: string | null;
  occurred_at: string;
  posted_at: string | null;
  amount: string;
  currency: string;
  fx_rate_to_base: string | null;
  base_amount: string | null;
  type: TransactionType;
  description: string | null;
  raw_description: string | null;
  counterparty: string | null;
  location: string | null;
  tags: string[];
  source: string;
  pdf_import_id: number | null;
  external_id: string | null;
  is_pending: boolean;
  metadata_json: string | null;
  user_note: string | null;
  created_at: string;
  updated_at: string;
}

export interface TransactionCreateInput {
  account_id: number;
  counter_account_id?: number;
  category_id?: number;
  occurred_at: string;
  posted_at?: string;
  amount: string;
  currency: string;
  type: string;
  description?: string;
  counterparty?: string;
  location?: string;
  tags?: string[];
  is_pending?: boolean;
}

export interface TransactionUpdateInput {
  account_id?: number;
  counter_account_id?: number;
  category_id?: number | null;
  occurred_at?: string;
  posted_at?: string;
  amount?: string;
  currency?: string;
  type?: string;
  description?: string;
  counterparty?: string;
  location?: string;
  tags?: string[];
  is_pending?: boolean;
}

export async function fetchTransactions(
  filters?: TransactionFilters
): Promise<TransactionListResponse> {
  const params = new URLSearchParams();
  if (filters) {
    Object.entries(filters).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") {
        params.set(k, String(v));
      }
    });
  }
  const res = await fetch(`${API_BASE}/api/v1/transactions?${params}`, {
    headers: {
      ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
    },
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body?.error?.message || res.statusText, body?.error?.code);
  }

  const json = await res.json();
  return { data: json.data, meta: json.meta };
}

export async function fetchTransaction(id: number): Promise<TransactionOut> {
  return request(`/api/v1/transactions/${id}`);
}

export async function createTransaction(data: TransactionCreateInput): Promise<TransactionOut> {
  return request(`/api/v1/transactions`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

/** How a manual category change should propagate.
 *  - "all":    create/strengthen rule + cascade to identical-description siblings (default)
 *  - "single": skip both — one-off correction only
 *  - "never":  skip both AND disable any existing auto-rule for this keyword
 */
export type ApplyScope = "all" | "single" | "never";

export async function updateTransaction(
  id: number,
  data: TransactionUpdateInput,
  applyScope?: ApplyScope,
): Promise<TransactionOut> {
  const qs = applyScope ? `?apply_scope=${applyScope}` : "";
  return request(`/api/v1/transactions/${id}${qs}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export async function fetchSimilarCount(
  id: number,
  categoryId?: number | null,
): Promise<{ count: number; keyword: string | null }> {
  const qs = categoryId != null ? `?category_id=${categoryId}` : "";
  return request(`/api/v1/transactions/${id}/similar-count${qs}`);
}

export async function deleteTransaction(id: number): Promise<{ id: number; deleted: boolean }> {
  return request(`/api/v1/transactions/${id}`, { method: "DELETE" });
}

export async function recategorizeTransaction(id: number): Promise<TransactionOut> {
  return request(`/api/v1/transactions/${id}/categorize`, { method: "POST" });
}

// ─── Categories ────────────────────────────────────────────────────────

export interface CategoryOut {
  id: number;
  name: string;
  kind: string;
  parent_id: number | null;
  icon: string | null;
  color: string | null;
  sort_order: number;
  is_system: boolean;
  created_at: string;
}

export interface CategoryTree {
  id: number;
  name: string;
  kind: string;
  icon: string | null;
  color: string | null;
  sort_order: number;
  is_system: boolean;
  children: CategoryTree[];
}

export async function fetchCategories(kind?: string): Promise<CategoryOut[]> {
  const params = kind ? `?kind=${kind}` : "";
  return request(`/api/v1/categories${params}`);
}

export async function fetchCategoryTree(): Promise<CategoryTree[]> {
  return request("/api/v1/categories/tree");
}

export interface CategoryCreateInput {
  name: string;
  kind: string;
  parent_id?: number | null;
  icon?: string | null;
  color?: string | null;
  sort_order?: number;
}

export interface CategoryUpdateInput {
  name?: string;
  parent_id?: number | null;
  icon?: string | null;
  color?: string | null;
  sort_order?: number;
}

export async function createCategory(data: CategoryCreateInput): Promise<CategoryOut> {
  return request("/api/v1/categories", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function updateCategory(id: number, data: CategoryUpdateInput): Promise<CategoryOut> {
  return request(`/api/v1/categories/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export async function deleteCategory(id: number): Promise<{ id: number; deleted: boolean }> {
  return request(`/api/v1/categories/${id}`, { method: "DELETE" });
}

// ─── Accounts ──────────────────────────────────────────────────────────

export interface AccountOut {
  id: number;
  name: string;
  type: string;
  institution: string | null;
  account_number: string | null;
  iban: string | null;
  currency: string;
  initial_balance: string;
  is_active: boolean;
  include_in_total: boolean;
  notes: string | null;
  metadata_json: string | null;
  created_at: string;
  updated_at: string;
}

export async function fetchAccounts(activeOnly?: boolean): Promise<AccountOut[]> {
  const params = activeOnly ? "?active_only=true" : "";
  return request(`/api/v1/accounts${params}`);
}

export interface AccountCreateInput {
  name: string;
  type: string;
  institution?: string;
  account_number?: string;
  iban?: string;
  currency: string;
  initial_balance?: string;
  include_in_total?: boolean;
  notes?: string;
}

export interface AccountUpdateInput {
  name?: string;
  type?: string;
  institution?: string;
  account_number?: string;
  iban?: string | null;
  currency?: string;
  is_active?: boolean;
  include_in_total?: boolean;
  notes?: string;
  metadata_json?: string | null;
}

export async function createAccount(data: AccountCreateInput): Promise<AccountOut> {
  return request("/api/v1/accounts", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function updateAccount(id: number, data: AccountUpdateInput): Promise<AccountOut> {
  return request(`/api/v1/accounts/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export async function deleteAccount(id: number): Promise<{ id: number; deleted: boolean }> {
  return request(`/api/v1/accounts/${id}`, { method: "DELETE" });
}

export interface BalanceAdjustmentInput {
  target_balance: string;
  note?: string;
  occurred_at?: string;
}

export async function adjustAccountBalance(
  accountId: number,
  data: BalanceAdjustmentInput,
): Promise<BalanceOut> {
  return request(`/api/v1/accounts/${accountId}/adjust-balance`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

// ─── PDF Import ────────────────────────────────────────────────────────

// ─── Transfer matching ────────────────────────────────────────────────

export interface TransferSuggestion {
  out_transaction_id: number;
  in_transaction_id: number;
  out_account_id: number;
  in_account_id: number;
  amount: string;
  currency: string;
  out_date: string;
  in_date: string;
  out_description: string | null;
  in_description: string | null;
  score: number;
  reasons: string[];
}

export async function fetchTransferSuggestions(): Promise<TransferSuggestion[]> {
  return request("/api/v1/transactions/transfers/suggestions");
}

export interface UnpairedTransfer {
  transaction_id: number;
  account_id: number;
  account_name: string | null;
  category_id: number | null;
  category_name: string | null;
  occurred_at: string;
  amount: string;
  currency: string;
  description: string | null;
  raw_description: string | null;
  transfer_direction: "in" | "out" | null;
}

export async function fetchUnpairedTransfers(): Promise<UnpairedTransfer[]> {
  return request("/api/v1/transactions/transfers/unpaired");
}

export interface CounterLegCandidate {
  transaction_id: number;
  account_id: number;
  account_name: string | null;
  occurred_at: string;
  amount: string;
  // Signed difference: candidate.amount - source.amount. Positive = candidate
  // is larger. UI formats with explicit + / - prefix.
  amount_diff: string;
  currency: string;
  type: string;
  description: string | null;
  raw_description: string | null;
  days_diff: number | null;
  // 'free'             — unpaired, clean candidate
  // 'synthetic_bound'  — currently paired to a synthetic mirror leg; binding
  //                      will retire the synthetic and pair to the real leg
  status: "free" | "synthetic_bound";
}

export async function fetchCounterLegCandidates(
  txId: number,
  windowDays = 10,
  amountTolerance: string | number = "0.01",
): Promise<CounterLegCandidate[]> {
  const tol = String(amountTolerance);
  return request(
    `/api/v1/transactions/transfers/${txId}/counter-leg-candidates?window_days=${windowDays}&amount_tolerance=${encodeURIComponent(tol)}`,
  );
}

export async function unbindTransferCounter(
  id: number,
): Promise<{ transaction_id: number; counterpart_id: number | null; deleted_synthetic: boolean }> {
  return request(`/api/v1/transactions/${id}/unbind-counter`, {
    method: "POST",
  });
}

export interface RefreshMatchingSummary {
  orphan_pointers_cleared: number;
  type_promoted_to_transfer: number;
  recategorized: number;
  subaccount_pairs: number;
  single_leg_iban: number;
  auto_paired: number;
  orphan_paired: number;
  subaccount_orphans_categorized: number;
  reenqueued_to_inbox: number;
  periods_recomputed: number;
  // Rows queued for asynchronous L2 LLM classification (fire-and-forget;
  // results land in the inbox or on the row's metadata.llm_suggestion).
  llm_dispatched: number;
}

export async function refreshAllMatching(): Promise<RefreshMatchingSummary> {
  return request("/api/v1/system/refresh-matching", { method: "POST" });
}

export interface PromotedTransaction {
  transaction_id: number;
  account_id: number;
  account_name: string | null;
  category_id: number | null;
  category_name: string | null;
  occurred_at: string;
  amount: string;
  currency: string;
  description: string | null;
  current_type: string;
  original_type: string | null;
  promoted_at: string | null;
}

export async function fetchRecentlyPromoted(): Promise<PromotedTransaction[]> {
  return request("/api/v1/transactions/recently-promoted-to-transfer");
}

export async function revertTypePromotion(id: number): Promise<TransactionOut> {
  return request(`/api/v1/transactions/${id}/revert-type-promotion`, {
    method: "POST",
  });
}

export async function markAsTransfer(
  id: number,
  options?: {
    counterTransactionId?: number;
    counterAccountId?: number | null;
    direction?: "in" | "out";
    categoryId?: number | null;
    // Allowed |out - in| amount diff; default 0.01 (cent precision).
    // Pass a larger value (e.g. "5") for manual binding when the legs
    // legitimately differ (uneven split / rounded reimbursement).
    amountTolerance?: string | number;
  },
): Promise<TransactionOut> {
  return request(`/api/v1/transactions/${id}/mark-transfer`, {
    method: "POST",
    body: JSON.stringify({
      counter_transaction_id: options?.counterTransactionId ?? null,
      counter_account_id: options?.counterAccountId ?? null,
      transfer_direction: options?.direction ?? null,
      category_id: options?.categoryId ?? null,
      amount_tolerance:
        options?.amountTolerance != null ? String(options.amountTolerance) : null,
    }),
  });
}

// ─── Inbox (pending transactions awaiting user confirmation) ──────────

export async function fetchInbox(limit: number = 100): Promise<TransactionOut[]> {
  return request(`/api/v1/transactions/inbox/list?limit=${limit}`);
}

export async function confirmInboxItem(
  id: number,
  data: {
    category_id?: number | null;
    description?: string | null;
    user_note?: string | null;
  },
  applyScope?: ApplyScope,
): Promise<TransactionOut> {
  const qs = applyScope ? `?apply_scope=${applyScope}` : "";
  return request(`/api/v1/transactions/inbox/${id}/confirm${qs}`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

// ─── FX rates ──────────────────────────────────────────────────────────

export interface FxRateOut {
  base_currency: string;
  quote_currency: string;
  quoted_at: string;
  rate: string;
  source: string;
}

export async function fetchFxRates(
  base: string = "CNY",
  limit: number = 50,
): Promise<FxRateOut[]> {
  return request(`/api/v1/market/fx?base=${encodeURIComponent(base)}&limit=${limit}`);
}

export async function triggerMarketRefresh(): Promise<{
  prices_updated: number;
  fx_updated: number;
  errors: string[];
}> {
  return request(`/api/v1/market/refresh`, { method: "POST" });
}

export interface PdfImportOut {
  id: number;
  filename: string;
  file_hash: string;
  file_size: number;
  detected_bank: string | null;
  parser_version: string | null;
  account_id: number | null;
  statement_period: string | null;
  transactions_count: number;
  status: string;
  error_message: string | null;
  preview: TransactionOut[];
  created_at: string;
  updated_at: string;
}

export async function uploadPdf(
  file: File,
  accountId?: number
): Promise<PdfImportOut> {
  const formData = new FormData();
  formData.append("file", file);

  const params = accountId ? `?account_id=${accountId}` : "";
  const token = getToken();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${API_BASE}/api/v1/statements/upload${params}`, {
    method: "POST",
    headers,
    body: formData,
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body?.error?.message || res.statusText, body?.error?.code);
  }

  const json = await res.json();
  return json.data;
}

export async function fetchStatements(limit?: number, status?: string): Promise<PdfImportOut[]> {
  const params = new URLSearchParams();
  if (limit) params.set("limit", String(limit));
  if (status) params.set("status", status);
  return request(`/api/v1/statements?${params}`);
}

export async function fetchStatement(id: number): Promise<PdfImportOut> {
  return request(`/api/v1/statements/${id}`);
}

export async function confirmStatement(id: number): Promise<{ import_id: number; confirmed: number }> {
  return request(`/api/v1/statements/${id}/confirm`, { method: "POST" });
}

export async function deleteStatement(id: number): Promise<{ id: number; deleted: boolean }> {
  return request(`/api/v1/statements/${id}`, { method: "DELETE" });
}

// ─── Categorization Notes (knowledge base) ─────────────────────────────

export interface CategorizationNoteOut {
  id: number;
  category_id: number;
  category_name: string | null;
  trigger_text: string;
  note_text: string;
  source_transaction_id: number | null;
  usage_count: number;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface CategorizationNoteCreateInput {
  category_id: number;
  trigger_text: string;
  note_text: string;
  enabled?: boolean;
}

export interface CategorizationNoteUpdateInput {
  category_id?: number;
  trigger_text?: string;
  note_text?: string;
  enabled?: boolean;
}

export async function fetchCategorizationNotes(params?: {
  category_id?: number;
  enabled?: boolean;
}): Promise<CategorizationNoteOut[]> {
  const search = new URLSearchParams();
  if (params?.category_id !== undefined) search.set("category_id", String(params.category_id));
  if (params?.enabled !== undefined) search.set("enabled", String(params.enabled));
  const qs = search.toString();
  return request(`/api/v1/categorization-notes${qs ? `?${qs}` : ""}`);
}

export async function createCategorizationNote(
  data: CategorizationNoteCreateInput,
): Promise<CategorizationNoteOut> {
  return request(`/api/v1/categorization-notes`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function updateCategorizationNote(
  id: number,
  data: CategorizationNoteUpdateInput,
): Promise<CategorizationNoteOut> {
  return request(`/api/v1/categorization-notes/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export async function deleteCategorizationNote(
  id: number,
): Promise<{ id: number; deleted: boolean }> {
  return request(`/api/v1/categorization-notes/${id}`, { method: "DELETE" });
}

// ─── LLM settings + cost ───────────────────────────────────────────────

export interface LLMSettingsOut {
  enabled: boolean;
  provider: string;
  model: string;
  monthly_usd_budget: number;
  confidence_threshold: number;
  use_grounding: boolean;
  max_notes_in_prompt: number;
  api_key_present: boolean;
}

export interface LLMSettingsUpdateInput {
  enabled?: boolean;
  model?: string;
  monthly_usd_budget?: number;
  confidence_threshold?: number;
  use_grounding?: boolean;
  max_notes_in_prompt?: number;
  // Empty string clears the stored key. Omit to leave it untouched.
  gemini_api_key?: string;
}

export interface LLMCostOut {
  used_usd: number;
  budget_usd: number;
  remaining_usd: number;
  period: string;
}

export async function fetchLLMSettings(): Promise<LLMSettingsOut> {
  return request(`/api/v1/llm/settings`);
}

export async function updateLLMSettings(
  data: LLMSettingsUpdateInput,
): Promise<LLMSettingsOut> {
  return request(`/api/v1/llm/settings`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export async function fetchLLMCost(): Promise<LLMCostOut> {
  return request(`/api/v1/llm/cost`);
}

// ─── Wallet sync (P1-4) ─────────────────────────────────────────────────────

export interface ChainAddressOut {
  id: number;
  chain: string;
  address: string;
  label: string | null;
  last_synced_at: string | null;
  last_sync_status: string | null;
  last_sync_error: string | null;
}

export interface ChainAddressInput {
  chain: string;
  address: string;
  label?: string | null;
}

export async function fetchChainAddresses(
  accountId: number,
): Promise<ChainAddressOut[]> {
  return request(`/api/v1/accounts/${accountId}/addresses`);
}

export async function addChainAddress(
  accountId: number,
  data: ChainAddressInput,
): Promise<ChainAddressOut> {
  return request(`/api/v1/accounts/${accountId}/addresses`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function deleteChainAddress(
  accountId: number,
  addrId: number,
): Promise<{ deleted: number }> {
  return request(`/api/v1/accounts/${accountId}/addresses/${addrId}`, {
    method: "DELETE",
  });
}

export interface ExchangeConnectionOut {
  id: number;
  // Narrowed to the literal union — backend enforces this via the
  // `ck_exchange_conn_exchange` CHECK and the dispatcher whitelist,
  // so any string outside this set is a bug the UI shouldn't accept.
  exchange: "binance" | "bitget";
  has_credentials: boolean;
  has_passphrase: boolean;
  last_synced_at: string | null;
  last_sync_status: string | null;
  last_sync_error: string | null;
}

export interface ExchangeConnectionInput {
  exchange: "binance" | "bitget";
  api_key: string;
  api_secret: string;
  passphrase?: string | null;
}

export async function fetchExchangeConnection(
  accountId: number,
): Promise<ExchangeConnectionOut | null> {
  return request(`/api/v1/accounts/${accountId}/exchange-connection`);
}

export async function upsertExchangeConnection(
  accountId: number,
  data: ExchangeConnectionInput,
): Promise<ExchangeConnectionOut> {
  return request(`/api/v1/accounts/${accountId}/exchange-connection`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export async function deleteExchangeConnection(
  accountId: number,
): Promise<{ deleted: number }> {
  return request(`/api/v1/accounts/${accountId}/exchange-connection`, {
    method: "DELETE",
  });
}

export interface SyncResultOut {
  label: string;
  chain: string | null;
  exchange: string | null;
  synced: number;
  error: string | null;
}

export interface SyncSummaryOut {
  account_id: number;
  account_type: string;
  total_synced: number;
  total_errors: number;
  results: SyncResultOut[];
}

export async function syncAccount(accountId: number): Promise<SyncSummaryOut> {
  return request(`/api/v1/accounts/${accountId}/sync`, { method: "POST" });
}
