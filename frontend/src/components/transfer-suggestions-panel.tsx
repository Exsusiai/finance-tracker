"use client";

import { useEffect, useState } from "react";
import {
  invalidateTransactionGraph,
  useAccounts,
  useTransferSuggestions,
  useUnpairedTransfers,
} from "@/lib/hooks";
import {
  ApiError,
  type CounterLegCandidate,
  fetchCounterLegCandidates,
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
  const [pickerOpen, setPickerOpen] = useState(false);

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

  const handleManualPair = async (counterTxId: number, amountTolerance: string) => {
    setError(null);
    try {
      setSubmitting(true);
      await markAsTransfer(tx.transaction_id, {
        counterTransactionId: counterTxId,
        direction,
        categoryId: tx.category_id ?? undefined,
        amountTolerance,
      });
      setPickerOpen(false);
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
          className="text-xs px-3 py-1 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
        >
          {submitting ? "绑定中…" : "✓ 绑定并生成对手腿"}
        </button>
        <button
          type="button"
          onClick={() => setPickerOpen(true)}
          disabled={submitting}
          title="从其他账户里挑一笔已存在的交易作为对手腿（适用于自动匹配漏掉的对子）"
          className="text-xs px-3 py-1 rounded-md border border-border bg-card hover:bg-muted transition-colors disabled:opacity-50 ml-auto"
        >
          🔗 选对手腿…
        </button>
      </div>
      {error && <p className="mt-1 text-[11px] text-destructive">{error}</p>}

      {pickerOpen && (
        <CounterLegPickerDialog
          srcTx={tx}
          onClose={() => setPickerOpen(false)}
          onPick={handleManualPair}
          submitting={submitting}
        />
      )}
    </div>
  );
}

// ─── Counter-leg picker dialog ────────────────────────────────────────

interface CounterLegPickerProps {
  srcTx: UnpairedTransfer;
  onClose: () => void;
  // Includes the tolerance the user picked so the bind call can accept
  // pairs that differ by more than 0.01 (paying-for-friends scenarios).
  onPick: (counterTxId: number, amountTolerance: string) => Promise<void> | void;
  submitting: boolean;
}

// Format the signed amount diff between candidate and source. "0E-8" /
// 0 → "完全一致"; otherwise "+12.34" / "-3.50".
function _formatAmountDiff(amountDiff: string, currency: string): string {
  const n = parseFloat(amountDiff);
  if (!Number.isFinite(n) || n === 0) return "完全一致";
  const sign = n > 0 ? "+" : "−";
  const abs = Math.abs(n);
  return `${sign}${formatCurrency(abs, currency)}`;
}

function CounterLegPickerDialog({ srcTx, onClose, onPick, submitting }: CounterLegPickerProps) {
  const [candidates, setCandidates] = useState<CounterLegCandidate[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [windowDays, setWindowDays] = useState(10);
  // Amount tolerance is a string so the user can type partial decimals
  // ("2.", ".5") while editing without losing focus on every keystroke.
  const [tolInput, setTolInput] = useState("0.01");

  // Debounce tolerance changes so we don't fire one request per keystroke.
  const [debouncedTol, setDebouncedTol] = useState(tolInput);
  useEffect(() => {
    const id = setTimeout(() => setDebouncedTol(tolInput), 350);
    return () => clearTimeout(id);
  }, [tolInput]);

  useEffect(() => {
    let cancelled = false;
    setCandidates(null);
    setLoadError(null);
    // Validate before firing — empty / NaN / negative → fall back to 0.01
    const parsed = parseFloat(debouncedTol);
    const tolForRequest =
      Number.isFinite(parsed) && parsed >= 0 ? String(parsed) : "0.01";
    fetchCounterLegCandidates(srcTx.transaction_id, windowDays, tolForRequest)
      .then((rows) => {
        if (!cancelled) setCandidates(rows);
      })
      .catch((e) => {
        if (!cancelled) setLoadError(e instanceof ApiError ? e.message : "加载候选失败");
      });
    return () => {
      cancelled = true;
    };
  }, [srcTx.transaction_id, windowDays, debouncedTol]);

  const handlePick = async (counterTxId: number) => {
    const parsed = parseFloat(debouncedTol);
    const tol = Number.isFinite(parsed) && parsed >= 0 ? String(parsed) : "0.01";
    await onPick(counterTxId, tol);
  };

  const tolNum = parseFloat(tolInput);
  const tolValid = Number.isFinite(tolNum) && tolNum >= 0;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="fixed inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />
      <div className="relative w-full max-w-2xl rounded-xl border border-border bg-card p-5 shadow-xl space-y-3 max-h-[85vh] overflow-y-auto">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h3 className="text-base font-semibold">选择对手腿</h3>
            <p className="text-xs text-muted-foreground mt-0.5">
              其他账户中币种相同、未配对、金额 ±{tolValid ? tolNum : 0.01}、日期在 ±{windowDays} 天内的交易
            </p>
          </div>
          <button
            onClick={onClose}
            className="text-muted-foreground hover:text-foreground text-sm"
            aria-label="关闭"
          >
            ✕
          </button>
        </div>

        <div className="rounded-md bg-muted/40 p-2.5 text-xs">
          <span className="text-muted-foreground">源交易: </span>
          <span className="font-medium tabular-nums">
            {formatCurrency(parseFloat(srcTx.amount), srcTx.currency)}
          </span>
          <span className="text-muted-foreground"> · </span>
          <span>{formatDate(srcTx.occurred_at)}</span>
          <span className="text-muted-foreground"> · </span>
          <span>{srcTx.account_name ?? `#${srcTx.account_id}`}</span>
          {srcTx.description && (
            <div className="mt-1 text-muted-foreground truncate">{srcTx.description}</div>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
          <div className="flex items-center gap-2">
            <label className="text-muted-foreground">日期窗口 (±天):</label>
            {[5, 10, 20, 30].map((d) => (
              <button
                key={d}
                onClick={() => setWindowDays(d)}
                className={cn(
                  "px-2 py-0.5 rounded-md border transition-colors",
                  d === windowDays
                    ? "border-primary bg-primary/10 text-primary font-medium"
                    : "border-border hover:bg-muted",
                )}
              >
                {d}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <label className="text-muted-foreground">金额容差 ±</label>
            <input
              type="number"
              step="0.01"
              min="0"
              value={tolInput}
              onChange={(e) => setTolInput(e.target.value)}
              className={cn(
                "w-24 px-2 py-0.5 rounded-md border bg-background tabular-nums focus:outline-none focus:ring-2 focus:ring-ring",
                tolValid ? "border-border" : "border-destructive/50",
              )}
              title="允许两腿金额差。0.01 = 严格分到分；2 = 帮朋友付钱、对方少给 1-2 块也能配；以此类推。"
            />
            <span className="text-muted-foreground">{srcTx.currency}</span>
            {[0.01, 1, 5, 20].map((preset) => (
              <button
                key={preset}
                onClick={() => setTolInput(String(preset))}
                className={cn(
                  "px-1.5 py-0.5 rounded-md border transition-colors text-[10px]",
                  Math.abs((Number.isFinite(tolNum) ? tolNum : -1) - preset) < 1e-9
                    ? "border-primary bg-primary/10 text-primary font-medium"
                    : "border-border hover:bg-muted",
                )}
              >
                {preset}
              </button>
            ))}
          </div>
        </div>

        {loadError && (
          <div className="rounded-md bg-destructive/10 border border-destructive/30 p-2.5 text-xs text-destructive">
            {loadError}
          </div>
        )}

        {candidates === null && !loadError && (
          <div className="py-8 flex items-center justify-center">
            <LoadingSpinner />
          </div>
        )}

        {candidates !== null && candidates.length === 0 && (
          <div className="py-8 text-center text-sm text-muted-foreground">
            未找到匹配的候选交易。可以扩大日期窗口或金额容差再试。
          </div>
        )}

        {candidates !== null && candidates.length > 0 && (
          <div className="space-y-1.5">
            {candidates.map((c) => {
              const diff = parseFloat(c.amount_diff);
              const isExact = Number.isFinite(diff) && diff === 0;
              const isSyntheticBound = c.status === "synthetic_bound";
              return (
                <button
                  key={c.transaction_id}
                  onClick={() => handlePick(c.transaction_id)}
                  disabled={submitting}
                  className={cn(
                    "w-full text-left rounded-md border bg-background hover:bg-muted/40 p-2.5 transition-colors disabled:opacity-50",
                    isSyntheticBound ? "border-amber-500/40" : "border-border",
                  )}
                >
                  <div className="flex items-center justify-between gap-2 text-xs">
                    <div className="flex items-center gap-2 min-w-0 flex-wrap">
                      <span className="font-medium tabular-nums shrink-0">
                        {formatCurrency(parseFloat(c.amount), c.currency)}
                      </span>
                      <span className="text-muted-foreground shrink-0">
                        {formatDate(c.occurred_at)}
                      </span>
                      {c.days_diff !== null && (
                        <span
                          className={cn(
                            "text-[10px] px-1.5 py-0.5 rounded shrink-0",
                            c.days_diff <= 1
                              ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
                              : c.days_diff <= 5
                                ? "bg-amber-500/15 text-amber-700 dark:text-amber-300"
                                : "bg-muted text-muted-foreground",
                          )}
                        >
                          差 {c.days_diff} 天
                        </span>
                      )}
                      <span
                        className={cn(
                          "text-[10px] px-1.5 py-0.5 rounded shrink-0 tabular-nums",
                          isExact
                            ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
                            : "bg-amber-500/15 text-amber-700 dark:text-amber-300",
                        )}
                        title={isExact ? "金额完全一致" : "金额差额（候选 - 源）"}
                      >
                        {_formatAmountDiff(c.amount_diff, c.currency)}
                      </span>
                      {isSyntheticBound && (
                        <span
                          className="text-[10px] px-1.5 py-0.5 rounded shrink-0 bg-amber-500/15 text-amber-700 dark:text-amber-300"
                          title="此交易当前绑定的是一条由「绑定到账户」生成的合成腿。点击后会先废弃合成腿，再把这两笔真实交易绑成对子。"
                        >
                          ⚠ 含合成腿（点击后会替换）
                        </span>
                      )}
                      <span className="text-muted-foreground truncate">
                        {c.account_name ?? `#${c.account_id}`}
                      </span>
                    </div>
                    <span className="text-[10px] text-muted-foreground shrink-0">
                      type={c.type}
                    </span>
                  </div>
                  <div className="mt-1 text-xs text-foreground/80 truncate" title={c.description ?? ""}>
                    {c.description || c.raw_description || "—"}
                  </div>
                </button>
              );
            })}
          </div>
        )}

        <div className="pt-2 text-[10px] text-muted-foreground border-t border-border">
          点击候选条目即可绑定。绑定后两笔都会标为 transfer，方向沿用源腿。金额容差大于 0.01 时（帮朋友付钱场景）两腿不必精确相等。
        </div>
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
