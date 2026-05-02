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
