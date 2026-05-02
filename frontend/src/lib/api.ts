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

export async function fetchPortfolioBreakdown(): Promise<PortfolioBreakdown> {
  return request("/api/v1/holdings/portfolio/breakdown");
}

export async function fetchHoldings(): Promise<HoldingOut[]> {
  return request("/api/v1/holdings");
}

export async function fetchBalances(): Promise<BalanceOut[]> {
  return request("/api/v1/accounts/balances");
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
