"use client";

import { useState } from "react";
import { useInbox, useCategories, invalidateTransactionGraph } from "@/lib/hooks";
import {
  ApiError,
  type ApplyScope,
  confirmInboxItem,
  type CategoryOut,
  type TransactionOut,
} from "@/lib/api";
import { CategoryScopeDialog } from "@/components/category-scope-dialog";
import { MarkTransferDialog } from "@/components/mark-transfer-dialog";
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
  const [transferTx, setTransferTx] = useState<TransactionOut | null>(null);

  // Use the canonical graph-wide invalidator. The previous hand-rolled
  // predicate left out transfer-suggestions / transfer-unpaired / accounts /
  // statements, so confirming an inbox item silently desynced those panels.
  const refreshAfterConfirm = () => {
    refreshInbox();
    invalidateTransactionGraph();
  };

  // Initial-load only. Background revalidation keeps the existing list
  // on screen so the user doesn't lose scroll position after confirming.
  if (isLoading && !items) return <LoadingSpinner />;
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

  // Pass ALL categories to each row; row filters by `tx.type` itself so
  // income / transfer / expense rows each see their own category set.
  const allCats = categories ?? [];

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
                  categories={allCats}
                  onDone={refreshAfterConfirm}
                  onRequestMarkTransfer={(t) => setTransferTx(t)}
                />
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {transferTx && (
        <MarkTransferDialog
          tx={transferTx}
          onClose={() => setTransferTx(null)}
          onSuccess={() => {
            setTransferTx(null);
            refreshAfterConfirm();
          }}
        />
      )}
    </div>
  );
}

interface InboxRowProps {
  tx: TransactionOut;
  categories: CategoryOut[];
  onDone: () => void;
  onRequestMarkTransfer: (tx: TransactionOut) => void;
}

function InboxRow({ tx, categories, onDone, onRequestMarkTransfer }: InboxRowProps) {
  const [pickedCat, setPickedCat] = useState<number | null>(tx.category_id ?? null);
  const [scopeDialogOpen, setScopeDialogOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isUserChange = pickedCat !== (tx.category_id ?? null);
  const isTransfer = tx.type === "transfer";

  // Distinguish sub-account vs cross-bank transfer for the visual badge.
  const txMeta = parseMeta(tx.metadata_json);
  const isSubaccount = txMeta?.subaccount === true;
  const isCrossBank = isTransfer && !isSubaccount && tx.counter_account_id != null;
  const transferLabel = isSubaccount ? "内部" : isCrossBank ? "跨行" : isTransfer ? "转账" : null;
  const transferBadgeClass = isSubaccount
    ? "bg-slate-500/15 text-slate-600 dark:text-slate-400"
    : "bg-blue-500/15 text-blue-600 dark:text-blue-400";

  // Filter inbox category dropdown by tx type (expense → expense cats only, etc.)
  const eligibleCategories = categories.filter((c) => c.kind === tx.type);

  // No category change: confirm straight through, no scope dialog needed.
  const handleConfirmDirect = async () => {
    setError(null);
    try {
      setSubmitting(true);
      await confirmInboxItem(tx.id, { category_id: pickedCat });
      invalidateTransactionGraph();
      onDone();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "确认失败");
    } finally {
      setSubmitting(false);
    }
  };

  const handleConfirmClick = () => {
    if (isUserChange && pickedCat !== null) {
      setScopeDialogOpen(true);
    } else {
      void handleConfirmDirect();
    }
  };

  // After user picks a scope in the dialog: send the request and close.
  const handleScopeConfirm = async (scope: ApplyScope, note: string | null) => {
    setError(null);
    try {
      setSubmitting(true);
      const payload: { category_id: number | null; user_note?: string | null } = {
        category_id: pickedCat,
      };
      if ((note ?? null) !== (tx.user_note ?? null)) {
        payload.user_note = note;
      }
      await confirmInboxItem(tx.id, payload, scope);
      invalidateTransactionGraph();
      setScopeDialogOpen(false);
      onDone();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "确认失败");
    } finally {
      setSubmitting(false);
    }
  };


  const grouped = categoriesByParent(eligibleCategories);

  return (
    <tr className="border-t border-border hover:bg-muted/30 transition-colors align-top">
      <td className="px-3 py-2.5 whitespace-nowrap text-muted-foreground">
        {formatDate(tx.occurred_at)}
      </td>
      <td className="px-3 py-2.5">
        <div className="font-medium text-foreground truncate max-w-[280px] flex items-center gap-1.5" title={tx.description ?? ""}>
          {transferLabel && (
            <span
              className={cn(
                "inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-semibold uppercase shrink-0",
                transferBadgeClass,
              )}
              title={
                isSubaccount
                  ? "同银行内子账户互转（不影响该银行余额）"
                  : isCrossBank
                  ? "跨银行转账（已配对，不计入支出/收入）"
                  : "转账（不计入支出/收入）"
              }
            >
              {transferLabel}
            </span>
          )}
          <span className="truncate">{tx.description || tx.raw_description || "—"}</span>
        </div>
        {tx.account_name && (
          <div className="text-[10px] text-muted-foreground mt-0.5">{tx.account_name}</div>
        )}
        {tx.user_note && (
          <div className="mt-1 text-[10px] text-muted-foreground italic max-w-[320px] truncate" title={tx.user_note}>
            备注：{tx.user_note}
          </div>
        )}
        {(() => {
          const sugg = txMeta?.llm_suggestion as
            | {
                category_id: number;
                category_path: string;
                confidence: number;
                reason: string;
                used_search?: boolean;
              }
            | undefined;
          if (!sugg) return null;
          const handleAdopt = () => setPickedCat(sugg.category_id);
          return (
            <div
              className="mt-1.5 max-w-[360px] rounded-md bg-violet-500/10 border border-violet-500/30 px-2 py-1.5 text-[10px] text-violet-700 dark:text-violet-300"
              title={sugg.reason}
            >
              <div className="flex items-center gap-1.5 flex-wrap">
                <span className="font-medium">✨ LLM 推荐</span>
                <span className="font-mono">{sugg.category_path}</span>
                <span className="opacity-70">置信 {sugg.confidence.toFixed(2)}</span>
                {sugg.used_search && <span className="opacity-70">· 已联网</span>}
                <button
                  onClick={handleAdopt}
                  className="ml-auto px-1.5 py-0.5 rounded text-[10px] bg-violet-500/20 hover:bg-violet-500/30"
                >
                  采纳
                </button>
              </div>
              {sugg.reason && (
                <div className="mt-0.5 opacity-80 truncate">{sugg.reason}</div>
              )}
            </div>
          );
        })()}
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
            ⚡ 确认时可选择应用范围
          </div>
        )}
      </td>
      <td className="px-3 py-2.5 text-right whitespace-nowrap">
        <div className="flex flex-col items-end gap-1">
          <button
            onClick={handleConfirmClick}
            disabled={submitting}
            className="text-xs px-3 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
          >
            {submitting ? "确认中…" : "确认"}
          </button>
          {!isTransfer && (
            <button
              type="button"
              onClick={() => onRequestMarkTransfer(tx)}
              disabled={submitting}
              title="标记为转账：选择方向 + 对方账户"
              className="text-[10px] px-2 py-1 rounded-md text-blue-600 dark:text-blue-400 hover:bg-blue-500/10 transition-colors disabled:opacity-50"
            >
              这是转账…
            </button>
          )}
        </div>
        {error && (
          <div className="text-[10px] text-destructive mt-1">{error}</div>
        )}
      </td>
      <CategoryScopeDialog
        open={scopeDialogOpen}
        txId={tx.id}
        newCategoryId={pickedCat}
        initialNote={tx.user_note}
        onConfirm={handleScopeConfirm}
        onClose={() => { if (!submitting) setScopeDialogOpen(false); }}
      />
    </tr>
  );
}

/** Safely parse `metadata_json` returned by backend. */
function parseMeta(raw: string | null): Record<string, unknown> | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    return typeof parsed === "object" && parsed !== null ? parsed : null;
  } catch {
    return null;
  }
}

/** Group categories by their parent (only return parents that have at least one child). */
function categoriesByParent(cats: CategoryOut[]): Array<[CategoryOut, CategoryOut[]]> {
  const parents = cats.filter((c) => c.parent_id == null);
  return parents
    .map((p) => [p, cats.filter((c) => c.parent_id === p.id)] as [CategoryOut, CategoryOut[]])
    .filter(([, kids]) => kids.length > 0);
}
