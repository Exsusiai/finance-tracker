"use client";

import { useEffect, useState } from "react";
import { mutate as swrMutate } from "swr";
import { ApiError, type AccountOut, updateAccount } from "@/lib/api";
import { invalidateTransactionGraph } from "@/lib/hooks";
import { cn } from "@/lib/utils";

/**
 * Per-account sub-account name editor.
 * Reads/writes `accounts.metadata_json.subaccount_names: string[]`.
 *
 * The PDF parser uses this list to identify in-bank moves (e.g. N26 main →
 * "Investing" Space) and tag them as `subaccount=true`, so the balance view
 * skips them and the cash-flow report doesn't double-count them as income/expense.
 */
export function SubaccountListEditor({ account }: { account: AccountOut }) {
  const initial = parseSubaccountNames(account.metadata_json);
  const [names, setNames] = useState<string[]>(initial);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Re-sync when account prop changes (after refresh from another component)
  useEffect(() => {
    setNames(parseSubaccountNames(account.metadata_json));
  }, [account.metadata_json]);

  const dirty = !arrayEqual(names, initial);

  const handleAdd = () => {
    const v = draft.trim();
    if (!v) return;
    if (names.some((n) => n.toLowerCase() === v.toLowerCase())) {
      setDraft("");
      return; // already in list
    }
    setNames([...names, v]);
    setDraft("");
  };

  const handleRemove = (i: number) => {
    setNames(names.filter((_, idx) => idx !== i));
  };

  const handleSave = async () => {
    setError(null);
    try {
      setSaving(true);
      const merged = mergeSubaccountNames(account.metadata_json, names);
      await updateAccount(account.id, { metadata_json: merged });
      // Backend now retroactively reclassifies this account's pending
      // transactions when subaccount_names changes — invalidate the whole
      // transaction graph so inbox / cashflow / balances all refresh.
      await invalidateTransactionGraph();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mt-3 pt-3 border-t border-border/60">
      <div className="flex items-center justify-between mb-1.5">
        <p className="text-[11px] font-semibold text-muted-foreground">
          子账户名（PDF 内此账户里出现的子账户/Space/Pocket 名）
        </p>
        {dirty && (
          <button
            onClick={handleSave}
            disabled={saving}
            className="text-[10px] px-2 py-0.5 rounded text-primary hover:bg-primary/10 transition-colors disabled:opacity-50"
          >
            {saving ? "保存中…" : "保存"}
          </button>
        )}
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
                className="text-muted-foreground hover:text-destructive transition-colors"
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
          placeholder="例如：Investing / Dream List / Saving"
          className="flex-1 px-2 py-1 text-[11px] rounded-md border border-border bg-background focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <button
          onClick={handleAdd}
          disabled={!draft.trim()}
          className={cn(
            "text-[10px] px-2 py-1 rounded-md transition-colors",
            draft.trim()
              ? "bg-primary/10 text-primary hover:bg-primary/20"
              : "text-muted-foreground cursor-not-allowed",
          )}
        >
          + 添加
        </button>
      </div>
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
