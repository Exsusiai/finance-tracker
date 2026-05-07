"use client";

import useSWR, { mutate as globalMutate } from "swr";
import useSWRInfinite from "swr/infinite";

// 2026-05-06: cross-page SWR invalidation. Whenever a write operation
// mutates the transaction graph (PDF upload, inbox confirm, mark-transfer,
// PATCH transaction, account subaccount edit, etc.) call this to force
// every dependent SWR cache (transactions list / inbox / cashflow /
// balances / net worth / statements / transfer-suggestions) to revalidate.
// Without this the user has to manually hard-refresh between pages
// because each tab keeps its stale cache.
const _TX_GRAPH_KEY_PREFIXES = [
  "transactions",       // useTransactions(...)
  "transaction-",       // useTransaction(id)
  "inbox",              // useInbox()
  "statements",         // useStatements()
  "balances",           // useBalances()
  "cashflow",           // useCashFlowMonthly / by-category / timeseries
  "transfer-suggestions",
  "transfer-unpaired",
  "accounts",           // re-balanced after delete / status changes
];
const _TX_GRAPH_EXACT_KEYS = new Set([
  "net-worth",
  "portfolio-summary",
  "portfolio-breakdown",
]);

export function invalidateTransactionGraph(): Promise<unknown> {
  // Trigger background revalidation WITHOUT clearing the cached data.
  // The previous form passed `undefined` as the new data, which wiped the
  // cache → consumers' `isLoading` flipped back to `true` for an instant
  // → the entire panel re-rendered as a `<LoadingSpinner />`, losing the
  // user's scroll position after every confirm click. Passing a function
  // that returns the current data unchanged keeps the cache populated
  // while SWR fetches fresh data in the background.
  return globalMutate(
    (key) => {
      if (typeof key !== "string") return false;
      if (_TX_GRAPH_EXACT_KEYS.has(key)) return true;
      return _TX_GRAPH_KEY_PREFIXES.some((p) => key.startsWith(p));
    },
    (current: unknown) => current,
    { revalidate: true, populateCache: true },
  );
}
import {
  fetchCashFlowMonthly,
  fetchCashFlowByCategory,
  fetchCashFlowTimeseries,
  fetchPortfolioSummary,
  fetchPortfolioBreakdown,
  fetchHoldings,
  fetchBalances,
  fetchNetWorth,
  fetchTransactions,
  fetchTransaction,
  fetchCategories,
  fetchAccounts,
  fetchStatements,
  fetchAssets,
  fetchFxRates,
  fetchInbox,
  fetchTransferSuggestions,
  fetchUnpairedTransfers,
} from "@/lib/api";
import type { TransactionFilters, TransactionOut } from "@/lib/api";

// Generic fetcher for SWR
async function swrFetcher<T>(fn: () => Promise<T>): Promise<T> {
  return fn();
}

export function useCashFlowMonthly(from?: string, to?: string) {
  return useSWR(
    from || to ? `cashflow-monthly-${from}-${to}` : "cashflow-monthly",
    () => fetchCashFlowMonthly(from, to),
    { revalidateOnFocus: false },
  );
}

export function useCashFlowByCategory(period: string | null) {
  return useSWR(
    period ? `cashflow-category-${period}` : null,
    () => fetchCashFlowByCategory(period!),
    { revalidateOnFocus: false },
  );
}

export function useCashFlowTimeseries(from?: string, to?: string) {
  return useSWR(
    from || to ? `cashflow-ts-${from}-${to}` : "cashflow-ts",
    () => fetchCashFlowTimeseries(from, to),
    { revalidateOnFocus: false },
  );
}

export function usePortfolioSummary() {
  return useSWR("portfolio-summary", () => fetchPortfolioSummary(), {
    revalidateOnFocus: false,
  });
}

export function usePortfolioBreakdown() {
  return useSWR("portfolio-breakdown", () => fetchPortfolioBreakdown(), {
    revalidateOnFocus: false,
  });
}

export function useHoldings() {
  return useSWR("holdings", () => fetchHoldings(), {
    revalidateOnFocus: false,
  });
}

export function useBalances() {
  return useSWR("balances", () => fetchBalances(), {
    revalidateOnFocus: false,
  });
}

export function useNetWorth() {
  return useSWR("net-worth", () => fetchNetWorth(), {
    revalidateOnFocus: false,
  });
}

// ─── Transactions ──────────────────────────────────────────────────────

export function useTransactions(filters?: TransactionFilters) {
  const key = filters ? `transactions-${JSON.stringify(filters)}` : "transactions";

  return useSWR(key, () => fetchTransactions(filters), {
    revalidateOnFocus: false,
  });
}

export function useTransaction(id: number | null) {
  return useSWR(
    id ? `transaction-${id}` : null,
    () => fetchTransaction(id!),
    { revalidateOnFocus: false },
  );
}

// ─── Categories ────────────────────────────────────────────────────────

export function useCategories(kind?: string) {
  return useSWR(
    kind ? `categories-${kind}` : "categories",
    () => fetchCategories(kind),
    { revalidateOnFocus: false },
  );
}

// ─── Accounts ──────────────────────────────────────────────────────────

export function useAccounts(activeOnly?: boolean) {
  return useSWR(
    activeOnly ? "accounts-active" : "accounts",
    () => fetchAccounts(activeOnly),
    { revalidateOnFocus: false },
  );
}

// ─── Assets ────────────────────────────────────────────────────────────

export function useAssets(assetClass?: string) {
  return useSWR(
    assetClass ? `assets-${assetClass}` : "assets",
    () => fetchAssets(assetClass),
    { revalidateOnFocus: false },
  );
}

// ─── Inbox ─────────────────────────────────────────────────────────────

export function useInbox(limit: number = 100) {
  return useSWR(
    `inbox-${limit}`,
    () => fetchInbox(limit),
    { revalidateOnFocus: false },
  );
}

export function useTransferSuggestions() {
  return useSWR(
    "transfer-suggestions",
    () => fetchTransferSuggestions(),
    { revalidateOnFocus: false },
  );
}

export function useUnpairedTransfers() {
  return useSWR(
    "transfer-unpaired",
    () => fetchUnpairedTransfers(),
    { revalidateOnFocus: false },
  );
}

// ─── FX Rates ──────────────────────────────────────────────────────────

/**
 * Fetch latest FX rates with `base` as the source currency.
 * Returns up to `limit` recent snapshots; the consumer should pick the latest
 * per (base, quote) pair via `latestFxMap`.
 */
export function useFxRates(base: string = "CNY") {
  return useSWR(
    `fx-rates-${base}`,
    () => fetchFxRates(base, 300),
    { revalidateOnFocus: false, refreshInterval: 5 * 60 * 1000 },
  );
}

// ─── PDF Statements ────────────────────────────────────────────────────

export function useStatements(limit?: number, status?: string) {
  return useSWR(
    `statements-${limit || "all"}-${status || "all"}`,
    () => fetchStatements(limit, status),
    { revalidateOnFocus: false },
  );
}
