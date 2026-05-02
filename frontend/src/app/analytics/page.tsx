"use client";

import { useState, useMemo, useCallback } from "react";
import { useCashFlowMonthly, useCashFlowByCategory } from "@/lib/hooks";
import { downloadCsv, downloadBlob } from "@/lib/api";
import { getTimeRangeDates, type TimeRange } from "@/lib/time-range";
import { formatCurrency, formatPercent, periodLabel } from "@/lib/utils";
import {
  AssetTrendChart,
  MonthlyBarChart,
  SavingsRateChart,
  ExpensePieChart,
} from "@/components/charts";
import { TimeRangeSelector, TabSelector, ExportButton, LoadingSpinner, ErrorDisplay } from "@/components/ui-common";

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

  // Current period for category drill-down (latest month)
  const currentPeriod = to;

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
  } = useCashFlowByCategory(currentPeriod);

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
        ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--card").trim() || "#ffffff";
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

  const hasAnyError = monthlyError || categoryError;
  const hasAnyLoading = monthlyLoading || categoryLoading;

  return (
    <div id="analytics-page" className="min-h-screen bg-background text-foreground">
      <div className="mx-auto max-w-7xl px-4 py-6 md:px-6 lg:px-8">
        {/* ─── Header ──────────────────────────────────────────────── */}
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 mb-6">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">📊 现金流分析</h1>
            <p className="text-sm text-muted-foreground mt-1">
              {latestMonth ? `最近数据：${periodLabel(latestMonth.period)}` : "加载中…"}
            </p>
          </div>
          <div className="flex items-center gap-3">
            <TimeRangeSelector
              value={timeRange}
              onChange={(v) => setTimeRange(v as TimeRange)}
              options={TIME_RANGE_OPTIONS}
            />
            <ExportButton onExportCsv={handleExportCsv} onExportPng={handleExportPng} />
          </div>
        </div>

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
            message={monthlyError?.message || categoryError?.message || "加载失败"}
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

            {/* Row 3: Expense/Income Category Pie */}
            <div>
              <div className="mb-4">
                <TabSelector
                  value={categoryKind}
                  onChange={(v) => setCategoryKind(v as "expense" | "income")}
                  options={CATEGORY_KIND_OPTIONS}
                />
              </div>
              <ExpensePieChart categories={categoryData || []} selectedKind={categoryKind} />
            </div>
          </div>
        )}
      </div>
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
    <div className="rounded-xl border border-border bg-card p-4">
      <p className="text-xs text-muted-foreground mb-1">{label}</p>
      <p className={`text-lg md:text-xl font-bold ${color || "text-card-foreground"}`}>
        {loading ? (
          <span className="inline-block h-5 w-16 animate-pulse rounded bg-muted" />
        ) : (
          value
        )}
      </p>
    </div>
  );
}
