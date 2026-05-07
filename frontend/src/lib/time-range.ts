/** "YYYY-MM" → first/last calendar day strings of that month.
 *  DST-safe: `new Date(y, m, 0)` evaluates to "day 0 of month m+1" =
 *  last day of month m, with no fixed-millisecond arithmetic to fall over
 *  spring-forward / fall-back transitions. */
export function monthDateRange(period: string): { from: string; to: string } {
  const [y, m] = period.split("-").map(Number);
  const first = `${y}-${String(m).padStart(2, "0")}-01`;
  const last = new Date(y, m, 0);
  const to = `${last.getFullYear()}-${String(last.getMonth() + 1).padStart(2, "0")}-${String(last.getDate()).padStart(2, "0")}`;
  return { from: first, to };
}

/** Shift a "YYYY-MM" period by `delta` months (negative = earlier). */
export function shiftPeriod(period: string, delta: number): string {
  const [y, m] = period.split("-").map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

/** Current "YYYY-MM" string (local time). */
export function currentPeriod(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

/** Last N months as "YYYY-MM" strings, newest first. */
export function recentPeriods(n: number = 12): string[] {
  const out: string[] = [];
  const now = new Date();
  for (let i = 0; i < n; i++) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    out.push(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`);
  }
  return out;
}

export type TimeRange = "1m" | "3m" | "6m" | "1y" | "all";

export function getTimeRangeDates(range: TimeRange): { from: string; to: string } {
  const now = new Date();
  const to = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
  let from: Date;

  switch (range) {
    case "1m":
      from = new Date(now.getFullYear(), now.getMonth() - 1, 1);
      break;
    case "3m":
      from = new Date(now.getFullYear(), now.getMonth() - 3, 1);
      break;
    case "6m":
      from = new Date(now.getFullYear(), now.getMonth() - 6, 1);
      break;
    case "1y":
      from = new Date(now.getFullYear() - 1, now.getMonth(), 1);
      break;
    case "all":
    default:
      from = new Date(2020, 0, 1);
      break;
  }

  const fromStr = `${from.getFullYear()}-${String(from.getMonth() + 1).padStart(2, "0")}`;
  return { from: fromStr, to };
}
