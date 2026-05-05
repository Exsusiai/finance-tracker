"use client";

import { useState } from "react";
import { mutate as swrMutate } from "swr";
import {
  ApiError,
  type CategoryOut,
  type TransactionOut,
  updateTransaction,
} from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Compact "click to change category" button. Used in any tx list row to let
 * the user reclassify a transaction inline. Picking a category triggers the
 * backend's update path which:
 *   - learns a new rule from the description (`learn_from_user_assignment`)
 *   - cascades the new category to ALL other pending tx with the same desc
 *     (`apply_to_similar_pending`) — so the user only has to fix one of N
 *     identical rows
 *   - re-computes cash-flow snapshots for affected months
 *
 * After success we invalidate inbox / cashflow / balances / transactions
 * SWR caches so every page that's currently visible refreshes.
 */
interface Props {
  tx: TransactionOut;
  /** Full category list — we filter by `tx.type` ourselves. */
  categories: CategoryOut[];
  /** Optional: parent triggers a custom refresh after a successful change. */
  onChanged?: () => void;
  /** "label" = pill button (existing label / 未分类 link); "icon" = small ✎ next to text */
  variant?: "label" | "icon";
  /** Override the displayed text (used when caller wants the parent component
      to show its own label e.g. "≈ ¥…"). Falls back to `tx.category_name`. */
  displayLabel?: string;
}

export function InlineCategoryPicker({
  tx,
  categories,
  onChanged,
  variant = "label",
  displayLabel,
}: Props) {
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Cross-kind switchable: list ALL of expense / income / transfer in one
  // dropdown, prefixed with their kind so the user can re-classify a row's
  // very nature (e.g. "this isn't a refund expense — it's an income"). When
  // they pick a category whose `kind` differs from `tx.type`, we send `type`
  // alongside `category_id` so the backend flips it atomically.
  const KIND_LABEL: Record<string, string> = {
    expense: "支出",
    income: "收入",
    transfer: "转账",
  };
  const KIND_ORDER = ["expense", "income", "transfer"] as const;
  const grouped = KIND_ORDER.flatMap((kind) =>
    categories
      .filter((c) => c.kind === kind && c.parent_id === null)
      .map((parent) => ({
        kind,
        parent,
        kids: categories.filter((c) => c.kind === kind && c.parent_id === parent.id),
      }))
      .filter((g) => g.kids.length > 0),
  );

  const refreshAfter = () => {
    swrMutate(
      (k) =>
        typeof k === "string" &&
        (k.startsWith("transactions") ||
          k.startsWith("inbox") ||
          k.startsWith("cashflow") ||
          k.startsWith("balances")),
      undefined,
      { revalidate: true },
    );
    if (onChanged) onChanged();
  };

  const handlePick = async (id: number | null) => {
    setError(null);
    setSaving(true);
    try {
      const payload: { category_id: number | null; type?: string } = { category_id: id };
      if (id !== null) {
        const picked = categories.find((c) => c.id === id);
        if (picked && picked.kind !== tx.type) {
          // User re-classified across kinds (e.g. expense → income). Flip the
          // type so cash-flow/breakdown/inbox re-bucket this tx correctly.
          payload.type = picked.kind;
        }
      }
      await updateTransaction(tx.id, payload);
      setEditing(false);
      refreshAfter();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  if (editing) {
    return (
      <div className="inline-flex flex-col gap-0.5">
        <select
          autoFocus
          disabled={saving}
          value={tx.category_id ?? ""}
          onChange={(e) => handlePick(e.target.value ? Number(e.target.value) : null)}
          onBlur={() => !saving && setEditing(false)}
          className="px-1.5 py-0.5 text-xs rounded border border-primary bg-background focus:outline-none focus:ring-1 focus:ring-ring max-w-[200px]"
        >
          <option value="">— 未分类 —</option>
          {grouped.map((g) => (
            <optgroup key={`${g.kind}-${g.parent.id}`} label={`${KIND_LABEL[g.kind]} · ${g.parent.name}`}>
              {g.kids.map((k) => (
                <option key={k.id} value={k.id}>
                  {k.name}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        {error && <span className="text-[10px] text-destructive">{error}</span>}
      </div>
    );
  }

  const label = displayLabel ?? tx.category_name ?? "未分类";

  if (variant === "icon") {
    return (
      <button
        onClick={() => setEditing(true)}
        title="点击修改分类（系统会自动归类同描述的所有条目）"
        className={cn(
          "text-[10px] px-1 py-0 rounded text-muted-foreground hover:text-primary transition-colors",
        )}
      >
        ✎
      </button>
    );
  }

  return tx.category_name ? (
    <button
      onClick={() => setEditing(true)}
      title="点击修改分类（系统会自动归类同描述的所有条目）"
      className="inline-block px-2 py-0.5 text-xs rounded-full bg-muted text-foreground hover:bg-primary/10 hover:text-primary transition-colors cursor-pointer"
    >
      {label}
    </button>
  ) : (
    <button
      onClick={() => setEditing(true)}
      title="点击设置分类"
      className="text-muted-foreground text-xs hover:text-primary cursor-pointer underline decoration-dotted"
    >
      {label}
    </button>
  );
}
