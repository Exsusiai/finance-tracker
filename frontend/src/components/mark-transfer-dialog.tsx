"use client";

import { useEffect, useMemo, useState } from "react";
import {
  useAccounts,
  useCategories,
  useTransactions,
  invalidateTransactionGraph,
} from "@/lib/hooks";
import { ApiError, markAsTransfer, type TransactionOut } from "@/lib/api";
import { cn, formatCurrency, formatDate } from "@/lib/utils";

/**
 * Modal: prompt the user to confirm direction + counter-account when marking
 * a single tx as a transfer. Shows candidate counter-leg transactions
 * (same amount, ±3 days) so user can one-click pair.
 */
interface Props {
  tx: TransactionOut;
  onClose: () => void;
  onSuccess: () => void;
}

export function MarkTransferDialog({ tx, onClose, onSuccess }: Props) {
  const [direction, setDirection] = useState<"out" | "in" | null>(null);
  const [counterAccountId, setCounterAccountId] = useState<number | "external" | null>(null);
  const [counterTxId, setCounterTxId] = useState<number | null>(null);
  // Pre-fill if tx already has a transfer-kind category assigned (e.g. matcher
  // resolved 跨行划转), otherwise force the user to pick one before submitting.
  const [categoryId, setCategoryId] = useState<number | null>(
    tx.category_id ?? null,
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: accounts } = useAccounts(true);
  const { data: categories } = useCategories();
  const transferCategories = useMemo(
    () => (categories ?? []).filter((c) => c.kind === "transfer"),
    [categories],
  );

  // Pull candidate counter-leg transactions: same amount, opposite direction,
  // within ±3 days.
  const range = dateRange(tx.occurred_at, 3);
  const oppositeType = tx.type === "expense" ? "income" : tx.type === "income" ? "expense" : "";
  const { data: candResp } = useTransactions({
    type: oppositeType || undefined,
    from_date: range.from,
    to_date: range.to,
    limit: 50,
  });
  const candidates = useMemo(() => {
    const list = candResp?.data ?? [];
    return list.filter(
      (c) =>
        c.id !== tx.id &&
        c.account_id !== tx.account_id &&
        c.currency === tx.currency &&
        Math.abs(parseFloat(c.amount) - parseFloat(tx.amount)) < 0.01,
    );
  }, [candResp, tx]);

  // Sensible defaults: pick direction from current tx.type, pick the most
  // likely candidate (closest date) by default.
  useEffect(() => {
    if (tx.type === "expense") setDirection("out");
    else if (tx.type === "income") setDirection("in");
  }, [tx.type]);

  const handleConfirm = async () => {
    if (!direction) {
      setError("请选择「转出」或「转入」");
      return;
    }
    if (categoryId == null) {
      setError("请选择转账分类");
      return;
    }
    setError(null);
    try {
      setSubmitting(true);
      // Only send counter_account_id when (a) we have one, (b) it's an
      // internal account, and (c) we don't already have a counter tx.
      // Sending it together with counterTransactionId would conflict — the
      // backend uses counter_transaction_id when both are present.
      const internalCounterAccount =
        typeof counterAccountId === "number" ? counterAccountId : undefined;
      await markAsTransfer(tx.id, {
        counterTransactionId: counterTxId ?? undefined,
        counterAccountId: counterTxId ? undefined : internalCounterAccount,
        direction: direction ?? undefined,
        categoryId,
      });
      invalidateTransactionGraph();
      onSuccess();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "标记失败");
    } finally {
      setSubmitting(false);
    }
  };

  const myAccountName = accounts?.find((a) => a.id === tx.account_id)?.name ?? `#${tx.account_id}`;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="fixed inset-0 bg-black/50 backdrop-blur-sm" onClick={() => !submitting && onClose()} />
      <div className="relative w-full max-w-lg rounded-xl border border-border bg-card p-5 shadow-xl max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-base font-semibold">标记为转账</h2>
          <button onClick={onClose} disabled={submitting} className="text-muted-foreground hover:text-foreground">
            ✕
          </button>
        </div>

        {/* Tx summary */}
        <div className="mb-4 p-3 rounded-lg bg-muted/40 border border-border">
          <p className="text-xs text-muted-foreground mb-0.5">{myAccountName} · {formatDate(tx.occurred_at)}</p>
          <p className="text-sm font-medium">{tx.description || tx.raw_description || "—"}</p>
          <p className={cn(
            "text-base tabular-nums font-semibold mt-1",
            tx.type === "income" ? "text-emerald-600" : "text-rose-600",
          )}>
            {tx.type === "income" ? "+" : "-"}{formatCurrency(Math.abs(parseFloat(tx.amount)), tx.currency)}
          </p>
        </div>

        {/* Direction */}
        <div className="mb-4">
          <p className="text-xs font-medium mb-1.5">方向 <span className="text-destructive">*</span></p>
          <div className="grid grid-cols-2 gap-2">
            <button
              type="button"
              onClick={() => setDirection("out")}
              className={cn(
                "px-3 py-2 text-sm rounded-md border-2 transition-colors",
                direction === "out"
                  ? "border-rose-500/60 bg-rose-500/10 text-rose-600 dark:text-rose-400"
                  : "border-border text-muted-foreground hover:border-rose-500/30",
              )}
            >
              转出 ↗
              <span className="block text-[10px] opacity-70 mt-0.5">
                这笔钱从 {myAccountName} 出
              </span>
            </button>
            <button
              type="button"
              onClick={() => setDirection("in")}
              className={cn(
                "px-3 py-2 text-sm rounded-md border-2 transition-colors",
                direction === "in"
                  ? "border-emerald-500/60 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                  : "border-border text-muted-foreground hover:border-emerald-500/30",
              )}
            >
              转入 ↘
              <span className="block text-[10px] opacity-70 mt-0.5">
                这笔钱到 {myAccountName}
              </span>
            </button>
          </div>
        </div>

        {/* Transfer category — required */}
        <div className="mb-4">
          <p className="text-xs font-medium mb-1.5">
            转账分类 <span className="text-destructive">*</span>
          </p>
          <select
            value={categoryId ?? ""}
            onChange={(e) => setCategoryId(e.target.value ? Number(e.target.value) : null)}
            className={cn(
              "w-full px-2.5 py-1.5 text-sm rounded-md border bg-background focus:outline-none focus:ring-2 focus:ring-ring",
              categoryId == null ? "border-amber-500/50" : "border-border",
            )}
          >
            <option value="">— 请选择 —</option>
            {groupedTransferCategories(transferCategories).map(([parent, kids]) => (
              <optgroup key={parent.id} label={parent.name}>
                {kids.map((k) => (
                  <option key={k.id} value={k.id}>
                    {k.name}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
          <p className="mt-1 text-[10px] text-muted-foreground">
            内部储蓄 = 同银行子账户互转 · 跨行划转 = 不同银行间 · 信用卡还款 = 银行→信用卡
          </p>
        </div>

        {/* Counter-account picker */}
        <div className="mb-4">
          <p className="text-xs font-medium mb-1.5">对方账户</p>
          <select
            value={counterAccountId === "external" ? "external" : (counterAccountId ?? "")}
            onChange={(e) => {
              const v = e.target.value;
              setCounterAccountId(v === "external" ? "external" : v ? Number(v) : null);
              setCounterTxId(null);
            }}
            className="w-full px-2.5 py-1.5 text-sm rounded-md border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">— 未选（仅标记，不配对）—</option>
            {accounts?.filter((a) => a.id !== tx.account_id).map((a) => (
              <option key={a.id} value={a.id}>
                {a.name} ({a.currency})
              </option>
            ))}
            <option value="external">外部 / 未知账户</option>
          </select>
        </div>

        {/* Candidate counter-leg picker */}
        {typeof counterAccountId === "number" && (
          <div className="mb-4">
            <p className="text-xs font-medium mb-1.5">
              候选对手交易（金额一致 + 同方向相反 + ±3 天）
            </p>
            <div className="space-y-1.5 max-h-44 overflow-y-auto">
              {candidates.filter((c) => c.account_id === counterAccountId).length === 0 ? (
                <p className="text-[11px] text-muted-foreground italic">
                  在 ±3 天内未找到金额一致的对手交易
                </p>
              ) : (
                candidates
                  .filter((c) => c.account_id === counterAccountId)
                  .map((c) => (
                    <button
                      key={c.id}
                      type="button"
                      onClick={() => setCounterTxId(c.id === counterTxId ? null : c.id)}
                      className={cn(
                        "w-full text-left px-2.5 py-1.5 rounded-md border transition-colors",
                        counterTxId === c.id
                          ? "border-primary bg-primary/10"
                          : "border-border hover:bg-muted/40",
                      )}
                    >
                      <div className="flex justify-between gap-2 text-xs">
                        <span className="truncate">{c.description || "—"}</span>
                        <span className="tabular-nums shrink-0">
                          {c.type === "income" ? "+" : "-"}{formatCurrency(Math.abs(parseFloat(c.amount)), c.currency)}
                        </span>
                      </div>
                      <p className="text-[10px] text-muted-foreground mt-0.5">
                        {formatDate(c.occurred_at)} · tx#{c.id}
                      </p>
                    </button>
                  ))
              )}
            </div>
          </div>
        )}

        {error && (
          <p className="mb-3 text-xs text-destructive">{error}</p>
        )}

        <div className="flex gap-2 pt-2 border-t border-border">
          <button
            onClick={onClose}
            disabled={submitting}
            className="flex-1 px-3 py-2 text-sm font-medium rounded-md border border-border hover:bg-muted transition-colors disabled:opacity-50"
          >
            取消
          </button>
          <button
            onClick={handleConfirm}
            disabled={submitting || !direction || categoryId == null}
            className="flex-1 px-3 py-2 text-sm font-medium rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
          >
            {submitting ? "保存中…" : "确认"}
          </button>
        </div>
      </div>
    </div>
  );
}

/** Group flat transfer-kind categories by their parent for `<optgroup>`. */
function groupedTransferCategories(
  cats: { id: number; name: string; parent_id: number | null }[],
): Array<[
  { id: number; name: string },
  { id: number; name: string }[],
]> {
  const parents = cats.filter((c) => c.parent_id == null);
  return parents
    .map((p) => [p, cats.filter((c) => c.parent_id === p.id)] as [
      { id: number; name: string },
      { id: number; name: string }[],
    ])
    .filter(([, kids]) => kids.length > 0);
}

function dateRange(occurred_at: string, deltaDays: number): { from: string; to: string } {
  const d = new Date(occurred_at);
  const from = new Date(d.getTime() - deltaDays * 86400000);
  const to = new Date(d.getTime() + deltaDays * 86400000);
  const iso = (x: Date) => x.toISOString().slice(0, 10);
  return { from: iso(from), to: iso(to) };
}
