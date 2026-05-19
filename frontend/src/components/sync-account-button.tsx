"use client";

import { useState } from "react";
import { ApiError, syncAccount, type SyncSummaryOut } from "@/lib/api";
import { cn } from "@/lib/utils";

interface SyncAccountButtonProps {
  accountId: number;
  // Optional callback so the parent (e.g. assets page) can refresh holdings.
  onSynced?: (summary: SyncSummaryOut) => void;
  className?: string;
}

export function SyncAccountButton({
  accountId,
  onSynced,
  className,
}: SyncAccountButtonProps) {
  const [busy, setBusy] = useState(false);
  const [lastSummary, setLastSummary] = useState<SyncSummaryOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleClick() {
    setError(null);
    try {
      setBusy(true);
      const s = await syncAccount(accountId);
      setLastSummary(s);
      onSynced?.(s);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "同步失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={cn("inline-flex flex-col items-end gap-1", className)}>
      <button
        type="button"
        onClick={handleClick}
        disabled={busy}
        className={cn(
          "inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded-md border border-border hover:bg-muted transition-colors disabled:opacity-60",
        )}
        title="拉取链上 / 交易所最新余额"
      >
        <span className={cn(busy && "animate-spin")}>↻</span>
        {busy ? "同步中…" : "立即同步"}
      </button>
      {lastSummary && !busy && (
        <div className="flex flex-col items-end gap-0.5 max-w-[280px]">
          <span
            className={cn(
              "text-[11px]",
              lastSummary.total_errors > 0 ? "text-amber-600" : "text-muted-foreground",
            )}
          >
            已同步 {lastSummary.total_synced} 个币种
            {lastSummary.total_errors > 0 && `，${lastSummary.total_errors} 处出错`}
          </span>
          {/* Show each failing source so the user knows WHICH chain /
              exchange failed and why — not just an opaque counter. */}
          {lastSummary.results
            .filter((r) => r.error)
            .map((r, idx) => (
              <span
                key={idx}
                className="text-[10px] text-destructive text-right leading-snug"
                title={r.error ?? ""}
              >
                {r.label}: {r.error}
              </span>
            ))}
        </div>
      )}
      {error && (
        <span className="text-[11px] text-destructive max-w-[280px] text-right">
          {error}
        </span>
      )}
    </div>
  );
}
