import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import type { FxRateOut } from "@/lib/api";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Crypto / pseudo-currencies that are not ISO-4217 — render with a manual symbol. */
const NON_ISO_CURRENCY_SYMBOLS: Record<string, string> = {
  USDT: "₮",
  USDC: "$",
  DAI: "DAI ",
  BUSD: "BUSD ",
  TUSD: "TUSD ",
  BTC: "₿",
  ETH: "Ξ",
  SOL: "SOL ",
};

export function formatCurrency(value: string | number, currency: string = "EUR"): string {
  const num = typeof value === "string" ? parseFloat(value) : value;
  if (isNaN(num)) return "—";

  const code = (currency || "EUR").toUpperCase();
  const sym = NON_ISO_CURRENCY_SYMBOLS[code];
  if (sym !== undefined) {
    const formatted = new Intl.NumberFormat("de-DE", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(num);
    return `${sym}${formatted}`;
  }

  try {
    return new Intl.NumberFormat("de-DE", {
      style: "currency",
      currency: code,
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(num);
  } catch {
    // Unknown ISO code — fall back to plain number with the code suffix.
    const formatted = new Intl.NumberFormat("de-DE", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(num);
    return `${formatted} ${code}`;
  }
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

export function formatDate(dateStr: string): string {
  if (!dateStr) return "—";
  try {
    const d = new Date(dateStr.includes("T") ? dateStr : dateStr + "T00:00:00");
    if (isNaN(d.getTime())) return dateStr;
    return d.toLocaleDateString("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
  } catch {
    return dateStr;
  }
}

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function periodLabel(period: string): string {
  const [year, month] = period.split("-");
  const date = new Date(parseInt(year), parseInt(month) - 1);
  return date.toLocaleDateString("zh-CN", { year: "numeric", month: "short" });
}

/** Categorical palette (theme-aware via CSS vars). Jewel-toned, evenly spaced
 *  hues at matched lightness so distribution categories stay distinguishable. */
export const CHART_COLORS = [
  "var(--cat-1)",
  "var(--cat-2)",
  "var(--cat-3)",
  "var(--cat-4)",
  "var(--cat-5)",
  "var(--cat-6)",
  "var(--cat-7)",
  "var(--cat-8)",
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

// ─── Currency conversion ───────────────────────────────────────────────

/** Stablecoins that we map 1:1 to USD when no direct quote is available. */
const USD_STABLECOINS = new Set(["USDT", "USDC", "DAI", "BUSD", "TUSD"]);

function normalizeForFx(c: string): string {
  return USD_STABLECOINS.has(c) ? "USD" : c;
}

/** Build a (base→quote) → latest rate map, picking the newest `quoted_at`. */
export function latestFxMap(rates: FxRateOut[] | undefined): Map<string, number> {
  if (!rates) return new Map();
  const tmp = new Map<string, { rate: number; ts: string }>();
  for (const r of rates) {
    const rate = parseFloat(r.rate);
    if (!isFinite(rate) || rate <= 0) continue;
    const key = `${r.base_currency}→${r.quote_currency}`;
    const prev = tmp.get(key);
    if (!prev || r.quoted_at > prev.ts) tmp.set(key, { rate, ts: r.quoted_at });
  }
  const out = new Map<string, number>();
  tmp.forEach((v, k) => out.set(k, v.rate));
  return out;
}

/**
 * Convert `amount` from currency `from` → `to` using `fxMap`.
 * Returns null when no path is available.
 *
 * Strategy: same-currency (with stablecoin folding) → direct → inverse → triangulate via CNY/USD/EUR.
 */
export function convertAmount(
  amount: number | string,
  from: string,
  to: string,
  fxMap: Map<string, number>,
): number | null {
  const num = typeof amount === "string" ? parseFloat(amount) : amount;
  if (!isFinite(num)) return null;

  const f = normalizeForFx(from.toUpperCase());
  const t = normalizeForFx(to.toUpperCase());
  if (f === t) return num;

  const direct = fxMap.get(`${f}→${t}`);
  if (direct) return num * direct;
  const inverse = fxMap.get(`${t}→${f}`);
  if (inverse) return num / inverse;

  for (const pivot of ["CNY", "USD", "EUR"]) {
    if (pivot === f || pivot === t) continue;
    const aDirect = fxMap.get(`${f}→${pivot}`);
    const aInverse = fxMap.get(`${pivot}→${f}`);
    const a = aDirect ?? (aInverse ? 1 / aInverse : null);
    const bDirect = fxMap.get(`${pivot}→${t}`);
    const bInverse = fxMap.get(`${t}→${pivot}`);
    const b = bDirect ?? (bInverse ? 1 / bInverse : null);
    if (a && b) return num * a * b;
  }
  return null;
}

/**
 * Currency choices for account/holding forms, grouped for `<optgroup>`.
 * Keep ISO-4217 codes for fiat; stablecoins / crypto use ad-hoc codes that the
 * formatter falls back to via `NON_ISO_CURRENCY_SYMBOLS`.
 */
export const CURRENCY_GROUPS: Array<{ label: string; values: string[] }> = [
  { label: "法币", values: ["EUR", "USD", "CNY", "GBP", "JPY", "HKD", "CHF"] },
  { label: "稳定币", values: ["USDT", "USDC", "DAI", "BUSD", "TUSD"] },
  { label: "加密货币", values: ["BTC", "ETH", "SOL"] },
];

/** Flat list (preserves group order) — handy for default-pick logic. */
export const ALL_CURRENCIES: string[] = CURRENCY_GROUPS.flatMap((g) => g.values);

/** Display currency choices in the resource selector.
 *  Trimmed to the four the user actually tracks (2026-06-27). */
export const DISPLAY_CURRENCIES = [
  { value: "CNY", label: "¥ CNY" },
  { value: "USD", label: "$ USD" },
  { value: "EUR", label: "€ EUR" },
  { value: "USDT", label: "₮ USDT" },
];

/** Asset-class fills — each class a distinct categorical hue (theme-aware).
 *  Assigned for MAX hue separation so commonly co-held classes never share a
 *  family: cash=amber(gold), fund=emerald(green), us_stock=indigo, crypto=violet. */
export const ASSET_CLASS_COLORS: Record<string, string> = {
  cash: "var(--cat-3)",     // amber / gold (warm — distinct from fund's green)
  fund: "var(--cat-5)",     // emerald
  us_stock: "var(--cat-1)", // indigo
  crypto: "var(--cat-4)",   // violet
  eu_stock: "var(--cat-7)", // sky
  a_share: "var(--cat-6)",  // rose
  gold: "var(--cat-8)",     // orange
  bond: "var(--cat-2)",     // teal
  other: "oklch(0.6 0 0)",  // neutral
};
