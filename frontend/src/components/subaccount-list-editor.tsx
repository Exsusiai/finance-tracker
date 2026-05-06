"use client";

import { useEffect, useState } from "react";
import { ApiError, type AccountOut, updateAccount } from "@/lib/api";
import { invalidateTransactionGraph } from "@/lib/hooks";
import { cn } from "@/lib/utils";

/**
 * Per-account sub-account name editor.
 * Reads/writes `accounts.metadata_json.subaccount_names: string[]`.
 *
 * The PDF parser uses this list to identify in-bank moves (e.g. N26 main →
 * "Investing" Space) and tag them as `subaccount=true`, so the balance view
 * skips them and the cash-flow report doesn't double-count them as
 * income/expense. Backend's PATCH /accounts/{id} also retroactively
 * re-classifies any pending pdf_import rows that match the newly-added
 * names (see _reclassify_pending_for_subaccounts).
 *
 * 2026-05-06 UX fix: previously add/remove only updated local state and
 * required a second click on "保存" to persist — users would close the
 * page assuming it had saved. Now every add/remove fires a PATCH
 * immediately so what you see in the chip list IS what's saved.
 */
export function SubaccountListEditor({ account }: { account: AccountOut }) {
  const [names, setNames] = useState<string[]>(parseSubaccountNames(account.metadata_json));
  const [draft, setDraft] = useState("");
  const [status, setStatus] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  const [reclassified, setReclassified] = useState<number | null>(null);

  // Re-sync when account prop changes (after refresh from another component)
  useEffect(() => {
    setNames(parseSubaccountNames(account.metadata_json));
  }, [account.metadata_json]);

  // Persist a new name list to the backend immediately.
  const persist = async (next: string[], prev: string[]) => {
    setError(null);
    setReclassified(null);
    setStatus("saving");
    setNames(next);  // optimistic UI
    try {
      const merged = mergeSubaccountNames(account.metadata_json, next);
      const resp = await updateAccount(account.id, { metadata_json: merged });
      // Backend returns meta.subaccount_reclassified when added names
      // matched any pending pdf_import rows.
      const meta = (resp as unknown as { meta?: { subaccount_reclassified?: number } }).meta;
      if (meta?.subaccount_reclassified && meta.subaccount_reclassified > 0) {
        setReclassified(meta.subaccount_reclassified);
      }
      await invalidateTransactionGraph();
      setStatus("saved");
      // Brief flash then back to idle.
      setTimeout(() => setStatus("idle"), 1500);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "保存失败");
      setStatus("error");
      setNames(prev);  // rollback optimistic update
    }
  };

  const handleAdd = async () => {
    const v = draft.trim();
    if (!v) return;
    if (names.some((n) => n.toLowerCase() === v.toLowerCase())) {
      setDraft("");
      return;
    }
    const prev = names;
    const next = [...names, v];
    setDraft("");
    await persist(next, prev);
  };

  const handleRemove = async (i: number) => {
    const prev = names;
    const next = names.filter((_, idx) => idx !== i);
    await persist(next, prev);
  };

  return (
    <div className="mt-3 pt-3 border-t border-border/60">
      <div className="flex items-center justify-between mb-1.5">
        <p className="text-[11px] font-semibold text-muted-foreground">
          子账户名（PDF 内此账户里出现的子账户/Space/Pocket 名）
        </p>
        <span
          className={cn(
            "text-[10px] transition-opacity",
            status === "saving" && "text-muted-foreground",
            status === "saved" && "text-emerald-600 dark:text-emerald-400",
            status === "error" && "text-destructive",
            status === "idle" && "opacity-0",
          )}
        >
          {status === "saving" && "保存中…"}
          {status === "saved" && "✓ 已保存"}
          {status === "error" && "✗ 保存失败"}
          {status === "idle" && " "}
        </span>
      </div>
      <div className="flex flex-wrap gap-1.5 mb-2">
        {names.length === 0 ? (
          <span className="text-[10px] text-muted-foreground italic">
            暂无；添加后 PDF 解析会把这些名字下的转账识别为内部移动
          </span>
        ) : (
          names.map((n, i) => (
            <span
              key={`${n}-${i}`}
              className="inline-flex items-center gap-1 px-2 py-0.5 text-[10px] rounded-full bg-slate-500/10 text-slate-700 dark:text-slate-300"
            >
              {n}
              <button
                onClick={() => handleRemove(i)}
                disabled={status === "saving"}
                className="text-muted-foreground hover:text-destructive transition-colors disabled:opacity-50"
                aria-label={`删除 ${n}`}
              >
                ×
              </button>
            </span>
          ))
        )}
      </div>
      <div className="flex gap-1.5">
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              handleAdd();
            }
          }}
          disabled={status === "saving"}
          placeholder="例如：Investing / Dream List / Saving"
          className="flex-1 px-2 py-1 text-[11px] rounded-md border border-border bg-background focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
        />
        <button
          onClick={handleAdd}
          disabled={!draft.trim() || status === "saving"}
          className={cn(
            "text-[10px] px-2 py-1 rounded-md transition-colors",
            draft.trim() && status !== "saving"
              ? "bg-primary/10 text-primary hover:bg-primary/20"
              : "text-muted-foreground cursor-not-allowed",
          )}
        >
          + 添加
        </button>
      </div>
      {reclassified != null && (
        <p className="mt-1 text-[10px] text-emerald-600 dark:text-emerald-400">
          已自动把 {reclassified} 笔待确认交易识别为子账户内部转账
        </p>
      )}
      {error && <p className="mt-1 text-[10px] text-destructive">{error}</p>}
    </div>
  );
}

function parseSubaccountNames(metadata_json: string | null): string[] {
  if (!metadata_json) return [];
  try {
    const meta = JSON.parse(metadata_json);
    if (typeof meta !== "object" || meta === null) return [];
    const arr = (meta as Record<string, unknown>).subaccount_names;
    if (!Array.isArray(arr)) return [];
    return arr.map(String).filter((s) => s.trim().length > 0);
  } catch {
    return [];
  }
}

function mergeSubaccountNames(
  metadata_json: string | null,
  names: string[],
): string {
  let cur: Record<string, unknown> = {};
  if (metadata_json) {
    try {
      const parsed = JSON.parse(metadata_json);
      if (typeof parsed === "object" && parsed !== null) {
        cur = parsed as Record<string, unknown>;
      }
    } catch {
      cur = {};
    }
  }
  cur.subaccount_names = names;
  return JSON.stringify(cur);
}

function arrayEqual(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  return a.every((v, i) => v === b[i]);
}
