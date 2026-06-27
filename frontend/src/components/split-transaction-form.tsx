"use client";

import { useMemo, useState } from "react";
import {
  type TransactionOut,
  type CategoryOut,
  type SplitLineInput,
  splitTransaction,
  ApiError,
} from "@/lib/api";
import { invalidateTransactionGraph } from "@/lib/hooks";
import { cn } from "@/lib/utils";

interface SplitTransactionFormProps {
  tx: TransactionOut;
  categories: CategoryOut[];
  onClose: () => void;
  onSuccess: () => void;
}

interface DraftLine {
  amount: string;
  type: string;
  category_id: number | null;
  description: string;
}

const LINE_TYPES = [
  { value: "expense", label: "支出" },
  { value: "income", label: "收入" },
  { value: "transfer", label: "转账(如借出)" },
];

function round2(n: number): number {
  return Math.round((n + Number.EPSILON) * 100) / 100;
}

/** Split a transaction into N lines that must sum to the original amount.
 *  AA use case: €100 餐饮 → €20 餐饮 + €80 借出. */
export function SplitTransactionForm({ tx, categories, onClose, onSuccess }: SplitTransactionFormProps) {
  const original = round2(parseFloat(tx.amount));
  const [lines, setLines] = useState<DraftLine[]>([
    { amount: "", type: tx.type === "income" ? "income" : "expense", category_id: tx.category_id, description: "" },
    { amount: "", type: "transfer", category_id: null, description: "" },
  ]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const total = useMemo(
    () => round2(lines.reduce((s, l) => s + (parseFloat(l.amount) || 0), 0)),
    [lines],
  );
  const remaining = round2(original - total);
  const balanced = remaining === 0 && lines.every((l) => (parseFloat(l.amount) || 0) > 0);

  const setLine = (i: number, patch: Partial<DraftLine>) =>
    setLines((ls) => ls.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  const addLine = () =>
    setLines((ls) => [...ls, { amount: "", type: "expense", category_id: null, description: "" }]);
  const removeLine = (i: number) =>
    setLines((ls) => (ls.length > 2 ? ls.filter((_, j) => j !== i) : ls));

  const handleSubmit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const payload: SplitLineInput[] = lines.map((l) => ({
        amount: String(parseFloat(l.amount)),
        type: l.type,
        category_id: l.category_id,
        description: l.description || null,
      }));
      await splitTransaction(tx.id, payload);
      invalidateTransactionGraph();
      onSuccess();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "拆分失败，请重试");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-base font-semibold">拆分交易</h3>
        <span className="text-sm text-muted-foreground">原额 {original.toFixed(2)} {tx.currency}</span>
      </div>
      <p className="text-xs text-muted-foreground">
        把这一笔拆成几条(各条之和须等于原额)。例：100 元聚餐 → 20「餐饮」+ 80「借出」；
        别人转还你的钱在交易列表里改成「还款收回」。
      </p>

      <div className="space-y-3">
        {lines.map((l, i) => {
          const cats = categories.filter((c) => c.kind === l.type);
          return (
            <div key={i} className="rounded-lg border border-border p-3 space-y-2">
              <div className="flex gap-2">
                <input
                  type="number"
                  step="0.01"
                  inputMode="decimal"
                  value={l.amount}
                  onChange={(e) => setLine(i, { amount: e.target.value })}
                  placeholder="金额"
                  className="w-28 px-2 py-1.5 text-sm rounded-md border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
                />
                <select
                  value={l.type}
                  onChange={(e) => setLine(i, { type: e.target.value, category_id: null })}
                  className="px-2 py-1.5 text-sm rounded-md border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
                >
                  {LINE_TYPES.map((t) => (
                    <option key={t.value} value={t.value}>{t.label}</option>
                  ))}
                </select>
                <select
                  value={l.category_id ?? ""}
                  onChange={(e) => setLine(i, { category_id: e.target.value ? Number(e.target.value) : null })}
                  className="flex-1 min-w-0 px-2 py-1.5 text-sm rounded-md border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
                >
                  <option value="">分类…</option>
                  {cats.map((c) => (
                    <option key={c.id} value={c.id}>{c.name}</option>
                  ))}
                </select>
                {lines.length > 2 && (
                  <button
                    onClick={() => removeLine(i)}
                    className="px-2 rounded-md text-muted-foreground hover:text-destructive hover:bg-destructive/5"
                    title="删除此行"
                  >
                    ×
                  </button>
                )}
              </div>
              <input
                type="text"
                value={l.description}
                onChange={(e) => setLine(i, { description: e.target.value })}
                placeholder="备注(可选)"
                className="w-full px-2 py-1.5 text-xs rounded-md border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
              />
            </div>
          );
        })}
      </div>

      <button onClick={addLine} className="text-sm text-primary hover:underline">+ 添加一行</button>

      <div className={cn("text-sm font-medium", remaining === 0 ? "text-emerald-600 dark:text-emerald-400" : "text-amber-600 dark:text-amber-400")}>
        已分配 {total.toFixed(2)} / {original.toFixed(2)}
        {remaining === 0 ? "✓ 已平" : `还差 ${remaining.toFixed(2)}`}
      </div>

      {error && (
        <div className="p-3 rounded-lg bg-destructive/10 border border-destructive/20 text-sm text-destructive">{error}</div>
      )}

      <div className="flex gap-2">
        <button
          onClick={handleSubmit}
          disabled={!balanced || submitting}
          className="flex-1 px-4 py-2.5 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
        >
          {submitting ? "拆分中…" : "确认拆分"}
        </button>
        <button
          onClick={onClose}
          className="px-4 py-2.5 text-sm font-medium rounded-lg border border-border hover:bg-muted transition-colors"
        >
          取消
        </button>
      </div>
    </div>
  );
}
