"use client";

import { useState } from "react";
import { mutate as swrMutate } from "swr";
import {
  ApiError,
  type ApplyScope,
  type CategoryOut,
  type TransactionOut,
  updateTransaction,
} from "@/lib/api";
import { CategoryScopeDialog } from "@/components/category-scope-dialog";
import { cn } from "@/lib/utils";

/**
 * Compact "click to change category" button. Picking a different category
 * opens the scope dialog so the user can decide how the change propagates
 * (single / all-same-name / disable-rule), with an optional note attached.
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
  // Pending pick — shown in scope dialog. null id = "未分类" (clearing).
  const [pending, setPending] = useState<{
    categoryId: number | null;
    type?: string;
  } | null>(null);

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

  // User picked a category from the dropdown — stage it and open the
  // scope dialog. We don't PATCH yet; the dialog confirms scope + note.
  const handlePick = (id: number | null) => {
    setError(null);
    if (id === tx.category_id) {
      setEditing(false);
      return;
    }
    let nextType: string | undefined;
    if (id !== null) {
      const picked = categories.find((c) => c.id === id);
      if (picked && picked.kind !== tx.type) {
        // User re-classified across kinds (e.g. expense → income). Flip the
        // type so cash-flow/breakdown/inbox re-bucket this tx correctly.
        nextType = picked.kind;
      }
    }
    setPending({ categoryId: id, type: nextType });
  };

  const handleScopeConfirm = async (scope: ApplyScope, note: string | null) => {
    if (!pending) return;
    setSaving(true);
    try {
      const payload: {
        category_id: number | null;
        type?: string;
        user_note?: string | null;
      } = { category_id: pending.categoryId };
      if (pending.type) payload.type = pending.type;
      if ((note ?? null) !== (tx.user_note ?? null)) payload.user_note = note;
      await updateTransaction(tx.id, payload, scope);
      setPending(null);
      setEditing(false);
      refreshAfter();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const dialog = pending !== null && (
    <CategoryScopeDialog
      open={true}
      txId={tx.id}
      newCategoryId={pending.categoryId}
      initialNote={tx.user_note}
      onConfirm={handleScopeConfirm}
      onClose={() => {
        if (!saving) setPending(null);
      }}
    />
  );

  if (editing) {
    return (
      <div className="inline-flex flex-col gap-0.5">
        <select
          autoFocus
          disabled={saving}
          value={pending?.categoryId ?? tx.category_id ?? ""}
          onChange={(e) => handlePick(e.target.value ? Number(e.target.value) : null)}
          onBlur={() => !saving && pending === null && setEditing(false)}
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
        {dialog}
      </div>
    );
  }

  const label = displayLabel ?? tx.category_name ?? "未分类";

  if (variant === "icon") {
    return (
      <button
        onClick={() => setEditing(true)}
        title="点击修改分类"
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
      title="点击修改分类"
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
