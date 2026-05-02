"use client";

import { useState, useMemo, useCallback } from "react";
import { useCashFlowMonthly, useCashFlowByCategory, usePortfolioBreakdown, usePortfolioSummary } from "@/lib/hooks";
import { downloadCsv, downloadBlob } from "@/lib/api";
import { getTimeRangeDates, type TimeRange } from "@/lib/time-range";
import { formatCurrency, formatPercent, periodLabel } from "@/lib/utils";
import {
  AssetTrendChart,
  AssetDistributionChart,
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

const DISTRIBUTION_MODE_OPTIONS = [
  { value: "class", label: "按类型" },
  { value: "currency", label: "按币种" },
];

const CATEGORY_KIND_OPTIONS = [
  { value: "expense", label: "支出" },
  { value: "income", label: "收入" },
];

export default function AnalyticsPage() {
  const [timeRange, setTimeRange] = useState<TimeRange>("6m");
  const [distMode, setDistMode] = useState<"class" | "currency">("class");
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

  const {
    data: breakdown,
    error: breakdownError,
    isLoading: breakdownLoading,
  } = usePortfolioBreakdown();

  const {
    data: summary,
    error: summaryError,
    isLoading: summaryLoading,
  } = usePortfolioSummary();

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
    // Use html2canvas-like approach via SVG serialization
    // For now, export individual SVGs
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

  // ─── Derived values ───────────────────────────────────────────────────
  const latestMonth = monthlyData?.[0];
  const totalIncome = latestMonth ? parseFloat(latestMonth.income) : 0;
  const totalExpense = latestMonth ? parseFloat(latestMonth.expense) : 0;
  const latestSavingsRate = totalIncome > 0 ? ((totalIncome - totalExpense) / totalIncome) * 100 : 0;
  const portfolioTotal = summary ? parseFloat(summary.total_value) : 0;

  const hasAnyError = monthlyError || categoryError || breakdownError || summaryError;
  const hasAnyLoading = monthlyLoading || categoryLoading || breakdownLoading || summaryLoading;

  return (
    <div id="analytics-page" className="min-h-screen bg-background text-foreground">
      <div className="mx-auto max-w-7xl px-4 py-6 md:px-6 lg:px-8">
        {/* ─── Header ──────────────────────────────────────────────── */}
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 mb-6">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">📊 资产分析 & 现金流</h1>
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

        {/* ─── Summary Cards ───────────────────────────────────────── */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 md:gap-4 mb-6">
          <SummaryCard
            label="投资组合总值"
            value={portfolioTotal > 0 ? formatCurrency(portfolioTotal, summary?.base_currency) : "—"}
            loading={summaryLoading}
          />
          <SummaryCard
            label="本月收入"
            value={totalIncome > 0 ? formatCurrency(totalIncome) : "—"}
            loading={monthlyLoading}
          />
          <SummaryCard
            label="本月支出"
            value={totalExpense > 0 ? formatCurrency(totalExpense) : "—"}
            loading={monthlyLoading}
          />
          <SummaryCard
            label="储蓄率"
            value={monthlyData && monthlyData.length > 0 ? formatPercent(latestSavingsRate) : "—"}
            loading={monthlyLoading}
            color={latestSavingsRate >= 0 ? "text-green-500" : "text-red-500"}
          />
        </div>

        {/* ─── Error state ─────────────────────────────────────────── */}
        {hasAnyError && !hasAnyLoading && (
          <ErrorDisplay
            message={monthlyError?.message || categoryError?.message || breakdownError?.message || summaryError?.message || "加载失败"}
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
            {/* Row 1: Asset Trend + Asset Distribution */}
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
              <AssetTrendChart data={monthlyData || []} range={timeRange} />
              <div>
                <div className="mb-4">
                  <TabSelector
                    value={distMode}
                    onChange={(v) => setDistMode(v as "class" | "currency")}
                    options={DISTRIBUTION_MODE_OPTIONS}
                  />
                </div>
                <AssetDistributionChart
                  byClass={breakdown?.by_class || {}}
                  byCurrency={breakdown?.by_currency || {}}
                  mode={distMode}
                />
              </div>
            </div>

            {/* Row 2: Monthly Bar + Savings Rate */}
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
              <MonthlyBarChart data={monthlyData || []} />
              <SavingsRateChart data={monthlyData || []} />
            </div>

            {/* Row 3: Expense Category Pie */}
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
