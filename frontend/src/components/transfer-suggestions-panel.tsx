"use client";

import { useState } from "react";
import {
  invalidateTransactionGraph,
  useAccounts,
  useTransferSuggestions,
  useUnpairedTransfers,
} from "@/lib/hooks";
import {
  ApiError,
  markAsTransfer,
  type TransferSuggestion,
  type UnpairedTransfer,
} from "@/lib/api";
import { cn, formatCurrency, formatDate } from "@/lib/utils";
import { LoadingSpinner } from "@/components/ui-common";

/**
 * Two-section panel:
 *   1. Mid-confidence candidate pairs (50–74) the matcher couldn't auto-confirm
 *   2. Unpaired transfer rows — every `type=transfer` row that still has no
 *      counter account bound. User picks the destination account → backend
 *      auto-creates the mirror leg so balances reconcile.
 *
 * Section 2 is the cure for banks (e.g. TF Bank) whose statements omit the
 * incoming side of a transfer: the matcher has no counter-leg to find, so
 * the user has to bind it manually.
 */
export function TransferSuggestionsPanel() {
  const { data: suggestions, isLoading: loadingSugs } = useTransferSuggestions();
  const { data: unpaired, isLoading: loadingUnpaired } = useUnpairedTransfers();
  const { data: accounts } = useAccounts(false);

  const accountName = (id: number) => accounts?.find((a) => a.id === id)?.name ?? `#${id}`;

  const isLoading = loadingSugs || loadingUnpaired;
  const refreshAll = () => invalidateTransactionGraph();

  // Only show the spinner on the *initial* load. Once we have data, keep
  // it on screen during background revalidation — flipping back to a
  // full-page spinner mid-confirm scrolls the user back to the top.
  if (isLoading && !suggestions && !unpaired) return <LoadingSpinner />;

  const hasSuggestions = (suggestions?.length ?? 0) > 0;
  const hasUnpaired = (unpaired?.length ?? 0) > 0;

  if (!hasSuggestions && !hasUnpaired) {
    return (
      <div className="rounded-xl border border-border bg-card p-12 text-center">
        <p className="text-base font-medium mb-1">所有转账都已配对</p>
        <p className="text-sm text-muted-foreground">
          没有未配对的转账，也没有待确认的候选对。
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {hasUnpaired && (
        <section>
          <div className="mb-2">
            <h3 className="text-sm font-semibold text-foreground">
              未配对转账
              <span className="ml-2 text-xs font-normal text-muted-foreground">
                {unpaired!.length} 笔需要绑定对手账户
              </span>
            </h3>
            <p className="text-xs text-muted-foreground mt-0.5">
              这些转账目前只有单边记录（常见原因：信用卡账单不显示收款侧）。
              选定对手账户后，系统会在那边自动生成镜像，保证余额对齐。
            </p>
          </div>
          <div className="space-y-2">
            {unpaired!.map((t) => (
              <UnpairedCard
                key={t.transaction_id}
                tx={t}
                accounts={accounts ?? []}
                onDone={refreshAll}
              />
            ))}
          </div>
        </section>
      )}

      {hasSuggestions && (
        <section>
          <div className="mb-2">
            <h3 className="text-sm font-semibold text-foreground">
              候选配对
              <span className="ml-2 text-xs font-normal text-muted-foreground">
                {suggestions!.length} 组待确认
              </span>
            </h3>
            <p className="text-xs text-muted-foreground mt-0.5">
              系统识别了双边但置信度不够（50–74 分）。逐对确认即可。
            </p>
          </div>
          <div className="space-y-2">
            {suggestions!.map((s) => (
              <SuggestionCard
                key={`${s.out_transaction_id}-${s.in_transaction_id}`}
                suggestion={s}
                outAccountName={accountName(s.out_account_id)}
                inAccountName={accountName(s.in_account_id)}
                onDone={refreshAll}
              />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

// ─── Unpaired single-leg card ─────────────────────────────────────────

interface UnpairedCardProps {
  tx: UnpairedTransfer;
  accounts: Array<{ id: number; name: string; currency: string }>;
  onDone: () => void;
}

function UnpairedCard({ tx, accounts, onDone }: UnpairedCardProps) {
  const [counterAccountId, setCounterAccountId] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Default direction from metadata; fall back: if amount > 0 logically we
  // can't tell, so default 'out' (most common single-leg case).
  const direction: "in" | "out" = tx.transfer_direction === "in" ? "in" : "out";

  const handleConfirm = async () => {
    if (counterAccountId == null) {
      setError("请先选择对手账户");
      return;
    }
    setError(null);
    try {
      setSubmitting(true);
      await markAsTransfer(tx.transaction_id, {
        counterAccountId,
        direction,
        categoryId: tx.category_id ?? undefined,
      });
      onDone();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "绑定失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-3">
      <div className="flex flex-wrap items-center gap-2 mb-2">
        <span className="text-base font-semibold tabular-nums">
          {direction === "out" ? "-" : "+"}
          {formatCurrency(parseFloat(tx.amount), tx.currency)}
        </span>
        <span className="text-xs text-muted-foreground">
          {formatDate(tx.occurred_at)} · {tx.account_name ?? `#${tx.account_id}`}
        </span>
        {tx.category_name && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground">
            {tx.category_name}
          </span>
        )}
      </div>
      <div className="text-xs text-foreground truncate mb-2" title={tx.description ?? ""}>
        {tx.description || tx.raw_description || "—"}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] text-muted-foreground shrink-0">对手账户:</span>
        <select
          value={counterAccountId ?? ""}
          onChange={(e) => setCounterAccountId(e.target.value ? Number(e.target.value) : null)}
          disabled={submitting}
          className={cn(
            "px-2 py-1 text-xs rounded-md border bg-background focus:outline-none focus:ring-2 focus:ring-ring max-w-[220px]",
            counterAccountId == null ? "border-amber-500/50" : "border-border",
          )}
        >
          <option value="">— 请选择 —</option>
          {accounts
            .filter((a) => a.id !== tx.account_id)
            .map((a) => (
              <option key={a.id} value={a.id}>
                {a.name} ({a.currency})
              </option>
            ))}
        </select>
        <button
          onClick={handleConfirm}
          disabled={submitting || counterAccountId == null}
          className="text-xs px-3 py-1 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50 ml-auto"
        >
          {submitting ? "绑定中…" : "✓ 绑定并生成对手腿"}
        </button>
      </div>
      {error && <p className="mt-1 text-[11px] text-destructive">{error}</p>}
    </div>
  );
}

interface CardProps {
  suggestion: TransferSuggestion;
  outAccountName: string;
  inAccountName: string;
  onDone: () => void;
}

function SuggestionCard({ suggestion: s, outAccountName, inAccountName, onDone }: CardProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleConfirm = async () => {
    setError(null);
    try {
      setSubmitting(true);
      await markAsTransfer(s.out_transaction_id, { counterTransactionId: s.in_transaction_id, direction: "out" });
      // onDone already calls invalidateTransactionGraph (via refreshAll);
      // double-firing it would trigger a second SWR revalidation round.
      onDone();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "确认失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="rounded-xl border border-border bg-card p-4">
      <div className="flex items-center justify-between gap-3 mb-3">
        <div className="flex items-center gap-2">
          <span className="text-base font-semibold tabular-nums">
            {formatCurrency(s.amount, s.currency)}
          </span>
          <span className={cn(
            "text-[10px] px-2 py-0.5 rounded-full font-medium",
            s.score >= 65 ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                          : "bg-amber-500/10 text-amber-600 dark:text-amber-400",
          )}>
            置信度 {s.score}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleConfirm}
            disabled={submitting}
            className="text-xs px-3 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
          >
            {submitting ? "确认中…" : "✓ 确认配对"}
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-[1fr_auto_1fr] gap-3 items-center text-xs">
        {/* Outgoing leg */}
        <div className="rounded-lg border border-rose-500/20 bg-rose-500/5 p-2.5">
          <div className="flex items-center justify-between mb-1">
            <span className="font-medium text-foreground">{outAccountName}</span>
            <span className="text-rose-600 dark:text-rose-400">-{formatCurrency(s.amount, s.currency)}</span>
          </div>
          <div className="text-muted-foreground truncate" title={s.out_description ?? ""}>
            {s.out_description || "—"}
          </div>
          <div className="text-[10px] text-muted-foreground mt-0.5">
            {formatDate(s.out_date)} · tx#{s.out_transaction_id}
          </div>
        </div>

        <span className="text-muted-foreground text-center">→</span>

        {/* Incoming leg */}
        <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-2.5">
          <div className="flex items-center justify-between mb-1">
            <span className="font-medium text-foreground">{inAccountName}</span>
            <span className="text-emerald-600 dark:text-emerald-400">+{formatCurrency(s.amount, s.currency)}</span>
          </div>
          <div className="text-muted-foreground truncate" title={s.in_description ?? ""}>
            {s.in_description || "—"}
          </div>
          <div className="text-[10px] text-muted-foreground mt-0.5">
            {formatDate(s.in_date)} · tx#{s.in_transaction_id}
          </div>
        </div>
      </div>

      {s.reasons.length > 0 && (
        <p className="mt-2 text-[10px] text-muted-foreground">
          匹配依据: {s.reasons.join(" · ")}
        </p>
      )}

      {error && (
        <p className="mt-2 text-xs text-destructive">{error}</p>
      )}
    </div>
  );
}
