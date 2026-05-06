"use client";

import { useEffect, useMemo, useState } from "react";
import {
  useCategories,
  useTransactions,
} from "@/lib/hooks";
import {
  type CategoryOut,
  type TransactionOut,
} from "@/lib/api";
import { cn, formatCurrency, formatDate, periodLabel } from "@/lib/utils";
import { LoadingSpinner } from "@/components/ui-common";
import { InlineCategoryPicker } from "@/components/inline-category-picker";

/**
 * Hierarchical spending view: pick a top-level category → see sub-categories'
 * monthly totals → drill into individual transactions.
 *
 * Designed to replace the "all transactions flat-list" feeling. The user
 * always sees `consumption structure` first, then can zoom in.
 */

interface CategoryBreakdownViewProps {
  /** Default expense | income | transfer (defaults to expense). */
  defaultKind?: "expense" | "income" | "transfer";
}

export function CategoryBreakdownView({ defaultKind = "expense" }: CategoryBreakdownViewProps) {
  const [period, setPeriod] = useState<string>(() => currentYearMonth());
  const [kind, setKind] = useState<"expense" | "income" | "transfer">(defaultKind);
  const [selectedParentId, setSelectedParentId] = useState<number | null>(null);
  const [selectedChildId, setSelectedChildId] = useState<number | null>(null);

  // FIX-3 (review V1 §P1-2): the previous version hard-coded EUR for display
  // even when the user's chosen display / base currency was something else.
  // Read the user's preference (shared with /assets and /dashboard via
  // localStorage); fall back to CNY which is the project's BASE_CURRENCY default.
  const [displayCurrency, setDisplayCurrency] = useState<string>("CNY");
  useEffect(() => {
    const saved = typeof window !== "undefined" ? window.localStorage.getItem("display_currency") : null;
    if (saved) setDisplayCurrency(saved);
  }, []);

  const { data: categories } = useCategories();
  // Pull ALL transactions for the month (including pending) — pending tx
  // already have categories assigned by the auto-categorizer and the user
  // wants to see them in the structural overview.
  const { fromDate, toDate } = useMemo(() => monthRange(period), [period]);
  const { data: txResp, isLoading: txLoading } = useTransactions({
    type: kind,
    from_date: fromDate,
    to_date: toDate,
    limit: 500,
  });

  // Aggregate sub-cat totals up into parents. FIX-3: prefer base_amount (folded
  // to base_currency by the ingestion pipeline) over raw amount. Falls back
  // to amount if base_amount is missing — same-currency rows are still correct;
  // foreign-currency rows without base_amount fall through with a known caveat
  // (Sprint 1 FIX-4 will guarantee base_amount on every ingested row).
  const tree = useMemo(
    () => buildTreeFromTx(categories ?? [], txResp?.data ?? [], kind),
    [categories, txResp, kind],
  );
  const grandTotal = useMemo(() => tree.reduce((s, p) => s + p.total, 0), [tree]);
  const byCatLoading = txLoading;

  // Auto-select the first parent on mount / when kind/period changes.
  useEffect(() => {
    if (tree.length > 0 && !tree.some((p) => p.id === selectedParentId)) {
      setSelectedParentId(tree[0].id);
      setSelectedChildId(null);
    }
  }, [tree, selectedParentId]);

  const selectedParent = tree.find((p) => p.id === selectedParentId) ?? null;

  return (
    <div className="space-y-4">
      <Header
        period={period}
        onPeriodChange={(p) => {
          setPeriod(p);
          setSelectedChildId(null);
        }}
        kind={kind}
        onKindChange={(k) => {
          setKind(k);
          setSelectedParentId(null);
          setSelectedChildId(null);
        }}
        grandTotal={grandTotal}
        displayCurrency={displayCurrency}
      />

      {byCatLoading ? (
        <LoadingSpinner />
      ) : tree.length === 0 ? (
        <div className="rounded-xl border border-border bg-card p-12 text-center text-sm text-muted-foreground">
          {periodLabel(period)} 暂无{kindLabel(kind)}数据
        </div>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[280px_1fr]">
          <ParentList
            parents={tree}
            grandTotal={grandTotal}
            selectedId={selectedParentId}
            onSelect={(id) => {
              setSelectedParentId(id);
              setSelectedChildId(null);
            }}
            displayCurrency={displayCurrency}
          />
          {selectedParent && (
            <ParentDetail
              parent={selectedParent}
              grandTotal={grandTotal}
              period={period}
              kind={kind}
              selectedChildId={selectedChildId}
              onSelectChild={(id) => setSelectedChildId(id === selectedChildId ? null : id)}
              allCategories={categories ?? []}
              displayCurrency={displayCurrency}
            />
          )}
        </div>
      )}
    </div>
  );
}

// ─── Header ────────────────────────────────────────────────────────────

interface HeaderProps {
  period: string;
  onPeriodChange: (p: string) => void;
  kind: "expense" | "income" | "transfer";
  onKindChange: (k: "expense" | "income" | "transfer") => void;
  grandTotal: number;
  displayCurrency: string;
}

function Header({ period, onPeriodChange, kind, onKindChange, grandTotal, displayCurrency }: HeaderProps) {
  const months = useMemo(() => last12Months(), []);
  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2">
            <button
              onClick={() => onPeriodChange(shiftMonth(period, -1))}
              className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
              aria-label="上个月"
            >
              ◀
            </button>
            <select
              value={period}
              onChange={(e) => onPeriodChange(e.target.value)}
              className="px-2.5 py-1.5 text-sm rounded-md border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring tabular-nums"
            >
              {months.map((m) => (
                <option key={m} value={m}>
                  {periodLabel(m)}
                </option>
              ))}
            </select>
            <button
              onClick={() => onPeriodChange(shiftMonth(period, 1))}
              className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
              aria-label="下个月"
            >
              ▶
            </button>
          </div>

          <div className="inline-flex rounded-md border border-border bg-card p-0.5">
            {(["expense", "income", "transfer"] as const).map((k) => (
              <button
                key={k}
                onClick={() => onKindChange(k)}
                className={cn(
                  "px-2.5 py-1 text-xs font-medium rounded transition-colors",
                  kind === k
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {kindLabel(k)}
              </button>
            ))}
          </div>
        </div>

        <div className="text-right">
          <p className="text-xs text-muted-foreground">
            总{kindLabel(kind)} <span className="text-[10px] opacity-60">({displayCurrency})</span>
          </p>
          <p className="text-2xl font-bold tabular-nums">
            {grandTotal > 0 ? formatCurrency(grandTotal, displayCurrency) : "—"}
          </p>
        </div>
      </div>
    </div>
  );
}

// ─── Parent list (left column) ─────────────────────────────────────────

interface ParentListProps {
  parents: ParentNode[];
  grandTotal: number;
  selectedId: number | null;
  onSelect: (id: number) => void;
  displayCurrency: string;
}

function ParentList({ parents, grandTotal, selectedId, onSelect, displayCurrency }: ParentListProps) {
  return (
    <div className="rounded-xl border border-border bg-card overflow-hidden">
      <div className="px-3 py-2 border-b border-border bg-muted/30">
        <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
          一级类目
        </p>
      </div>
      <ul>
        {parents.map((p) => {
          const pct = grandTotal > 0 ? (p.total / grandTotal) * 100 : 0;
          const active = p.id === selectedId;
          return (
            <li
              key={p.id}
              onClick={() => onSelect(p.id)}
              className={cn(
                "px-3 py-2.5 cursor-pointer border-b border-border last:border-0 transition-colors",
                active ? "bg-primary/10" : "hover:bg-muted/40",
              )}
            >
              <div className="flex items-center justify-between gap-2 mb-1">
                <span className={cn("text-sm font-medium truncate", active ? "text-foreground" : "text-foreground")}>
                  {p.name}
                </span>
                <span className="text-xs tabular-nums text-foreground shrink-0">
                  {formatCurrency(p.total, displayCurrency)}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <div className="flex-1 h-1.5 rounded-full bg-muted overflow-hidden">
                  <div
                    className={cn(
                      "h-full transition-all",
                      active ? "bg-primary" : "bg-primary/60",
                    )}
                    style={{ width: `${pct}%` }}
                  />
                </div>
                <span className="text-[10px] text-muted-foreground tabular-nums w-9 text-right">
                  {pct.toFixed(0)}%
                </span>
              </div>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                {p.children.length} 个二级类目 · {p.txCount} 笔
              </p>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// ─── Parent detail (right column) ──────────────────────────────────────

interface ParentDetailProps {
  parent: ParentNode;
  grandTotal: number;
  period: string;
  kind: "expense" | "income" | "transfer";
  selectedChildId: number | null;
  onSelectChild: (id: number) => void;
  allCategories: CategoryOut[];
  displayCurrency: string;
}

function ParentDetail({ parent, grandTotal, period, kind, selectedChildId, onSelectChild, allCategories, displayCurrency }: ParentDetailProps) {
  const parentPct = grandTotal > 0 ? (parent.total / grandTotal) * 100 : 0;

  return (
    <div className="rounded-xl border border-border bg-card overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <div className="flex items-baseline justify-between gap-3">
          <h3 className="text-base font-semibold">{parent.name}</h3>
          <div className="text-right">
            <p className="text-lg font-bold tabular-nums">{formatCurrency(parent.total, displayCurrency)}</p>
            <p className="text-[10px] text-muted-foreground">
              占总{kindLabel(kind)} {parentPct.toFixed(1)}%
            </p>
          </div>
        </div>
      </div>

      {parent.children.length === 0 ? (
        <div className="p-8 text-center text-sm text-muted-foreground">
          该类目下暂无二级类目数据
        </div>
      ) : (
        <ul className="divide-y divide-border">
          {parent.children.map((c) => {
            const pct = parent.total > 0 ? (c.total / parent.total) * 100 : 0;
            const expanded = c.id === selectedChildId;
            return (
              <li key={c.id}>
                <div
                  onClick={() => onSelectChild(c.id)}
                  className={cn(
                    "px-4 py-2.5 cursor-pointer transition-colors",
                    expanded ? "bg-primary/5" : "hover:bg-muted/30",
                  )}
                >
                  <div className="flex items-center justify-between gap-3 mb-1">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className={cn(
                        "text-xs transition-transform inline-block",
                        expanded && "rotate-90",
                      )}>▶</span>
                      <span className="text-sm font-medium text-foreground truncate">
                        {c.name}
                      </span>
                      <span className="text-[10px] text-muted-foreground shrink-0">
                        {c.count} 笔
                      </span>
                    </div>
                    <span className="text-sm tabular-nums font-medium shrink-0">
                      {formatCurrency(c.total, displayCurrency)}
                    </span>
                  </div>
                  <div className="flex items-center gap-2 ml-5">
                    <div className="flex-1 h-1 rounded-full bg-muted overflow-hidden">
                      <div className="h-full bg-primary/70" style={{ width: `${pct}%` }} />
                    </div>
                    <span className="text-[10px] text-muted-foreground tabular-nums w-9 text-right">
                      {pct.toFixed(0)}%
                    </span>
                  </div>
                </div>
                {expanded && (
                  <ChildTransactions
                    categoryId={c.id}
                    period={period}
                    allCategories={allCategories}
                  />
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

// ─── Drilldown: transactions in a subcategory ──────────────────────────

interface ChildTransactionsProps {
  categoryId: number;
  period: string;
  allCategories: CategoryOut[];
}

function ChildTransactions({ categoryId, period, allCategories }: ChildTransactionsProps) {
  const { fromDate, toDate } = monthRange(period);
  const { data: txResp, isLoading, mutate: refreshList } = useTransactions({
    category_id: categoryId,
    from_date: fromDate,
    to_date: toDate,
    limit: 200,
  });

  const txs = txResp?.data ?? [];

  if (isLoading) {
    return <div className="px-4 py-3 text-xs text-muted-foreground">加载中…</div>;
  }
  if (txs.length === 0) {
    return <div className="px-4 py-3 text-xs text-muted-foreground">该月该分类无交易</div>;
  }

  return (
    <div className="bg-muted/20 border-t border-border">
      <table className="w-full text-xs">
        <tbody>
          {txs.map((t) => (
            <tr key={t.id} className="border-b border-border last:border-0">
              <td className="px-4 py-1.5 whitespace-nowrap text-muted-foreground w-24">
                {formatDate(t.occurred_at)}
              </td>
              <td className="px-2 py-1.5">
                <span className="text-foreground truncate block max-w-md" title={t.description ?? ""}>
                  {t.description || t.raw_description || "—"}
                </span>
                {t.account_name && (
                  <span className="text-[10px] text-muted-foreground">{t.account_name}</span>
                )}
              </td>
              <td className="px-2 py-1.5 whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
                <InlineCategoryPicker
                  tx={t}
                  categories={allCategories}
                  onChanged={() => refreshList()}
                  variant="icon"
                />
              </td>
              <td className={cn(
                "px-4 py-1.5 text-right tabular-nums whitespace-nowrap font-medium",
                t.type === "income" ? "text-emerald-600 dark:text-emerald-400"
                : t.type === "expense" ? "text-rose-600 dark:text-rose-400"
                : "text-foreground",
              )}>
                {t.type === "income" ? "+" : t.type === "expense" ? "-" : ""}
                {formatCurrency(Math.abs(parseFloat(t.amount)), t.currency)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── Helpers ───────────────────────────────────────────────────────────

interface ChildNode {
  id: number;
  name: string;
  total: number;
  count: number;
}

interface ParentNode {
  id: number;
  name: string;
  total: number;
  txCount: number;
  children: ChildNode[];
}

function buildTreeFromTx(
  categories: CategoryOut[],
  txs: TransactionOut[],
  kind: "expense" | "income" | "transfer",
): ParentNode[] {
  // Filter categories by kind
  const ofKind = categories.filter((c) => c.kind === kind);
  const parents = ofKind.filter((c) => c.parent_id == null);
  const childrenByParent = new Map<number, CategoryOut[]>();
  for (const c of ofKind) {
    if (c.parent_id == null) continue;
    const arr = childrenByParent.get(c.parent_id) ?? [];
    arr.push(c);
    childrenByParent.set(c.parent_id, arr);
  }

  // Aggregate transactions by category_id. FIX-3: prefer base_amount (already
  // folded to BASE_CURRENCY by the parser / ingestion); fall back to amount
  // for rows that don't have it yet (handled fully in Sprint 1 FIX-4).
  const aggByCat = new Map<number, { total: number; count: number }>();
  for (const t of txs) {
    if (t.category_id == null) continue;
    if (t.type !== kind) continue;
    const baseAmt = (t as { base_amount?: string | null }).base_amount;
    const raw = baseAmt != null ? parseFloat(baseAmt) : parseFloat(t.amount);
    const amt = Math.abs(raw || 0);
    const cur = aggByCat.get(t.category_id) ?? { total: 0, count: 0 };
    cur.total += amt;
    cur.count += 1;
    aggByCat.set(t.category_id, cur);
  }

  const result: ParentNode[] = parents.map((p) => {
    const kids = (childrenByParent.get(p.id) ?? []).map<ChildNode>((c) => {
      const agg = aggByCat.get(c.id);
      return {
        id: c.id,
        name: c.name,
        total: agg?.total ?? 0,
        count: agg?.count ?? 0,
      };
    });
    // Also include direct hits on the parent itself (if some tx is tagged at parent level)
    const parentDirect = aggByCat.get(p.id);
    const childrenTotal = kids.reduce((s, k) => s + k.total, 0);
    const childrenCount = kids.reduce((s, k) => s + k.count, 0);
    const total = childrenTotal + (parentDirect?.total ?? 0);
    const txCount = childrenCount + (parentDirect?.count ?? 0);
    if (parentDirect && parentDirect.total > 0) {
      // Show un-subcategorized parent-level tx as a synthetic "（未分二级）"
      kids.push({
        id: -p.id,  // negative id won't clash; not clickable for drill-down
        name: "（未分二级）",
        total: parentDirect.total,
        count: parentDirect.count,
      });
    }
    return {
      id: p.id,
      name: p.name,
      total,
      txCount,
      children: kids
        .filter((k) => k.total > 0)
        .sort((a, b) => b.total - a.total),
    };
  });

  return result
    .filter((p) => p.total > 0)
    .sort((a, b) => b.total - a.total);
}

function currentYearMonth(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function shiftMonth(period: string, delta: number): string {
  const [y, m] = period.split("-").map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function last12Months(): string[] {
  const out: string[] = [];
  const now = new Date();
  for (let i = 0; i < 12; i++) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    out.push(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`);
  }
  return out;
}

function monthRange(period: string): { fromDate: string; toDate: string } {
  const [y, m] = period.split("-").map(Number);
  const first = `${y}-${String(m).padStart(2, "0")}-01`;
  const next = new Date(y, m, 1);
  const last = new Date(next.getTime() - 86400000);
  const toDate = `${last.getFullYear()}-${String(last.getMonth() + 1).padStart(2, "0")}-${String(last.getDate()).padStart(2, "0")}`;
  return { fromDate: first, toDate };
}

function kindLabel(k: string): string {
  return k === "expense" ? "支出" : k === "income" ? "收入" : "转账";
}
