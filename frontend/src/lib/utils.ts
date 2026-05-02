import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatCurrency(value: string | number, currency: string = "EUR"): string {
  const num = typeof value === "string" ? parseFloat(value) : value;
  if (isNaN(num)) return "—";

  return new Intl.NumberFormat("de-DE", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(num);
}

export function formatPercent(value: string | number): string {
  const num = typeof value === "string" ? parseFloat(value) : value;
  if (isNaN(num)) return "—";
  return `${num >= 0 ? "+" : ""}${num.toFixed(1)}%`;
}

export function formatNumber(value: string | number): string {
  const num = typeof value === "string" ? parseFloat(value) : value;
  if (isNaN(num)) return "—";
  return new Intl.NumberFormat("de-DE", { maximumFractionDigits: 2 }).format(num);
}

export function periodLabel(period: string): string {
  const [year, month] = period.split("-");
  const date = new Date(parseInt(year), parseInt(month) - 1);
  return date.toLocaleDateString("zh-CN", { year: "numeric", month: "short" });
}

/** Generate a palette of distinct colors for charts. */
export const CHART_COLORS = [
  "hsl(220, 70%, 55%)",  // blue
  "hsl(160, 60%, 45%)",  // teal
  "hsl(35, 90%, 55%)",   // amber
  "hsl(340, 70%, 55%)",  // rose
  "hsl(270, 60%, 60%)",  // violet
  "hsl(190, 70%, 50%)",  // cyan
  "hsl(15, 80%, 55%)",   // orange
  "hsl(140, 50%, 45%)",  // green
  "hsl(50, 70%, 50%)",   // yellow
  "hsl(300, 50%, 55%)",  // magenta
];

export const ASSET_CLASS_LABELS: Record<string, string> = {
  cash: "现金",
  a_share: "A股",
  eu_stock: "欧股",
  us_stock: "美股",
  crypto: "加密货币",
  gold: "黄金",
  bond: "债券",
  fund: "基金",
  other: "其他",
};

export const ASSET_CLASS_COLORS: Record<string, string> = {
  cash: "hsl(220, 70%, 55%)",
  a_share: "hsl(340, 70%, 55%)",
  eu_stock: "hsl(190, 70%, 50%)",
  us_stock: "hsl(220, 70%, 55%)",
  crypto: "hsl(270, 60%, 60%)",
  gold: "hsl(35, 90%, 55%)",
  bond: "hsl(160, 60%, 45%)",
  fund: "hsl(140, 50%, 45%)",
  other: "hsl(0, 0%, 50%)",
};
