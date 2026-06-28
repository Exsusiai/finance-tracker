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
import { formatCurrency, formatPercent, periodLabel, ASSET_CLASS_LABELS } from "@/lib/utils";
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
  fill: "var(--chart-axis)",
};

// Distribution pies / allocation use the jewel-toned categorical palette so
// categories stay distinguishable; single-series trends use --chart-ink (brand
// indigo) and income/expense use gain/loss — all via CSS vars, theme-aware.
const RAMP = [
  "var(--cat-1)",
  "var(--cat-2)",
  "var(--cat-3)",
  "var(--cat-4)",
  "var(--cat-5)",
  "var(--cat-6)",
  "var(--cat-7)",
  "var(--cat-8)",
];

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
              <stop offset="5%" stopColor="var(--chart-gain)" stopOpacity={0.3} />
              <stop offset="95%" stopColor="var(--chart-gain)" stopOpacity={0} />
            </linearGradient>
            <linearGradient id="gradExpense" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="var(--chart-loss)" stopOpacity={0.3} />
              <stop offset="95%" stopColor="var(--chart-loss)" stopOpacity={0} />
            </linearGradient>
            <linearGradient id="gradSavings" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="var(--chart-ink)" stopOpacity={0.3} />
              <stop offset="95%" stopColor="var(--chart-ink)" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
          <XAxis dataKey="period" tick={axisStyle} tickLine={false} axisLine={false} />
          <YAxis tick={axisStyle} tickLine={false} axisLine={false} tickFormatter={(v) => formatNumber(v)} />
          <Tooltip content={<ChartTooltip />} />
          <Legend />
          <Area type="monotone" dataKey="收入" stroke="var(--chart-gain)" fill="url(#gradIncome)" strokeWidth={2} />
          <Area type="monotone" dataKey="支出" stroke="var(--chart-loss)" fill="url(#gradExpense)" strokeWidth={2} />
          <Area type="monotone" dataKey="净储蓄" stroke="var(--chart-ink)" fill="url(#gradSavings)" strokeWidth={2} />
          {showBrush && (
            <Brush dataKey="period" height={30} stroke="var(--chart-ink)" startIndex={0} endIndex={chartData.length - 1} />
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

  const pieData = entries.map(([key, val], idx) => ({
    name: mode === "class" ? (ASSET_CLASS_LABELS[key] || key) : key,
    value: parseFloat(val.value || "0"),
    fill: RAMP[idx % RAMP.length],
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
          <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" vertical={false} />
          <XAxis dataKey="period" tick={axisStyle} tickLine={false} axisLine={false} />
          <YAxis tick={axisStyle} tickLine={false} axisLine={false} tickFormatter={(v) => formatNumber(v)} />
          <Tooltip content={<ChartTooltip />} />
          <Legend />
          <Bar dataKey="收入" fill="var(--chart-gain)" radius={[4, 4, 0, 0]} />
          <Bar dataKey="支出" fill="var(--chart-loss)" radius={[4, 4, 0, 0]} />
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
          <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
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
            stroke="var(--chart-ink)"
            strokeWidth={2.5}
            dot={{ r: 4, fill: "var(--chart-ink)" }}
            activeDot={{ r: 6 }}
          />
        </LineChart>
      </ResponsiveContainer>
    </Card>
  );
}

// ─── Financial Flow: monthly income/expense + cumulative savings ────────

interface FinancialFlowChartProps {
  periods: string[];
  income: string[];
  expense: string[];
  cash: string[]; // real cash assets at each month-end (account balances, base ccy)
  currency: string;
}

/** Income & expense (left axis, monthly flows) + cash assets (right axis, the
 *  real balance of all cash accounts at each month-end). Stock-vs-flow scales
 *  differ, hence two axes. */
export function FinancialFlowChart({ periods, income, expense, cash, currency }: FinancialFlowChartProps) {
  const data = periods.map((p, i) => ({
    period: periodLabel(p),
    收入: parseFloat(income[i] || "0"),
    支出: parseFloat(expense[i] || "0"),
    现金资产: parseFloat(cash[i] || "0"),
  }));

  if (data.length === 0) return <EmptyChart message="暂无数据" />;

  return (
    <Card title="收支与现金资产趋势">
      <ResponsiveContainer width="100%" height={340}>
        <LineChart data={data} margin={{ top: 5, right: 16, left: 10, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
          <XAxis dataKey="period" tick={axisStyle} tickLine={false} axisLine={false} />
          <YAxis yAxisId="left" tick={axisStyle} tickLine={false} axisLine={false} tickFormatter={(v) => formatNumber(v)} />
          <YAxis yAxisId="right" orientation="right" tick={axisStyle} tickLine={false} axisLine={false} tickFormatter={(v) => formatNumber(v)} />
          <Tooltip content={<MoneyTooltip currency={currency} />} />
          <Legend />
          <Line yAxisId="left" type="monotone" dataKey="收入" stroke="var(--chart-gain)" strokeWidth={2} dot={false} />
          <Line yAxisId="left" type="monotone" dataKey="支出" stroke="var(--chart-loss)" strokeWidth={2} dot={false} />
          <Line yAxisId="right" type="monotone" dataKey="现金资产" stroke="var(--chart-ink)" strokeWidth={2.5} dot={{ r: 3 }} activeDot={{ r: 5 }} />
        </LineChart>
      </ResponsiveContainer>
      <p className="mt-2 text-xs text-muted-foreground">
        收入 / 支出（左轴）为每月流量；现金资产（右轴）= 所有现金/银行账户在该月末的真实余额（初始余额 + 账本，折 {currency}），随收支与转账变动。
      </p>
    </Card>
  );
}

// ─── Portfolio market value over time (forward snapshots) ───────────────

interface PortfolioValueChartProps {
  points: Array<{ period: string; investment_total: string }>;
  currency: string;
}

export function PortfolioValueChart({ points, currency }: PortfolioValueChartProps) {
  // `period` is the snapshot week's Monday ("YYYY-MM-DD"); label as MM-DD.
  const weekLabel = (iso: string) => (iso.length >= 10 ? iso.slice(5).replace("-", "/") : iso);
  const data = points.map((p) => ({
    period: weekLabel(p.period),
    组合市值: parseFloat(p.investment_total || "0"),
  }));

  if (data.length === 0) {
    return (
      <Card title="组合市值走势">
        <div className="flex h-[240px] flex-col items-center justify-center gap-2 text-center">
          <p className="text-sm text-muted-foreground">暂无快照数据</p>
          <p className="max-w-sm text-xs text-muted-foreground">
            组合市值历史无法回溯，系统从现在起每周记录一次快照。需积累几周才能看出趋势。
          </p>
        </div>
      </Card>
    );
  }

  return (
    <Card title="组合市值走势">
      <ResponsiveContainer width="100%" height={300}>
        <AreaChart data={data} margin={{ top: 5, right: 16, left: 10, bottom: 5 }}>
          <defs>
            <linearGradient id="gradPortfolio" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="var(--chart-ink)" stopOpacity={0.3} />
              <stop offset="95%" stopColor="var(--chart-ink)" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
          <XAxis dataKey="period" tick={axisStyle} tickLine={false} axisLine={false} />
          <YAxis tick={axisStyle} tickLine={false} axisLine={false} tickFormatter={(v) => formatNumber(v)} domain={["auto", "auto"]} />
          <Tooltip content={<MoneyTooltip currency={currency} />} />
          <Area type="monotone" dataKey="组合市值" stroke="var(--chart-ink)" fill="url(#gradPortfolio)" strokeWidth={2} dot={{ r: 3 }} />
        </AreaChart>
      </ResponsiveContainer>
      <p className="mt-2 text-xs text-muted-foreground">
        每周一次快照（含未实现浮盈），市值随行情波动。单位 {currency}。
        {data.length === 1 && " 已记录 1 周，继续积累中。"}
      </p>
    </Card>
  );
}

// ─── Money-aware tooltip (currency-formatted) ───────────────────────────

function MoneyTooltip({ active, payload, label, currency }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-border bg-card px-3 py-2 shadow-lg text-sm">
      <p className="text-muted-foreground mb-1">{label}</p>
      {payload.map((entry: any, i: number) => (
        <div key={i} className="flex items-center gap-2">
          <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ backgroundColor: entry.color || entry.stroke }} />
          <span className="text-muted-foreground">{entry.name}:</span>
          <span className="font-medium text-foreground">{formatCurrency(Number(entry.value ?? 0), currency)}</span>
        </div>
      ))}
    </div>
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
    fill: RAMP[i % RAMP.length],
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
              // Compute the share from value/total in closure scope. Do NOT
              // read `percent` here: the data row carries its own `percent`
              // field (already ×100) which shadows recharts' 0–1 fraction in
              // this callback, so `percent*100` double-counted → 3313%.
              // Suppress labels for slivers (<3%) so the pie stays legible.
              label={({ name, value }) => {
                const pct = total > 0 ? (Number(value) / total) * 100 : 0;
                return pct >= 3 ? `${name} ${pct.toFixed(1)}%` : "";
              }}
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
      className={`rounded-2xl border border-border bg-card p-5 md:p-6 shadow-xs ${className}`}
    >
      {title && <h3 className="text-sm font-medium text-muted-foreground mb-5">{title}</h3>}
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
    ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--canvas-bg").trim() || "#ffffff";
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
