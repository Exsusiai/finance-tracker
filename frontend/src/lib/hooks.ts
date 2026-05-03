"use client";

import useSWR from "swr";
import useSWRInfinite from "swr/infinite";
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

// ─── PDF Statements ────────────────────────────────────────────────────

export function useStatements(limit?: number, status?: string) {
  return useSWR(
    `statements-${limit || "all"}-${status || "all"}`,
    () => fetchStatements(limit, status),
    { revalidateOnFocus: false },
  );
}
