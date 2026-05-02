"use client";

import useSWR from "swr";
import {
  fetchCashFlowMonthly,
  fetchCashFlowByCategory,
  fetchCashFlowTimeseries,
  fetchPortfolioSummary,
  fetchPortfolioBreakdown,
  fetchHoldings,
  fetchBalances,
} from "@/lib/api";

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
