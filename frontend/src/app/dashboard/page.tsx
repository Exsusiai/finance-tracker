"use client";

import { useMemo } from "react";
import Link from "next/link";
import {
  AreaChart,
  Area,
  PieChart,
  Pie,
  Cell,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import {
  useBalances,
  usePortfolioBreakdown,
  useCashFlowMonthly,
  useTransactions,
  useCashFlowTimeseries,
} from "@/lib/hooks";
import {
  formatCurrency,
  formatDate,
  periodLabel,
  CHART_COLORS,
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
  const period = currentYearMonth();

  // ─── Data fetching ────────────────────────────────────────────────────
  const { data: balances, error: balancesError, isLoading: balancesLoading, mutate: refreshBalances } = useBalances();
  const { data: breakdown, error: breakdownError, isLoading: breakdownLoading, mutate: refreshBreakdown } = usePortfolioBreakdown();
  const { data: monthlyData, error: monthlyError, isLoading: monthlyLoading, mutate: refreshMonthly } = useCashFlowMonthly(period);
  const { data: txResp, error: txError, isLoading: txLoading, mutate: refreshTx } = useTransactions({ limit: 5 });
  const { data: timeseries, error: tsError, isLoading: tsLoading, mutate: refreshTs } = useCashFlowTimeseries();

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

  // ─── Derived: Pie chart data ──────────────────────────────────────────
  const pieData = useMemo(() => {
    if (!breakdown?.by_class) return [];
    const entries = Object.entries(breakdown.by_class);
    const total = entries.reduce((s, [, v]) => s + parseFloat(v.value || "0"), 0);
    return entries.map(([key, val], i) => ({
      name: ASSET_CLASS_LABELS[key] || key,
      value: parseFloat(val.value || "0"),
      fill: ASSET_CLASS_COLORS[key] || CHART_COLORS[i % CHART_COLORS.length],
      percent: total > 0 ? (parseFloat(val.value || "0") / total) * 100 : 0,
    }));
  }, [breakdown]);

  // ─── Derived: Timeseries chart data ───────────────────────────────────
  const trendData = useMemo(() => {
    if (!timeseries) return [];
    return timeseries.periods.map((p, i) => ({
      period: periodLabel(p),
      收入: parseFloat(timeseries.income[i] || "0"),
      支出: Math.abs(parseFloat(timeseries.expense[i] || "0")),
    }));
  }, [timeseries]);

  // ─── Error / Loading ──────────────────────────────────────────────────
  const hasError = balancesError || breakdownError || monthlyError || txError || tsError;
  const hasLoading = balancesLoading || breakdownLoading || monthlyLoading || txLoading || tsLoading;
  const refreshAll = () => { refreshBalances(); refreshBreakdown(); refreshMonthly(); refreshTx(); refreshTs(); };

  if (hasLoading) return <LoadingSpinner className="min-h-[60vh]" />;
  if (hasError) {
    return (
      <ErrorDisplay
        message={balancesError?.message || breakdownError?.message || monthlyError?.message || txError?.message || tsError?.message || "加载失败"}
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

        {/* ─── Row 1: Hero Card + Asset Distribution ──────────────── */}
        <div className="grid grid-cols-1 lg:grid-cols-5 gap-4 md:gap-6 mb-6">
          {/* Total Assets Hero */}
          <div className="lg:col-span-3 rounded-xl border border-border bg-card p-6">
            <p className="text-sm text-muted-foreground mb-1">总资产</p>
            <p className="text-3xl md:text-4xl font-bold text-card-foreground mb-4">
              {grandTotal > 0 ? formatCurrency(grandTotal) : "—"}
            </p>
            <div className="flex flex-wrap gap-3">
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
          </div>

          {/* Asset Distribution Pie */}
          <div className="lg:col-span-2 rounded-xl border border-border bg-card p-6">
            <h3 className="text-base font-semibold text-card-foreground mb-3">资产分布</h3>
            {pieData.length > 0 ? (
              <div className="flex flex-col items-center gap-4">
                <ResponsiveContainer width="100%" height={200}>
                  <PieChart>
                    <Pie
                      data={pieData}
                      cx="50%"
                      cy="50%"
                      innerRadius={50}
                      outerRadius={85}
                      paddingAngle={2}
                      dataKey="value"
                      stroke="none"
                    >
                      {pieData.map((entry, i) => (
                        <Cell key={i} fill={entry.fill} />
                      ))}
                    </Pie>
                    <Tooltip
                      content={({ active, payload }) => {
                        if (!active || !payload?.length) return null;
                        const d = payload[0];
                        return (
                          <div className="rounded-lg border border-border bg-card px-3 py-2 shadow-lg text-sm">
                            <span className="font-medium text-foreground">{d.name}</span>
                            <span className="text-muted-foreground ml-2">
                              {formatCurrency(Number(d.value ?? 0))} ({d.payload.percent?.toFixed(1) ?? "0"}%)
                            </span>
                          </div>
                        );
                      }}
                    />
                  </PieChart>
                </ResponsiveContainer>
                <div className="flex flex-wrap justify-center gap-x-4 gap-y-1 text-xs">
                  {pieData.map((d) => (
                    <div key={d.name} className="flex items-center gap-1.5">
                      <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: d.fill }} />
                      <span className="text-muted-foreground">{d.name}</span>
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div className="flex items-center justify-center h-[200px] text-sm text-muted-foreground">
                暂无持仓数据
              </div>
            )}
          </div>
        </div>

        {/* ─── Row 2: Monthly Summary Cards ───────────────────────── */}
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

        {/* ─── Row 3: Trend Chart + Recent Transactions ───────────── */}
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4 md:gap-6">
          {/* Trend Chart */}
          <div className="rounded-xl border border-border bg-card p-4 md:p-6">
            <h3 className="text-base font-semibold text-card-foreground mb-4">收支趋势</h3>
            {trendData.length > 0 ? (
              <ResponsiveContainer width="100%" height={240}>
                <AreaChart data={trendData} margin={{ top: 5, right: 10, left: 0, bottom: 5 }}>
                  <defs>
                    <linearGradient id="dashGradIncome" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="hsl(160, 60%, 45%)" stopOpacity={0.3} />
                      <stop offset="95%" stopColor="hsl(160, 60%, 45%)" stopOpacity={0} />
                    </linearGradient>
                    <linearGradient id="dashGradExpense" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="hsl(340, 70%, 55%)" stopOpacity={0.3} />
                      <stop offset="95%" stopColor="hsl(340, 70%, 55%)" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                  <XAxis dataKey="period" tick={{ fontSize: 12, fill: "hsl(var(--muted-foreground))" }} tickLine={false} axisLine={false} />
                  <YAxis tick={{ fontSize: 12, fill: "hsl(var(--muted-foreground))" }} tickLine={false} axisLine={false} tickFormatter={(v) => { if (Math.abs(v) >= 1000) return `${(v / 1000).toFixed(0)}K`; return v.toFixed(0); }} />
                  <Tooltip
                    content={({ active, payload, label }) => {
                      if (!active || !payload?.length) return null;
                      return (
                        <div className="rounded-lg border border-border bg-card px-3 py-2 shadow-lg text-sm">
                          <p className="text-muted-foreground mb-1">{label}</p>
                          {payload.map((entry, i) => (
                            <div key={i} className="flex items-center gap-2">
                              <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ backgroundColor: entry.color }} />
                              <span className="text-muted-foreground">{entry.name}:</span>
                              <span className="font-medium text-foreground">{formatCurrency(entry.value as number)}</span>
                            </div>
                          ))}
                        </div>
                      );
                    }}
                  />
                  <Area type="monotone" dataKey="收入" stroke="hsl(160, 60%, 45%)" fill="url(#dashGradIncome)" strokeWidth={2} />
                  <Area type="monotone" dataKey="支出" stroke="hsl(340, 70%, 55%)" fill="url(#dashGradExpense)" strokeWidth={2} />
                </AreaChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex items-center justify-center h-[200px] text-sm text-muted-foreground">
                暂无趋势数据
              </div>
            )}
          </div>

          {/* Recent Transactions */}
          <div className="rounded-xl border border-border bg-card p-4 md:p-6">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-base font-semibold text-card-foreground">最近交易</h3>
              <Link href="/transactions" className="text-xs text-muted-foreground hover:text-foreground transition-colors">
                查看全部 →
              </Link>
            </div>
            {txResp && txResp.data.length > 0 ? (
              <div className="space-y-3">
                {txResp.data.map((tx) => (
                  <div
                    key={tx.id}
                    className="flex items-center justify-between gap-3 py-2 border-b border-border last:border-0"
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
    </div>
  );
}
