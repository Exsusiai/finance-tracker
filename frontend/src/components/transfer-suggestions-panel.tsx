"use client";

import { useState } from "react";
import { mutate as swrMutate } from "swr";
import { useAccounts, useTransferSuggestions } from "@/lib/hooks";
import { ApiError, markAsTransfer, type TransferSuggestion } from "@/lib/api";
import { cn, formatCurrency, formatDate } from "@/lib/utils";
import { LoadingSpinner } from "@/components/ui-common";

/**
 * Mid-confidence transfer pair candidates (score 50–74) the matcher couldn't
 * auto-confirm. The user reviews each side-by-side and confirms / dismisses.
 */
export function TransferSuggestionsPanel() {
  const { data: suggestions, isLoading, mutate: refresh } = useTransferSuggestions();
  const { data: accounts } = useAccounts(false);

  const accountName = (id: number) => accounts?.find((a) => a.id === id)?.name ?? `#${id}`;

  const refreshAll = () => {
    refresh();
    swrMutate(
      (k) =>
        typeof k === "string" &&
        (k.startsWith("transactions") || k.startsWith("inbox") ||
         k.startsWith("balances") || k.startsWith("cashflow")),
      undefined,
      { revalidate: true },
    );
  };

  if (isLoading) return <LoadingSpinner />;
  if (!suggestions || suggestions.length === 0) {
    return (
      <div className="rounded-xl border border-border bg-card p-12 text-center">
        <p className="text-base font-medium mb-1">无待确认的转账建议</p>
        <p className="text-sm text-muted-foreground">
          系统没有发现 50–74 分的悬空配对。高置信度（≥75）的已自动配对完成。
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        以下是系统识别但置信度不够的转账候选。请逐对确认或忽略。
      </p>
      <div className="space-y-2">
        {suggestions.map((s) => (
          <SuggestionCard
            key={`${s.out_transaction_id}-${s.in_transaction_id}`}
            suggestion={s}
            outAccountName={accountName(s.out_account_id)}
            inAccountName={accountName(s.in_account_id)}
            onDone={refreshAll}
          />
        ))}
      </div>
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
