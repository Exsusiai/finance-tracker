"use client";

import { useState } from "react";
import { mutate as swrMutate } from "swr";
import { useInbox, useCategories } from "@/lib/hooks";
import { ApiError, confirmInboxItem, type CategoryOut, type TransactionOut } from "@/lib/api";
import { cn, formatCurrency, formatDate } from "@/lib/utils";
import { LoadingSpinner } from "@/components/ui-common";

/**
 * Pending-transaction inbox.
 *
 * Each row shows: date · description · amount · suggested-category · row actions.
 * The user can:
 *   - Accept the suggested category (just click "确认")
 *   - Pick a different category from the dropdown then "确认" (this triggers
 *     `learn_from_user_assignment` on the backend → builds a new rule)
 *   - Skip (leave it pending; it stays in the inbox)
 */
export function InboxPanel() {
  const { data: items, isLoading, mutate: refreshInbox } = useInbox(200);
  const { data: categories } = useCategories();

  const refreshAfterConfirm = () => {
    refreshInbox();
    // Also invalidate the transactions list and cashflow / dashboard caches
    swrMutate(
      (k) =>
        typeof k === "string" &&
        (k.startsWith("transactions") || k.startsWith("cashflow") || k.startsWith("balances")),
      undefined,
      { revalidate: true },
    );
  };

  if (isLoading) return <LoadingSpinner />;
  if (!items || items.length === 0) {
    return (
      <div className="rounded-xl border border-border bg-card p-12 text-center">
        <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-emerald-500/10">
          <svg className="h-6 w-6 text-emerald-600 dark:text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
          </svg>
        </div>
        <p className="text-base font-medium mb-1">收件箱已清空</p>
        <p className="text-sm text-muted-foreground">
          暂无待确认的交易。新导入的 PDF 会自动出现在这里。
        </p>
      </div>
    );
  }

  // Group by detected_bank / source for visual grouping
  const expense = (categories ?? []).filter((c) => c.kind === "expense");

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          <span className="font-medium text-foreground">{items.length}</span> 笔待确认交易
          <span className="ml-2 text-xs">
            ｜ 已自动建议分类的可一键确认；改选其他分类会让系统记住下次自动归并
          </span>
        </p>
      </div>

      <div className="rounded-xl border border-border bg-card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left">日期</th>
                <th className="px-3 py-2 text-left">描述</th>
                <th className="px-3 py-2 text-right">金额</th>
                <th className="px-3 py-2 text-left">分类</th>
                <th className="px-3 py-2 text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((tx) => (
                <InboxRow
                  key={tx.id}
                  tx={tx}
                  categories={expense}
                  onDone={refreshAfterConfirm}
                />
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

interface InboxRowProps {
  tx: TransactionOut;
  categories: CategoryOut[];
  onDone: () => void;
}

function InboxRow({ tx, categories, onDone }: InboxRowProps) {
  const [pickedCat, setPickedCat] = useState<number | null>(tx.category_id ?? null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isUserChange = pickedCat !== (tx.category_id ?? null);

  const handleConfirm = async () => {
    setError(null);
    try {
      setSubmitting(true);
      // Always send category_id (even if unchanged) so backend's learn-only-on-change
      // logic can short-circuit cleanly.
      await confirmInboxItem(tx.id, { category_id: pickedCat });
      onDone();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "确认失败");
    } finally {
      setSubmitting(false);
    }
  };

  const grouped = categoriesByParent(categories);

  return (
    <tr className="border-t border-border hover:bg-muted/30 transition-colors">
      <td className="px-3 py-2.5 whitespace-nowrap text-muted-foreground">
        {formatDate(tx.occurred_at)}
      </td>
      <td className="px-3 py-2.5">
        <div className="font-medium text-foreground truncate max-w-[280px]" title={tx.description ?? ""}>
          {tx.description || tx.raw_description || "—"}
        </div>
        {tx.account_name && (
          <div className="text-[10px] text-muted-foreground mt-0.5">{tx.account_name}</div>
        )}
      </td>
      <td className={cn(
        "px-3 py-2.5 text-right tabular-nums whitespace-nowrap font-medium",
        tx.type === "income" ? "text-emerald-600 dark:text-emerald-400"
        : tx.type === "expense" ? "text-rose-600 dark:text-rose-400"
        : "text-foreground",
      )}>
        {tx.type === "income" ? "+" : tx.type === "expense" ? "-" : ""}
        {formatCurrency(Math.abs(parseFloat(tx.amount)), tx.currency)}
      </td>
      <td className="px-3 py-2.5">
        <select
          value={pickedCat ?? ""}
          onChange={(e) => setPickedCat(e.target.value ? Number(e.target.value) : null)}
          className={cn(
            "px-2 py-1 text-xs rounded-md border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring max-w-[200px]",
            tx.category_id ? "border-border" : "border-amber-500/40",
          )}
        >
          <option value="">— 未分类 —</option>
          {grouped.map(([parent, kids]) => (
            <optgroup key={parent.id} label={parent.name}>
              {kids.map((k) => (
                <option key={k.id} value={k.id}>
                  {k.name}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        {tx.category_id && !isUserChange && (
          <div className="text-[10px] text-emerald-600 dark:text-emerald-400 mt-0.5">
            ✓ 已建议
          </div>
        )}
        {isUserChange && (
          <div className="text-[10px] text-amber-600 dark:text-amber-400 mt-0.5">
            ⚡ 确认后会被记住
          </div>
        )}
      </td>
      <td className="px-3 py-2.5 text-right whitespace-nowrap">
        <button
          onClick={handleConfirm}
          disabled={submitting}
          className="text-xs px-3 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
        >
          {submitting ? "确认中…" : "确认"}
        </button>
        {error && (
          <div className="text-[10px] text-destructive mt-1">{error}</div>
        )}
      </td>
    </tr>
  );
}

/** Group categories by their parent (only return parents that have at least one child). */
function categoriesByParent(cats: CategoryOut[]): Array<[CategoryOut, CategoryOut[]]> {
  const parents = cats.filter((c) => c.parent_id == null);
  return parents
    .map((p) => [p, cats.filter((c) => c.parent_id === p.id)] as [CategoryOut, CategoryOut[]])
    .filter(([, kids]) => kids.length > 0);
}
