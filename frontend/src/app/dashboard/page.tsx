"use client";

import { useEffect, useMemo, useState } from "react";
import {
  useBalances,
  usePortfolioBreakdown,
  useNetWorth,
  useFxRates,
  useCashFlowTimeseries,
  usePortfolioValueHistory,
} from "@/lib/hooks";
import {
  formatCurrency,
  formatDate,
  ASSET_CLASS_LABELS,
  ASSET_CLASS_COLORS,
  DISPLAY_CURRENCIES,
  cn,
  convertAmount,
  latestFxMap,
} from "@/lib/utils";
import { FinancialFlowChart, PortfolioValueChart } from "@/components/charts";
import { ErrorDisplay } from "@/components/ui-common";
import {
  Tile,
  Sparkline,
  DeltaBadge,
  AllocationBar,
  ProportionRow,
  useCountUp,
  type AllocSegment,
} from "@/components/dashboard-widgets";

export default function DashboardPage() {
  // ─── Data fetching ───────────────────────────────────────────────────
  const { data: balances, error: balancesError, isLoading: balancesLoading, mutate: refreshBalances } = useBalances();
  const { data: breakdown, error: breakdownError, isLoading: breakdownLoading, mutate: refreshBreakdown } = usePortfolioBreakdown();
  const { data: netWorth } = useNetWorth();
  const { data: fxRatesRaw } = useFxRates("CNY");
  const fxMap = useMemo(() => latestFxMap(fxRatesRaw), [fxRatesRaw]);
  // Book-keeping series are pinned to BASE_CURRENCY (no displayCurrency conversion).
  const { data: timeseries, isLoading: tsLoading } = useCashFlowTimeseries();
  const { data: valueHistory } = usePortfolioValueHistory();

  // ─── Display currency (shared with /assets via localStorage) ──────────
  const [displayCurrency, setDisplayCurrency] = useState<string>("CNY");
  useEffect(() => {
    const saved = typeof window !== "undefined" ? window.localStorage.getItem("display_currency") : null;
    if (saved) setDisplayCurrency(saved);
  }, []);
  const handleDisplayCurrencyChange = (c: string) => {
    setDisplayCurrency(c);
    if (typeof window !== "undefined") window.localStorage.setItem("display_currency", c);
  };
  const baseCurrency = netWorth?.base_currency ?? "CNY";

  // Convert a base-currency amount to the chosen display currency (with fallback).
  const toDisplay = useMemo(() => {
    return (rawBase: number): { v: number; c: string } => {
      if (displayCurrency === baseCurrency) return { v: rawBase, c: displayCurrency };
      const conv = convertAmount(rawBase, baseCurrency, displayCurrency, fxMap);
      return conv != null ? { v: conv, c: displayCurrency } : { v: rawBase, c: baseCurrency };
    };
  }, [displayCurrency, baseCurrency, fxMap]);

  // ─── Headline figures ─────────────────────────────────────────────────
  const nw = netWorth ? toDisplay(parseFloat(netWorth.net_worth)) : { v: 0, c: displayCurrency };
  const cash = netWorth ? toDisplay(parseFloat(netWorth.cash_total)) : { v: 0, c: displayCurrency };
  const invest = netWorth ? toDisplay(parseFloat(netWorth.investment_total)) : { v: 0, c: displayCurrency };
  const nwTotal = cash.v + invest.v;
  const cashPct = nwTotal > 0 ? (cash.v / nwTotal) * 100 : 0;
  const investPct = nwTotal > 0 ? (invest.v / nwTotal) * 100 : 0;

  const animatedNw = useCountUp(nw.v);

  // Cash-assets trend (monthly, base currency) → hero sparkline + MoM delta.
  const cashSeries = useMemo(
    () => (timeseries?.cash ?? []).map((s) => parseFloat(s)).filter((n) => isFinite(n)),
    [timeseries],
  );
  const cashDelta = useMemo(() => {
    if (cashSeries.length < 2) return null;
    const prev = cashSeries[cashSeries.length - 2];
    const last = cashSeries[cashSeries.length - 1];
    if (!isFinite(prev) || prev === 0) return null;
    return ((last - prev) / Math.abs(prev)) * 100;
  }, [cashSeries]);

  // ─── Derived: Total Assets by currency (raw, account own currency) ─────
  const currencyTotals = useMemo(() => {
    if (!balances || balances.length === 0) return [];
    const map = new Map<string, number>();
    for (const b of balances) {
      map.set(b.currency, (map.get(b.currency) || 0) + parseFloat(b.balance || "0"));
    }
    return Array.from(map.entries())
      .map(([currency, total]) => ({ currency, total }))
      .sort((a, b) => Math.abs(b.total) - Math.abs(a.total));
  }, [balances]);

  // ─── Derived: Asset-class allocation segments (display currency) ───────
  // Coarse by-class view (us_stock / crypto / fund / …) PLUS bank cash, with
  // the lone "现金" class split into two so it's unambiguous:
  //   • 银行现金 — bank-account cash (net of credit cards) = net_worth.cash_total
  //   • 券商闲置现金 — the by_class "cash" bucket (idle cash held in IBKR/TR/CEX)
  const allocation = useMemo<AllocSegment[]>(() => {
    const raw: Array<{ key: string; label: string; base: number; color: string }> = [];

    const bankCash = netWorth ? parseFloat(netWorth.cash_total) : 0;
    if (Math.abs(bankCash) > 0.005) {
      raw.push({ key: "bank_cash", label: "银行现金", base: bankCash, color: ASSET_CLASS_COLORS.cash });
    }
    for (const [key, val] of Object.entries(breakdown?.by_class ?? {})) {
      const base = parseFloat(val.value || "0");
      if (Math.abs(base) < 0.005) continue;
      const isIdle = key === "cash"; // asset_class='cash' holdings = brokerage idle cash
      raw.push({
        key,
        label: isIdle ? "券商闲置现金" : (ASSET_CLASS_LABELS[key] || key),
        base,
        color: isIdle ? "var(--cat-2)" : (ASSET_CLASS_COLORS[key] || "var(--chart-5)"),
      });
    }

    const totalRaw = raw.reduce((s, x) => s + x.base, 0);
    if (totalRaw <= 0) return [];
    return raw
      .map((x) => {
        const d = toDisplay(x.base);
        return { key: x.key, label: x.label, value: d.v, currency: d.c, percent: (x.base / totalRaw) * 100, color: x.color };
      })
      .sort((a, b) => b.value - a.value);
  }, [breakdown, netWorth, toDisplay]);

  // ─── Error / Loading ──────────────────────────────────────────────────
  const hasError = balancesError || breakdownError;
  const hasLoading = balancesLoading || breakdownLoading;
  const refreshAll = () => { refreshBalances(); refreshBreakdown(); };

  if (hasError) {
    return (
      <ErrorDisplay
        message={balancesError?.message || breakdownError?.message || "加载失败"}
        onRetry={refreshAll}
      />
    );
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      <div className="mx-auto max-w-7xl px-5 py-8 md:px-8 lg:py-10">
        {/* ─── Header ──────────────────────────────────────────────── */}
        <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-[1.75rem] font-semibold tracking-tight">总览</h1>
            <p className="mt-1 text-sm text-muted-foreground">财务状况一览</p>
          </div>
          <div className="inline-flex rounded-full border border-border bg-card p-0.5 shadow-xs">
            {DISPLAY_CURRENCIES.map((c) => (
              <button
                key={c.value}
                onClick={() => handleDisplayCurrencyChange(c.value)}
                className={cn(
                  "rounded-full px-3 py-1.5 text-xs font-medium transition-colors",
                  displayCurrency === c.value
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {c.label}
              </button>
            ))}
          </div>
        </header>

        {hasLoading ? (
          <DashboardSkeleton />
        ) : (
          <div className="space-y-4">
            {/* ─── Bento row: hero + split ──────────────────────────── */}
            <div className="grid gap-4 lg:grid-cols-3">
              {/* Hero — net worth */}
              <Tile className="relative overflow-hidden lg:col-span-2" delay={0}>
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <p className="text-sm text-muted-foreground">总资产净值</p>
                    <p className="mt-3 text-4xl font-semibold leading-none tracking-[-0.02em] tabular-nums md:text-6xl">
                      {netWorth ? formatCurrency(animatedNw, nw.c) : "—"}
                    </p>
                    <p className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted-foreground">
                      <span>现金 <span className="font-medium text-foreground tabular-nums">{formatCurrency(cash.v, cash.c)}</span></span>
                      <span className="text-border">·</span>
                      <span>投资 <span className="font-medium text-foreground tabular-nums">{formatCurrency(invest.v, invest.c)}</span></span>
                    </p>
                  </div>
                  {netWorth?.as_of && (
                    <p className="shrink-0 text-xs text-muted-foreground tabular-nums">
                      截至 {formatDate(netWorth.as_of)}
                    </p>
                  )}
                </div>

                {cashSeries.length >= 2 && (
                  <div className="mt-8">
                    <div className="mb-2 flex items-center justify-between">
                      <span className="text-xs text-muted-foreground">现金资产 · 近 {cashSeries.length} 月</span>
                      {cashDelta != null && <DeltaBadge percent={cashDelta} />}
                    </div>
                    <Sparkline data={cashSeries} gradientId="heroSpark" height={56} />
                  </div>
                )}
              </Tile>

              {/* Split — cash vs investment */}
              <Tile delay={70} interactive>
                <p className="text-sm text-muted-foreground">资金构成</p>
                <div className="mt-6 space-y-6">
                  <ProportionRow
                    label="现金资产"
                    amount={formatCurrency(cash.v, cash.c)}
                    percent={cashPct}
                    emphasis
                  />
                  <ProportionRow
                    label="投资市值"
                    amount={formatCurrency(invest.v, invest.c)}
                    percent={investPct}
                    emphasis
                  />
                </div>
                <div className="mt-6 flex items-center justify-between border-t border-border pt-4 text-sm">
                  <span className="text-muted-foreground">资产类别</span>
                  <span className="font-medium tabular-nums">{allocation.length} 类</span>
                </div>
              </Tile>
            </div>

            {/* ─── Allocation bar ───────────────────────────────────── */}
            {allocation.length > 0 && (
              <Tile delay={140} interactive>
                <div className="mb-6 flex items-baseline justify-between">
                  <p className="text-sm text-muted-foreground">资产分布</p>
                  <p className="text-sm font-medium tabular-nums">
                    {formatCurrency(allocation.reduce((s, a) => s + a.value, 0), allocation[0]?.currency ?? displayCurrency)}
                  </p>
                </div>
                <AllocationBar segments={allocation} formatValue={formatCurrency} />
              </Tile>
            )}

            {/* ─── Currency strip ───────────────────────────────────── */}
            {currencyTotals.length > 0 && (
              <Tile delay={200}>
                <p className="mb-4 text-sm text-muted-foreground">账户币种余额（原币）</p>
                <div className="grid grid-cols-2 gap-x-8 gap-y-4 sm:grid-cols-3 lg:grid-cols-4">
                  {currencyTotals.map((c) => (
                    <div key={c.currency} className="border-l border-border pl-3">
                      <div className="text-xs text-muted-foreground">{c.currency}</div>
                      <div className="mt-0.5 text-base font-medium tabular-nums">
                        {formatCurrency(c.total, c.currency)}
                      </div>
                    </div>
                  ))}
                </div>
              </Tile>
            )}

            {/* ─── Trend charts ─────────────────────────────────────── */}
            <div className="space-y-4 pt-2">
              {!tsLoading && (
                <FinancialFlowChart
                  periods={timeseries?.periods ?? []}
                  income={timeseries?.income ?? []}
                  expense={timeseries?.expense ?? []}
                  cash={timeseries?.cash ?? []}
                  currency={baseCurrency}
                />
              )}
              <PortfolioValueChart points={valueHistory ?? []} currency={baseCurrency} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Loading skeleton (matches the bento, no center spinner) ────────────

function DashboardSkeleton() {
  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2 rounded-2xl border border-border bg-card p-6">
          <div className="skeleton h-4 w-24" />
          <div className="skeleton mt-4 h-12 w-72" />
          <div className="skeleton mt-4 h-4 w-56" />
          <div className="skeleton mt-8 h-14 w-full" />
        </div>
        <div className="rounded-2xl border border-border bg-card p-6">
          <div className="skeleton h-4 w-20" />
          <div className="skeleton mt-6 h-10 w-full" />
          <div className="skeleton mt-5 h-10 w-full" />
        </div>
      </div>
      <div className="rounded-2xl border border-border bg-card p-6">
        <div className="skeleton h-4 w-24" />
        <div className="skeleton mt-6 h-3 w-full rounded-full" />
        <div className="mt-6 grid grid-cols-3 gap-4">
          {[0, 1, 2].map((i) => <div key={i} className="skeleton h-10" />)}
        </div>
      </div>
      <div className="skeleton h-80 w-full rounded-2xl" />
    </div>
  );
}
