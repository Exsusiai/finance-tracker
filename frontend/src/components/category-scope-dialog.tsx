"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { type ApplyScope, fetchSimilarCount } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Confirmation dialog shown after the user manually changes a transaction's
 * category. Three propagation choices + an optional note that travels with
 * the same write call (so the user doesn't have to manage two forms).
 *
 * Default: "all" — clicking 确认 without choosing applies to all same-name
 * siblings, matching the rule-learning behavior that existed before this
 * dialog was introduced.
 */

interface Props {
  open: boolean;
  /** Transaction whose category is being changed (used for the preview count). */
  txId: number;
  /** Newly picked category id (used to ask backend how many siblings would change). */
  newCategoryId: number | null;
  /** Initial note value — pre-filled with the tx's existing user_note. */
  initialNote?: string | null;
  /** User confirmed: returns chosen scope + final note text (null = clear). */
  onConfirm: (scope: ApplyScope, note: string | null) => void | Promise<void>;
  onClose: () => void;
}

export function CategoryScopeDialog({
  open,
  txId,
  newCategoryId,
  initialNote,
  onConfirm,
  onClose,
}: Props) {
  const [scope, setScope] = useState<ApplyScope>("all");
  const [note, setNote] = useState(initialNote ?? "");
  const [count, setCount] = useState<number | null>(null);
  const [loadingCount, setLoadingCount] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  // Mounted gate so createPortal only runs after hydration. Without this,
  // an upstream component being moved to a Server Component would cause a
  // hydration mismatch when the portal target (document.body) is missing
  // during SSR.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  // Reset state and fetch preview every time the dialog opens.
  useEffect(() => {
    if (!open) return;
    setScope("all");
    setNote(initialNote ?? "");
    setCount(null);
    setLoadingCount(true);
    let cancelled = false;
    fetchSimilarCount(txId, newCategoryId)
      .then((r) => { if (!cancelled) setCount(r.count); })
      .catch(() => { if (!cancelled) setCount(null); })
      .finally(() => { if (!cancelled) setLoadingCount(false); });
    return () => { cancelled = true; };
  }, [open, txId, newCategoryId, initialNote]);

  if (!open || !mounted) return null;

  const handleSubmit = async () => {
    setSubmitting(true);
    try {
      const finalNote = note.trim() || null;
      await onConfirm(scope, finalNote);
    } finally {
      setSubmitting(false);
    }
  };

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-xl border border-border bg-card p-5 shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-base font-semibold mb-3">确认分类</h3>

        <div className="space-y-3">
          <ScopeOption
            value="all"
            current={scope}
            onPick={setScope}
            title="同名一起改"
            sub={
              loadingCount
                ? "正在统计…"
                : count != null
                ? `还会更新 ${count} 条同描述记录，并记住此分类`
                : "并记住此分类用于下次"
            }
          />
          <ScopeOption
            value="single"
            current={scope}
            onPick={setScope}
            title="只改这一条"
            sub="不学规则，不影响其它记录"
          />
          <ScopeOption
            value="never"
            current={scope}
            onPick={setScope}
            title="以后别再自动归类同名"
            sub="只改这一条，并停用现有的同名规则"
          />
        </div>

        <div className="mt-4">
          <label className="block text-xs text-muted-foreground mb-1">
            备注（可选 · 作为 AI 分类线索）
          </label>
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={2}
            placeholder="例如：跟朋友 AA 餐厅"
            className="w-full px-2 py-1.5 text-xs rounded-md border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring resize-y"
          />
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="px-3 py-1.5 text-xs rounded-md border border-border hover:bg-muted transition-colors disabled:opacity-50"
          >
            取消
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={submitting}
            className="px-3 py-1.5 text-xs rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
          >
            {submitting ? "确认中…" : "确认"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

interface ScopeOptionProps {
  value: ApplyScope;
  current: ApplyScope;
  onPick: (v: ApplyScope) => void;
  title: string;
  sub: string;
}

function ScopeOption({ value, current, onPick, title, sub }: ScopeOptionProps) {
  const active = current === value;
  return (
    <button
      type="button"
      onClick={() => onPick(value)}
      className={cn(
        "w-full text-left px-3 py-2 rounded-lg border transition-colors flex items-start gap-2",
        active
          ? "border-primary bg-primary/5"
          : "border-border hover:border-primary/40 hover:bg-muted/40",
      )}
    >
      <span
        className={cn(
          "mt-0.5 inline-flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full border-2",
          active ? "border-primary" : "border-muted-foreground/40",
        )}
      >
        {active && <span className="h-1.5 w-1.5 rounded-full bg-primary" />}
      </span>
      <span className="flex-1">
        <span className="block text-sm font-medium">{title}</span>
        <span className="block text-[11px] text-muted-foreground mt-0.5">{sub}</span>
      </span>
    </button>
  );
}
