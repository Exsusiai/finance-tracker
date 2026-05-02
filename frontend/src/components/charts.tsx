"use client";

import { useCallback, useRef } from "react";
import {
  AreaChart,
  Area,
  BarChart,
  Bar,
  PieChart,
  Pie,
  Cell,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
  Brush,
} from "recharts";
import { formatCurrency, formatPercent, periodLabel, CHART_COLORS, ASSET_CLASS_COLORS, ASSET_CLASS_LABELS } from "@/lib/utils";
import type { TimeRange } from "@/lib/time-range";

// ─── Shared Tooltip ────────────────────────────────────────────────────

function ChartTooltip({ active, payload, label, valuePrefix = "", valueSuffix = "" }: any) {
  if (!active || !payload?.length) return null;

  return (
    <div className="rounded-lg border border-border bg-card px-3 py-2 shadow-lg text-sm">
      <p className="text-muted-foreground mb-1">{label}</p>
      {payload.map((entry: any, i: number) => (
        <div key={i} className="flex items-center gap-2">
          <span
            className="inline-block h-2.5 w-2.5 rounded-full"
            style={{ backgroundColor: entry.color }}
          />
          <span className="text-muted-foreground">{entry.name}:</span>
          <span className="font-medium text-foreground">
            {valuePrefix}{formatNumber(entry.value)}{valueSuffix}
          </span>
        </div>
      ))}
    </div>
  );
}

function formatNumber(v: number): string {
  if (Math.abs(v) >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}M`;
  if (Math.abs(v) >= 1_000) return `${(v / 1_000).toFixed(1)}K`;
  return v.toFixed(2);
}

function PieTooltip({ active, payload }: any) {
  if (!active || !payload?.length) return null;
  const d = payload[0];
  return (
    <div className="rounded-lg border border-border bg-card px-3 py-2 shadow-lg text-sm">
      <div className="flex items-center gap-2 mb-1">
        <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ backgroundColor: d.payload.fill }} />
        <span className="font-medium text-foreground">{d.name}</span>
      </div>
      <p className="text-muted-foreground">
        {formatCurrency(d.value)} ({d.payload.percent?.toFixed(1) ?? "0"}%)
      </p>
    </div>
  );
}

// ─── Axis components ────────────────────────────────────────────────────

const axisStyle = {
  fontSize: 12,
  fill: "hsl(var(--muted-foreground))",
};

// ─── Asset Trend Chart (Area) ───────────────────────────────────────────

interface AssetTrendChartProps {
  data: Array<{ period: string; income: string; expense: string; savings: string }>;
  range: TimeRange;
}

export function AssetTrendChart({ data, range }: AssetTrendChartProps) {
  const chartData = [...data].reverse().map((d) => ({
    period: periodLabel(d.period),
    rawPeriod: d.period,
    收入: parseFloat(d.income) || 0,
    支出: parseFloat(d.expense) || 0,
    净储蓄: parseFloat(d.savings) || 0,
  }));

  if (chartData.length === 0) {
    return <EmptyChart message="暂无数据" />;
  }

  const showBrush = range === "1y" || range === "all";

  return (
    <Card title="资产变化趋势">
      <ResponsiveContainer width="100%" height={320}>
        <AreaChart data={chartData} margin={{ top: 5, right: 20, left: 10, bottom: showBrush ? 40 : 5 }}>
          <defs>
            <linearGradient id="gradIncome" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="hsl(160, 60%, 45%)" stopOpacity={0.3} />
              <stop offset="95%" stopColor="hsl(160, 60%, 45%)" stopOpacity={0} />
            </linearGradient>
            <linearGradient id="gradExpense" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="hsl(340, 70%, 55%)" stopOpacity={0.3} />
              <stop offset="95%" stopColor="hsl(340, 70%, 55%)" stopOpacity={0} />
            </linearGradient>
            <linearGradient id="gradSavings" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="hsl(220, 70%, 55%)" stopOpacity={0.3} />
              <stop offset="95%" stopColor="hsl(220, 70%, 55%)" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
          <XAxis dataKey="period" tick={axisStyle} tickLine={false} axisLine={false} />
          <YAxis tick={axisStyle} tickLine={false} axisLine={false} tickFormatter={(v) => formatNumber(v)} />
          <Tooltip content={<ChartTooltip />} />
          <Legend />
          <Area type="monotone" dataKey="收入" stroke="hsl(160, 60%, 45%)" fill="url(#gradIncome)" strokeWidth={2} />
          <Area type="monotone" dataKey="支出" stroke="hsl(340, 70%, 55%)" fill="url(#gradExpense)" strokeWidth={2} />
          <Area type="monotone" dataKey="净储蓄" stroke="hsl(220, 70%, 55%)" fill="url(#gradSavings)" strokeWidth={2} />
          {showBrush && (
            <Brush dataKey="period" height={30} stroke="hsl(var(--primary))" startIndex={0} endIndex={chartData.length - 1} />
          )}
        </AreaChart>
      </ResponsiveContainer>
    </Card>
  );
}

// ─── Asset Distribution (Pie) ──────────────────────────────────────────

interface AssetDistributionChartProps {
  byClass: Record<string, { value: string; count: number }>;
  byCurrency: Record<string, { value: string; count: number }>;
  mode: "class" | "currency";
}

export function AssetDistributionChart({ byClass, byCurrency, mode }: AssetDistributionChartProps) {
  const source = mode === "class" ? byClass : byCurrency;
  const entries = Object.entries(source);

  if (entries.length === 0) {
    return <EmptyChart message="暂无持仓数据" />;
  }

  const total = entries.reduce((sum, [, v]) => sum + parseFloat(v.value || "0"), 0);

  const pieData = entries.map(([key, val]) => ({
    name: mode === "class" ? (ASSET_CLASS_LABELS[key] || key) : key,
    value: parseFloat(val.value || "0"),
    fill: mode === "class" ? (ASSET_CLASS_COLORS[key] || CHART_COLORS[entries.indexOf(entries.find(e => e[0] === key)!) % CHART_COLORS.length]) : CHART_COLORS[entries.indexOf(entries.find(e => e[0] === key)!) % CHART_COLORS.length],
    percent: total > 0 ? (parseFloat(val.value || "0") / total) * 100 : 0,
  }));

  return (
    <Card title={mode === "class" ? "资产分布（按类型）" : "资产分布（按币种）"}>
      <div className="flex flex-col lg:flex-row items-center gap-6">
        <ResponsiveContainer width="100%" height={280} className="max-w-[320px]">
          <PieChart>
            <Pie
              data={pieData}
              cx="50%"
              cy="50%"
              innerRadius={60}
              outerRadius={100}
              paddingAngle={2}
              dataKey="value"
              stroke="none"
            >
              {pieData.map((entry, i) => (
                <Cell key={i} fill={entry.fill} />
              ))}
            </Pie>
            <Tooltip content={<PieTooltip />} />
          </PieChart>
        </ResponsiveContainer>
        <div className="flex flex-col gap-2 text-sm w-full lg:w-auto">
          {pieData.map((d) => (
            <div key={d.name} className="flex items-center justify-between gap-4">
              <div className="flex items-center gap-2">
                <span className="inline-block h-3 w-3 rounded-sm" style={{ backgroundColor: d.fill }} />
                <span className="text-foreground">{d.name}</span>
              </div>
              <div className="text-right">
                <span className="font-medium text-foreground">{formatCurrency(d.value)}</span>
                <span className="text-muted-foreground ml-2">({d.percent.toFixed(1)}%)</span>
              </div>
            </div>
          ))}
          <div className="border-t border-border pt-2 flex items-center justify-between gap-4">
            <span className="font-medium text-foreground">总计</span>
            <span className="font-bold text-foreground">{formatCurrency(total)}</span>
          </div>
        </div>
      </div>
    </Card>
  );
}

// ─── Monthly Income/Expense Bar Chart ───────────────────────────────────

interface MonthlyBarChartProps {
  data: Array<{ period: string; income: string; expense: string }>;
}

export function MonthlyBarChart({ data }: MonthlyBarChartProps) {
  const chartData = [...data].reverse().map((d) => ({
    period: periodLabel(d.period),
    收入: parseFloat(d.income) || 0,
    支出: parseFloat(d.expense) || 0,
  }));

  if (chartData.length === 0) {
    return <EmptyChart message="暂无数据" />;
  }

  return (
    <Card title="月度收支对比">
      <ResponsiveContainer width="100%" height={300}>
        <BarChart data={chartData} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
          <XAxis dataKey="period" tick={axisStyle} tickLine={false} axisLine={false} />
          <YAxis tick={axisStyle} tickLine={false} axisLine={false} tickFormatter={(v) => formatNumber(v)} />
          <Tooltip content={<ChartTooltip />} />
          <Legend />
          <Bar dataKey="收入" fill="hsl(160, 60%, 45%)" radius={[4, 4, 0, 0]} />
          <Bar dataKey="支出" fill="hsl(340, 70%, 55%)" radius={[4, 4, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </Card>
  );
}

// ─── Savings Rate Trend (Line) ──────────────────────────────────────────

interface SavingsRateChartProps {
  data: Array<{ period: string; income: string; expense: string; savings: string }>;
}

export function SavingsRateChart({ data }: SavingsRateChartProps) {
  const chartData = [...data].reverse().map((d) => {
    const income = parseFloat(d.income) || 0;
    const savings = parseFloat(d.savings) || 0;
    const rate = income > 0 ? (savings / income) * 100 : 0;
    return {
      period: periodLabel(d.period),
      储蓄率: Math.round(rate * 10) / 10,
    };
  });

  if (chartData.length === 0) {
    return <EmptyChart message="暂无数据" />;
  }

  return (
    <Card title="储蓄率趋势">
      <div className="mb-2 text-xs text-muted-foreground">储蓄率 = (收入 − 支出) / 收入 × 100%</div>
      <ResponsiveContainer width="100%" height={240}>
        <LineChart data={chartData} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
          <XAxis dataKey="period" tick={axisStyle} tickLine={false} axisLine={false} />
          <YAxis
            tick={axisStyle}
            tickLine={false}
            axisLine={false}
            tickFormatter={(v) => `${v}%`}
            domain={["auto", "auto"]}
          />
          <Tooltip
            content={({ active, payload, label }) => {
              if (!active || !payload?.length) return null;
              return (
                <div className="rounded-lg border border-border bg-card px-3 py-2 shadow-lg text-sm">
                  <p className="text-muted-foreground mb-1">{label}</p>
                  <span className="font-medium text-foreground">
                    {Number(payload[0]?.value ?? 0) >= 0 ? "+" : ""}{Number(payload[0]?.value ?? 0).toFixed(1)}%
                  </span>
                </div>
              );
            }}
          />
          <Line
            type="monotone"
            dataKey="储蓄率"
            stroke="hsl(220, 70%, 55%)"
            strokeWidth={2.5}
            dot={{ r: 4, fill: "hsl(220, 70%, 55%)" }}
            activeDot={{ r: 6 }}
          />
        </LineChart>
      </ResponsiveContainer>
    </Card>
  );
}

// ─── Expense Category Pie ───────────────────────────────────────────────

interface ExpensePieChartProps {
  categories: Array<{ category_name: string; kind: string; total: string; count: number }>;
  selectedKind: "expense" | "income";
}

export function ExpensePieChart({ categories, selectedKind }: ExpensePieChartProps) {
  const filtered = categories.filter((c) => c.kind === selectedKind);
  if (filtered.length === 0) {
    return <EmptyChart message={`暂无${selectedKind === "expense" ? "支出" : "收入"}分类数据`} />;
  }

  const total = filtered.reduce((sum, c) => sum + Math.abs(parseFloat(c.total)), 0);

  const pieData = filtered.map((c, i) => ({
    name: c.category_name,
    value: Math.abs(parseFloat(c.total)),
    fill: CHART_COLORS[i % CHART_COLORS.length],
    percent: total > 0 ? (Math.abs(parseFloat(c.total)) / total) * 100 : 0,
    count: c.count,
  }));

  return (
    <Card title={`${selectedKind === "expense" ? "支出" : "收入"}分类分布`}>
      <div className="flex flex-col lg:flex-row items-center gap-6">
        <ResponsiveContainer width="100%" height={280} className="max-w-[320px]">
          <PieChart>
            <Pie
              data={pieData}
              cx="50%"
              cy="50%"
              outerRadius={100}
              paddingAngle={2}
              dataKey="value"
              stroke="none"
              label={({ name, percent }) => `${name} ${(percent * 100).toFixed(0)}%`}
            >
              {pieData.map((entry, i) => (
                <Cell key={i} fill={entry.fill} />
              ))}
            </Pie>
            <Tooltip content={<PieTooltip />} />
          </PieChart>
        </ResponsiveContainer>
        <div className="flex flex-col gap-2 text-sm w-full lg:w-auto">
          {pieData.map((d) => (
            <div key={d.name} className="flex items-center justify-between gap-4">
              <div className="flex items-center gap-2">
                <span className="inline-block h-3 w-3 rounded-sm" style={{ backgroundColor: d.fill }} />
                <span className="text-foreground">{d.name}</span>
                <span className="text-muted-foreground text-xs">({d.count}笔)</span>
              </div>
              <div className="text-right">
                <span className="font-medium text-foreground">{formatCurrency(d.value)}</span>
                <span className="text-muted-foreground ml-2">({d.percent.toFixed(1)}%)</span>
              </div>
            </div>
          ))}
          <div className="border-t border-border pt-2 flex items-center justify-between gap-4">
            <span className="font-medium text-foreground">总计</span>
            <span className="font-bold text-foreground">{formatCurrency(total)}</span>
          </div>
        </div>
      </div>
    </Card>
  );
}

// ─── Card wrapper ───────────────────────────────────────────────────────

function Card({ title, children, className = "", id }: { title: string; children: React.ReactNode; className?: string; id?: string }) {
  const ref = useRef<HTMLDivElement>(null);

  return (
    <div
      ref={ref}
      id={id}
      className={`rounded-xl border border-border bg-card p-4 md:p-6 ${className}`}
    >
      <h3 className="text-base font-semibold text-card-foreground mb-4">{title}</h3>
      {children}
    </div>
  );
}

function EmptyChart({ message }: { message: string }) {
  return (
    <Card title="">
      <div className="flex items-center justify-center h-[240px] text-muted-foreground text-sm">
        {message}
      </div>
    </Card>
  );
}

// ─── Export helpers ─────────────────────────────────────────────────────

export function exportChartAsPng(chartId: string, filename: string) {
  const el = document.getElementById(chartId);
  if (!el) return;
  
  // Use canvas approach — recharts renders to SVG, so we serialize that
  const svgEl = el.querySelector("svg.recharts-surface");
  if (!svgEl) return;
  
  const svgData = new XMLSerializer().serializeToString(svgEl);
  const svgBlob = new Blob([svgData], { type: "image/svg+xml;charset=utf-8" });
  const url = URL.createObjectURL(svgBlob);
  
  const img = new Image();
  img.onload = () => {
    const canvas = document.createElement("canvas");
    const scale = 2; // retina
    canvas.width = img.width * scale;
    canvas.height = img.height * scale;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(scale, scale);
    // White background
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, img.width, img.height);
    ctx.drawImage(img, 0, 0);
    
    canvas.toBlob((blob) => {
      if (blob) {
        const { downloadBlob } = require("@/lib/api");
        downloadBlob(blob, filename);
      }
    }, "image/png");
    URL.revokeObjectURL(url);
  };
  img.src = url;
}
