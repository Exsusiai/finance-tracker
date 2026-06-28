"use client";

import { useState, useMemo, useCallback } from "react";
import { useCashFlowMonthly, useCashFlowByCategory, useCategories } from "@/lib/hooks";
import type { CashFlowByCategory } from "@/lib/api";
import { downloadCsv, downloadBlob } from "@/lib/api";
import {
  getTimeRangeDates,
  currentPeriod,
  shiftPeriod,
  recentPeriods,
  type TimeRange,
} from "@/lib/time-range";
import { cn, formatCurrency, formatPercent, periodLabel } from "@/lib/utils";
import {
  AssetTrendChart,
  MonthlyBarChart,
  SavingsRateChart,
  ExpensePieChart,
} from "@/components/charts";
import { TimeRangeSelector, TabSelector, ExportButton, LoadingSpinner, ErrorDisplay } from "@/components/ui-common";
import { PageHeader } from "@/components/ui-kit";

const TIME_RANGE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "1m", label: "1个月" },
  { value: "3m", label: "3个月" },
  { value: "6m", label: "6个月" },
  { value: "1y", label: "1年" },
  { value: "all", label: "全部" },
];

const CATEGORY_KIND_OPTIONS = [
  { value: "expense", label: "支出" },
  { value: "income", label: "收入" },
];

export default function AnalyticsPage() {
  const [timeRange, setTimeRange] = useState<TimeRange>("6m");
  const [categoryKind, setCategoryKind] = useState<"expense" | "income">("expense");

  const { from, to } = useMemo(() => getTimeRangeDates(timeRange), [timeRange]);

  // ─── Category-distribution scope (independent of the charts' time range) ──
  // The user can pin it to a single month or aggregate a month range.
  const [catMode, setCatMode] = useState<"single" | "range">("single");
  const [catMonth, setCatMonth] = useState<string>(() => currentPeriod());
  const [catFrom, setCatFrom] = useState<string>(() => shiftPeriod(currentPeriod(), -2));
  const [catTo, setCatTo] = useState<string>(() => currentPeriod());

  // ─── Data fetching ────────────────────────────────────────────────────
  const {
    data: monthlyData,
    error: monthlyError,
    isLoading: monthlyLoading,
    mutate: refreshMonthly,
  } = useCashFlowMonthly(from, to);

  const {
    data: categoryData,
    error: categoryError,
    isLoading: categoryLoading,
  } = useCashFlowByCategory(
    catMode === "single" ? catMonth : null,
    catMode === "range" ? catFrom : undefined,
    catMode === "range" ? catTo : undefined,
  );

  const { data: categories } = useCategories();

  // Roll leaf categories up to their top-level (大类) so the pie shows ~9
  // big buckets instead of every 二级 category — the flat list made the
  // chart unreadable. Uncategorized / unknown ids stay as their own bucket.
  const topLevelCategoryData = useMemo<CashFlowByCategory[]>(() => {
    if (!categoryData) return [];
    if (!categories) return categoryData;
    const byId = new Map(categories.map((c) => [c.id, c]));
    const topLevelOf = (id: number) => {
      let cur = byId.get(id);
      if (!cur) return null;
      // Strictly 2-level today, but walk up defensively.
      while (cur.parent_id != null && byId.has(cur.parent_id)) {
        cur = byId.get(cur.parent_id)!;
      }
      return cur;
    };
    const agg = new Map<string, CashFlowByCategory>();
    for (const row of categoryData) {
      const top = row.category_id != null ? topLevelOf(row.category_id) : null;
      const key = top ? `c${top.id}` : `n:${row.category_name}`;
      const prev = agg.get(key);
      if (prev) {
        prev.total = String(parseFloat(prev.total) + Math.abs(parseFloat(row.total)));
        prev.count += row.count;
      } else {
        agg.set(key, {
          category_id: top?.id ?? row.category_id,
          category_name: top?.name ?? row.category_name,
          kind: top?.kind ?? row.kind,
          total: String(Math.abs(parseFloat(row.total))),
          count: row.count,
        });
      }
    }
    return Array.from(agg.values());
  }, [categoryData, categories]);

  // ─── Export CSV ───────────────────────────────────────────────────────
  const handleExportCsv = useCallback(() => {
    if (!monthlyData || monthlyData.length === 0) return;
    const rows = monthlyData.map((d) => ({
      月份: periodLabel(d.period),
      收入: d.income,
      支出: d.expense,
      转账: d.transfer,
      净储蓄: d.savings,
    }));
    downloadCsv(rows, `现金流_${from}_${to}.csv`);
  }, [monthlyData, from, to]);

  // ─── Export PNG (all charts) ──────────────────────────────────────────
  const handleExportPng = useCallback(() => {
    const svgs = document.querySelectorAll("#analytics-page svg.recharts-surface");
    if (svgs.length === 0) return;

    svgs.forEach((svg, i) => {
      const svgData = new XMLSerializer().serializeToString(svg);
      const svgBlob = new Blob([svgData], { type: "image/svg+xml;charset=utf-8" });
      const url = URL.createObjectURL(svgBlob);

      const img = new Image();
      img.onload = () => {
        const canvas = document.createElement("canvas");
        const scale = 2;
        canvas.width = img.width * scale;
        canvas.height = img.height * scale;
        const ctx = canvas.getContext("2d");
        if (!ctx) return;
        ctx.scale(scale, scale);
        ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--canvas-bg").trim() || "#ffffff";
        ctx.fillRect(0, 0, img.width, img.height);
        ctx.drawImage(img, 0, 0);
        canvas.toBlob((blob) => {
          if (blob) downloadBlob(blob, `图表_${i + 1}.png`);
        }, "image/png");
        URL.revokeObjectURL(url);
      };
      img.src = url;
    });
  }, []);

  // ─── Derived: aggregate over selected time range ──────────────────────
  const rangeTotals = useMemo(() => {
    if (!monthlyData || monthlyData.length === 0) {
      return { income: 0, expense: 0, savings: 0, savingsRate: 0 };
    }
    const income = monthlyData.reduce((s, d) => s + parseFloat(d.income), 0);
    const expense = monthlyData.reduce((s, d) => s + parseFloat(d.expense), 0);
    const savings = monthlyData.reduce((s, d) => s + parseFloat(d.savings), 0);
    const savingsRate = income > 0 ? ((income - expense) / income) * 100 : 0;
    return { income, expense, savings, savingsRate };
  }, [monthlyData]);

  const latestMonth = monthlyData?.[0];

  // The category section has its own period control + loading state, so it is
  // NOT folded into the page-level gate (otherwise changing its scope would
  // blank out every chart above it).
  const hasAnyError = monthlyError;
  const hasAnyLoading = monthlyLoading;

  return (
    <div id="analytics-page" className="min-h-screen bg-background text-foreground">
      <div className="mx-auto max-w-7xl px-4 py-6 md:px-6 lg:px-8">
        {/* ─── Header ──────────────────────────────────────────────── */}
        <PageHeader
          title="现金流分析"
          subtitle={latestMonth ? `最近数据：${periodLabel(latestMonth.period)}` : "加载中…"}
          actions={
            <>
              <TimeRangeSelector
                value={timeRange}
                onChange={(v) => setTimeRange(v as TimeRange)}
                options={TIME_RANGE_OPTIONS}
              />
              <ExportButton onExportCsv={handleExportCsv} onExportPng={handleExportPng} />
            </>
          }
        />

        {/* ─── Summary Cards (time-range scoped) ──────────────────── */}
        <div className="grid grid-cols-3 gap-3 md:gap-4 mb-6">
          <SummaryCard
            label={`${TIME_RANGE_OPTIONS.find(o => o.value === timeRange)?.label || ""}收入`}
            value={rangeTotals.income > 0 ? formatCurrency(rangeTotals.income) : "—"}
            loading={monthlyLoading}
            color="text-green-500"
          />
          <SummaryCard
            label={`${TIME_RANGE_OPTIONS.find(o => o.value === timeRange)?.label || ""}支出`}
            value={rangeTotals.expense > 0 ? formatCurrency(rangeTotals.expense) : "—"}
            loading={monthlyLoading}
            color="text-red-500"
          />
          <SummaryCard
            label="平均储蓄率"
            value={monthlyData && monthlyData.length > 0 ? formatPercent(rangeTotals.savingsRate) : "—"}
            loading={monthlyLoading}
            color={rangeTotals.savingsRate >= 0 ? "text-green-500" : "text-red-500"}
          />
        </div>

        {/* ─── Error state ─────────────────────────────────────────── */}
        {hasAnyError && !hasAnyLoading && (
          <ErrorDisplay
            message={monthlyError?.message || "加载失败"}
            onRetry={() => refreshMonthly()}
          />
        )}

        {/* ─── Loading state ───────────────────────────────────────── */}
        {hasAnyLoading && !hasAnyError && (
          <LoadingSpinner />
        )}

        {/* ─── Charts ──────────────────────────────────────────────── */}
        {!hasAnyLoading && !hasAnyError && (
          <div className="space-y-6">
            {/* Row 1: Asset Trend */}
            <AssetTrendChart data={monthlyData || []} range={timeRange} />

            {/* Row 2: Monthly Bar + Savings Rate */}
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
              <MonthlyBarChart data={monthlyData || []} />
              <SavingsRateChart data={monthlyData || []} />
            </div>

            {/* Row 3: Expense/Income Category Pie — its own period scope */}
            <div>
              <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                <TabSelector
                  value={categoryKind}
                  onChange={(v) => setCategoryKind(v as "expense" | "income")}
                  options={CATEGORY_KIND_OPTIONS}
                />
                <CategoryPeriodControl
                  mode={catMode}
                  month={catMonth}
                  from={catFrom}
                  to={catTo}
                  onModeChange={setCatMode}
                  onMonthChange={setCatMonth}
                  onFromChange={setCatFrom}
                  onToChange={setCatTo}
                />
              </div>
              {categoryLoading ? (
                <LoadingSpinner />
              ) : categoryError ? (
                <ErrorDisplay message={categoryError.message || "加载分类数据失败"} />
              ) : (
                <ExpensePieChart categories={topLevelCategoryData} selectedKind={categoryKind} />
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Category-distribution period control ──────────────────────────────

interface CategoryPeriodControlProps {
  mode: "single" | "range";
  month: string;
  from: string;
  to: string;
  onModeChange: (m: "single" | "range") => void;
  onMonthChange: (p: string) => void;
  onFromChange: (p: string) => void;
  onToChange: (p: string) => void;
}

function CategoryPeriodControl({
  mode,
  month,
  from,
  to,
  onModeChange,
  onMonthChange,
  onFromChange,
  onToChange,
}: CategoryPeriodControlProps) {
  const months = useMemo(() => recentPeriods(24), []);
  const selectCls =
    "px-2.5 py-1.5 text-sm rounded-md border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring tabular-nums";
  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="inline-flex rounded-lg border border-border bg-card p-1">
        {([
          { value: "single", label: "单月" },
          { value: "range", label: "区间汇总" },
        ] as const).map((m) => (
          <button
            key={m.value}
            onClick={() => onModeChange(m.value)}
            className={cn(
              "px-2.5 py-1 text-xs font-medium rounded-md transition-colors",
              mode === m.value
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {m.label}
          </button>
        ))}
      </div>
      {mode === "single" ? (
        <select value={month} onChange={(e) => onMonthChange(e.target.value)} className={selectCls}>
          {months.map((m) => (
            <option key={m} value={m}>
              {periodLabel(m)}
            </option>
          ))}
        </select>
      ) : (
        <div className="flex items-center gap-1.5">
          <select value={from} onChange={(e) => onFromChange(e.target.value)} className={selectCls}>
            {months.map((m) => (
              <option key={m} value={m}>
                {periodLabel(m)}
              </option>
            ))}
          </select>
          <span className="text-muted-foreground text-sm">→</span>
          <select value={to} onChange={(e) => onToChange(e.target.value)} className={selectCls}>
            {months.map((m) => (
              <option key={m} value={m}>
                {periodLabel(m)}
              </option>
            ))}
          </select>
        </div>
      )}
    </div>
  );
}

// ─── Summary Card ──────────────────────────────────────────────────────

function SummaryCard({
  label,
  value,
  loading,
  color,
}: {
  label: string;
  value: string;
  loading?: boolean;
  color?: string;
}) {
  return (
    <div className="rounded-2xl border border-border bg-card p-5 shadow-xs">
      <p className="text-sm text-muted-foreground">{label}</p>
      {loading ? (
        <span className="skeleton mt-3 block h-7 w-24" />
      ) : (
        <p className={`mt-2 text-2xl font-semibold tabular-nums tracking-tight ${color || "text-foreground"}`}>
          {value}
        </p>
      )}
    </div>
  );
}
