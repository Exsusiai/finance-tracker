"use client";

import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  refreshAllMatching,
  type RefreshMatchingSummary,
} from "@/lib/api";
import { invalidateTransactionGraph } from "@/lib/hooks";

// Mirrors `services/refresh_matching/PIPELINE` order + the post-commit
// LLM dispatch step. Pure UI hint — the backend doesn't stream step
// events, so the front-end just rotates through the labels at a fixed
// cadence to give the user a sense that something is moving.
const _PIPELINE_LABELS = [
  "清理孤儿指针…",
  "重判交易类型…",
  "重新匹配分类规则…",
  "识别子账户配对…",
  "识别 IBAN 单边转账…",
  "跨账户自动配对…",
  "修复孤儿单边转账…",
  "补内部储蓄分类…",
  "重新入收件箱…",
  "重算月度现金流…",
  "派遣 LLM 智能分类…",
];

/**
 * Page-level "re-run every matching/categorisation pass globally" trigger.
 *
 * Mounted in the 交易记录 page header alongside 手动记账 because the action
 * affects every tab (待确认, 分类视图, 转账建议, 交易记录) — not just one
 * sub-section. Clicking is equivalent to "pretend I just re-imported all my
 * PDFs from scratch": re-detect type, re-classify category, re-pair across
 * accounts, re-enqueue anything still untagged.
 *
 * Manual edits (source='manual' or rows with user_note) are preserved.
 */
export function RefreshMatchingButton() {
  const [running, setRunning] = useState(false);
  const [stepIdx, setStepIdx] = useState(0);
  const [summary, setSummary] = useState<RefreshMatchingSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);

  // Cycle through the labels while the request is in flight. Caps at the
  // last one so we don't claim "done" before the API responds.
  useEffect(() => {
    if (!running) {
      setStepIdx(0);
      return;
    }
    setStepIdx(0);
    const id = setInterval(() => {
      setStepIdx((i) => Math.min(i + 1, _PIPELINE_LABELS.length - 1));
    }, 300);
    return () => clearInterval(id);
  }, [running]);

  // Dismiss the summary popover when the user clicks outside of it (or
  // presses Escape). Auto-dismiss timer below is kept as a safety net.
  useEffect(() => {
    if (!summary && !error) return;
    const handlePointer = (e: MouseEvent) => {
      const target = e.target as Node | null;
      if (!target) return;
      // Click on the trigger button itself shouldn't auto-close — the
      // user might be re-running and the next click handler resets state.
      if (buttonRef.current && buttonRef.current.contains(target)) return;
      if (popoverRef.current && popoverRef.current.contains(target)) return;
      setSummary(null);
      setError(null);
    };
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setSummary(null);
        setError(null);
      }
    };
    document.addEventListener("mousedown", handlePointer);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handlePointer);
      document.removeEventListener("keydown", handleKey);
    };
  }, [summary, error]);

  const handleClick = async () => {
    setError(null);
    setSummary(null);
    setRunning(true);
    try {
      const r = await refreshAllMatching();
      setSummary(r);
      invalidateTransactionGraph();
      // Auto-dismiss the summary after 8 seconds as a safety net (the
      // outside-click / Escape handlers above are the primary way out).
      setTimeout(() => setSummary((s) => (s === r ? null : s)), 8000);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "重新匹配失败");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="relative inline-flex flex-col items-end">
      <button
        ref={buttonRef}
        onClick={handleClick}
        disabled={running}
        title="对所有历史交易重跑：分类规则 + LLM 智能分类 + 类型识别 + 跨账户配对 + 子账户检测。已手动确认的不会被覆盖。"
        className="inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium rounded-lg border border-border bg-card hover:bg-muted transition-colors disabled:opacity-50"
      >
        <span className={running ? "animate-spin inline-block" : "inline-block"}>↻</span>
        {running ? _PIPELINE_LABELS[stepIdx] : "重新匹配"}
      </button>
      {summary && (
        <div
          ref={popoverRef}
          className="absolute top-full mt-1 right-0 z-10 w-[320px] rounded-lg border border-border bg-card p-3 shadow-md text-[11px] text-muted-foreground space-y-0.5"
        >
          <div className="flex items-center justify-between mb-1">
            <span className="text-xs font-medium text-foreground">已完成：</span>
            <button
              type="button"
              onClick={() => setSummary(null)}
              aria-label="关闭"
              className="text-[10px] text-muted-foreground hover:text-foreground"
            >
              ✕
            </button>
          </div>
          <Row label="清理孤儿指针" v={summary.orphan_pointers_cleared} />
          <Row label="升级为转账" v={summary.type_promoted_to_transfer} />
          <Row label="L1 自动分类" v={summary.recategorized} />
          <Row label="子账户对" v={summary.subaccount_pairs} />
          <Row label="IBAN 单边" v={summary.single_leg_iban} />
          <Row label="跨账户自动配对" v={summary.auto_paired} />
          <Row label="orphan 修复" v={summary.orphan_paired} />
          <Row label="补内部储蓄" v={summary.subaccount_orphans_categorized} />
          <Row label="重入收件箱" v={summary.reenqueued_to_inbox} />
          <Row label="重算月份数" v={summary.periods_recomputed} />
          <Row label="✨ 已派遣 LLM" v={summary.llm_dispatched ?? 0} />
          {(summary.llm_dispatched ?? 0) > 0 && (
            <div className="mt-1 text-[10px] text-muted-foreground italic">
              LLM 在后台异步运行，结果会在完成后陆续出现在收件箱
            </div>
          )}
          <div className="mt-1.5 pt-1.5 border-t border-border text-[10px] text-muted-foreground/80">
            点击外部或按 Esc 关闭
          </div>
        </div>
      )}
      {error && (
        <div
          ref={popoverRef}
          className="absolute top-full mt-1 right-0 z-10 px-3 py-2 rounded-lg border border-destructive/40 bg-destructive/10 text-[11px] text-destructive"
        >
          {error}
        </div>
      )}
    </div>
  );
}

function Row({ label, v }: { label: string; v: number }) {
  return (
    <div className="flex justify-between gap-2 tabular-nums">
      <span>{label}</span>
      <span className={v > 0 ? "font-medium text-foreground" : ""}>{v}</span>
    </div>
  );
}
