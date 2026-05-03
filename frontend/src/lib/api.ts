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
  income: string;
  expense: string;
  transfer: string;
  savings: string;
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
  type: string;
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
  category_id?: number;
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

export async function updateTransaction(
  id: number,
  data: TransactionUpdateInput
): Promise<TransactionOut> {
  return request(`/api/v1/transactions/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
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

// ─── Accounts ──────────────────────────────────────────────────────────

export interface AccountOut {
  id: number;
  name: string;
  type: string;
  institution: string | null;
  account_number: string | null;
  currency: string;
  initial_balance: string;
  is_active: boolean;
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
  currency: string;
  initial_balance?: string;
  notes?: string;
}

export interface AccountUpdateInput {
  name?: string;
  type?: string;
  institution?: string;
  account_number?: string;
  currency?: string;
  is_active?: boolean;
  notes?: string;
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
