"use client";

import { useMemo } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  useBalances,
  usePortfolioBreakdown,
  useCashFlowMonthly,
  useTransactions,
} from "@/lib/hooks";
import {
  formatCurrency,
  formatDate,
  periodLabel,
  ASSET_CLASS_LABELS,
  ASSET_CLASS_COLORS,
} from "@/lib/utils";
import { ErrorDisplay, LoadingSpinner } from "@/components/ui-common";

// ─── Current year-month ────────────────────────────────────────────────
function currentYearMonth(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

export default function DashboardPage() {
  const router = useRouter();
  const period = currentYearMonth();

  // ─── Data fetching (minimal set for fast load) ───────────────────────
  const { data: balances, error: balancesError, isLoading: balancesLoading, mutate: refreshBalances } = useBalances();
  const { data: breakdown, error: breakdownError, isLoading: breakdownLoading, mutate: refreshBreakdown } = usePortfolioBreakdown();
  const { data: monthlyData, error: monthlyError, isLoading: monthlyLoading, mutate: refreshMonthly } = useCashFlowMonthly(period);
  const { data: txResp, error: txError, isLoading: txLoading, mutate: refreshTx } = useTransactions({ limit: 8 });

  // ─── Derived: Total Assets by currency ────────────────────────────────
  const currencyTotals = useMemo(() => {
    if (!balances || balances.length === 0) return [];
    const map = new Map<string, number>();
    for (const b of balances) {
      map.set(b.currency, (map.get(b.currency) || 0) + parseFloat(b.balance || "0"));
    }
    return Array.from(map.entries()).map(([currency, total]) => ({ currency, total }));
  }, [balances]);

  const grandTotal = useMemo(
    () => currencyTotals.reduce((sum, c) => sum + c.total, 0),
    [currencyTotals],
  );

  // ─── Derived: This month income / expense / savings rate ──────────────
  const thisMonth = monthlyData?.[0];
  const monthIncome = thisMonth ? parseFloat(thisMonth.income) : 0;
  const monthExpense = thisMonth ? parseFloat(thisMonth.expense) : 0;
  const savingsRate = monthIncome > 0 ? ((monthIncome - monthExpense) / monthIncome) * 100 : 0;

  // ─── Derived: Asset class mini-cards ──────────────────────────────────
  const assetClassCards = useMemo(() => {
    if (!breakdown?.by_class) return [];
    const entries = Object.entries(breakdown.by_class);
    const total = entries.reduce((s, [, v]) => s + parseFloat(v.value || "0"), 0);
    if (total <= 0) return [];
    return entries
      .map(([key, val]) => ({
        key,
        label: ASSET_CLASS_LABELS[key] || key,
        value: parseFloat(val.value || "0"),
        percent: (parseFloat(val.value || "0") / total) * 100,
        color: ASSET_CLASS_COLORS[key] || "hsl(0, 0%, 50%)",
      }))
      .sort((a, b) => b.value - a.value);
  }, [breakdown]);

  // ─── Error / Loading ──────────────────────────────────────────────────
  const hasError = balancesError || breakdownError || monthlyError || txError;
  const hasLoading = balancesLoading || breakdownLoading || monthlyLoading || txLoading;
  const refreshAll = () => { refreshBalances(); refreshBreakdown(); refreshMonthly(); refreshTx(); };

  if (hasLoading) return <LoadingSpinner className="min-h-[60vh]" />;
  if (hasError) {
    return (
      <ErrorDisplay
        message={balancesError?.message || breakdownError?.message || monthlyError?.message || txError?.message || "加载失败"}
        onRetry={refreshAll}
      />
    );
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      <div className="mx-auto max-w-7xl px-4 py-6 md:px-6 lg:px-8">
        {/* ─── Header ──────────────────────────────────────────────── */}
        <div className="mb-6">
          <h1 className="text-2xl font-bold tracking-tight">总览</h1>
          <p className="text-sm text-muted-foreground mt-1">
            {period ? `数据截至 ${periodLabel(period)}` : "加载中…"}
          </p>
        </div>

        {/* ─── Total Assets Hero Card ──────────────────────────────── */}
        <div className="rounded-xl border border-border bg-card p-6 mb-6">
          <p className="text-sm text-muted-foreground mb-1">总资产</p>
          <p className="text-3xl md:text-4xl font-bold text-card-foreground mb-4">
            {grandTotal > 0 ? formatCurrency(grandTotal) : "—"}
          </p>
          <div className="flex flex-wrap gap-3 mb-4">
            {currencyTotals.map((c) => (
              <div
                key={c.currency}
                className="rounded-lg bg-muted/50 px-3 py-2 text-sm"
              >
                <span className="text-muted-foreground">{c.currency}</span>
                <span className="ml-2 font-semibold text-foreground">
                  {formatCurrency(c.total, c.currency)}
                </span>
              </div>
            ))}
            {currencyTotals.length === 0 && (
              <span className="text-sm text-muted-foreground">暂无账户数据</span>
            )}
          </div>

          {/* Asset class distribution mini-cards */}
          {assetClassCards.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {assetClassCards.map((ac) => (
                <div
                  key={ac.key}
                  className="flex items-center gap-2 rounded-lg border border-border bg-muted/30 px-3 py-1.5 text-sm"
                >
                  <span className="inline-block h-2.5 w-2.5 rounded-sm shrink-0" style={{ backgroundColor: ac.color }} />
                  <span className="text-muted-foreground">{ac.label}</span>
                  <span className="font-medium text-foreground">
                    {formatCurrency(ac.value)}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {ac.percent.toFixed(1)}%
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* ─── Monthly Summary Cards ───────────────────────────────── */}
        <div className="grid grid-cols-3 gap-3 md:gap-4 mb-6">
          {/* Income */}
          <div className="rounded-xl border border-border bg-card p-4">
            <div className="flex items-center gap-2 mb-1">
              <span className="inline-flex items-center justify-center h-7 w-7 rounded-lg bg-green-500/10">
                <svg className="h-4 w-4 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 10l7-7m0 0l7 7m-7-7v18" />
                </svg>
              </span>
              <span className="text-xs text-muted-foreground">本月收入</span>
            </div>
            <p className="text-lg md:text-xl font-bold text-green-500">
              {monthIncome > 0 ? formatCurrency(monthIncome) : "—"}
            </p>
          </div>

          {/* Expense */}
          <div className="rounded-xl border border-border bg-card p-4">
            <div className="flex items-center gap-2 mb-1">
              <span className="inline-flex items-center justify-center h-7 w-7 rounded-lg bg-red-500/10">
                <svg className="h-4 w-4 text-red-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 14l-7 7m0 0l-7-7m7 7V3" />
                </svg>
              </span>
              <span className="text-xs text-muted-foreground">本月支出</span>
            </div>
            <p className="text-lg md:text-xl font-bold text-red-500">
              {monthExpense > 0 ? formatCurrency(monthExpense) : "—"}
            </p>
          </div>

          {/* Savings Rate */}
          <div className="rounded-xl border border-border bg-card p-4">
            <div className="flex items-center gap-2 mb-1">
              <span className="inline-flex items-center justify-center h-7 w-7 rounded-lg bg-blue-500/10">
                <svg className="h-4 w-4 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" />
                </svg>
              </span>
              <span className="text-xs text-muted-foreground">储蓄率</span>
            </div>
            <p className="text-lg md:text-xl font-bold text-blue-500">
              {monthIncome > 0 ? `${savingsRate >= 0 ? "+" : ""}${savingsRate.toFixed(1)}%` : "—"}
            </p>
          </div>
        </div>

        {/* ─── Quick Actions ───────────────────────────────────────── */}
        <div className="grid grid-cols-3 gap-3 md:gap-4 mb-6">
          <button
            onClick={() => router.push("/transactions?action=add")}
            className="flex items-center justify-center gap-2 rounded-xl border border-border bg-card p-4 text-sm font-medium text-foreground hover:bg-muted/50 transition-colors cursor-pointer"
          >
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
            </svg>
            添加交易
          </button>
          <button
            onClick={() => router.push("/transactions?action=import")}
            className="flex items-center justify-center gap-2 rounded-xl border border-border bg-card p-4 text-sm font-medium text-foreground hover:bg-muted/50 transition-colors cursor-pointer"
          >
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
            </svg>
            导入PDF
          </button>
          <button
            onClick={() => router.push("/assets?action=add")}
            className="flex items-center justify-center gap-2 rounded-xl border border-border bg-card p-4 text-sm font-medium text-foreground hover:bg-muted/50 transition-colors cursor-pointer"
          >
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4" />
            </svg>
            添加资产
          </button>
        </div>

        {/* ─── Recent Transactions ────────────────────────────────── */}
        <div className="rounded-xl border border-border bg-card p-4 md:p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-base font-semibold text-card-foreground">最近交易</h3>
            <Link href="/transactions" className="text-xs text-muted-foreground hover:text-foreground transition-colors">
              查看全部 →
            </Link>
          </div>
          {txResp && txResp.data.length > 0 ? (
            <div className="space-y-1">
              {txResp.data.map((tx) => (
                <div
                  key={tx.id}
                  className="flex items-center justify-between gap-3 py-2.5 border-b border-border last:border-0"
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-foreground truncate">
                        {tx.description || tx.raw_description || "—"}
                      </span>
                      {tx.category_name && (
                        <span className="inline-flex shrink-0 items-center rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium text-muted-foreground">
                          {tx.category_name}
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      {formatDate(tx.occurred_at)}
                      {tx.account_name && ` · ${tx.account_name}`}
                    </p>
                  </div>
                  <span
                    className={`text-sm font-semibold whitespace-nowrap ${
                      tx.type === "income" ? "text-green-500" : "text-red-500"
                    }`}
                  >
                    {tx.type === "income" ? "+" : "-"}{formatCurrency(Math.abs(parseFloat(tx.amount)), tx.currency)}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div className="flex items-center justify-center h-[200px] text-sm text-muted-foreground">
              暂无交易记录
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
