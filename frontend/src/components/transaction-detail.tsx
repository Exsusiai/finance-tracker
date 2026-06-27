"use client";

import { useState, useCallback } from "react";
import {
  type TransactionOut,
  type AccountOut,
  type CategoryOut,
  updateTransaction,
  deleteTransaction,
  recategorizeTransaction,
  unsplitTransaction,
  ApiError,
} from "@/lib/api";
import { invalidateTransactionGraph } from "@/lib/hooks";
import { formatCurrency, formatDate, cn } from "@/lib/utils";
import { TransactionForm } from "./transaction-form";
import { SplitTransactionForm } from "./split-transaction-form";

interface TransactionDetailProps {
  tx: TransactionOut;
  accounts: AccountOut[];
  categories: CategoryOut[];
  onClose: () => void;
  onUpdate: () => void;
}

const TYPE_LABELS: Record<string, { label: string; color: string }> = {
  expense: { label: "支出", color: "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400" },
  income: { label: "收入", color: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400" },
  transfer: { label: "转账", color: "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400" },
  adjustment: { label: "调整", color: "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400" },
};

const SOURCE_LABELS: Record<string, string> = {
  manual: "手动录入",
  pdf_import: "PDF 导入",
  bank_api: "银行 API",
  mcp_agent: "MCP Agent",
};

export function TransactionDetail({
  tx,
  accounts,
  categories,
  onClose,
  onUpdate,
}: TransactionDetailProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [isSplitting, setIsSplitting] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [recategorizing, setRecategorizing] = useState(false);

  // Is this row part of a split group? (metadata.split_group_id)
  let isSplit = false;
  try {
    isSplit = !!(tx.metadata_json && JSON.parse(tx.metadata_json).split_group_id != null);
  } catch { /* malformed metadata → treat as not split */ }

  const handleUnsplit = async () => {
    try {
      await unsplitTransaction(tx.id);
      invalidateTransactionGraph();
      onUpdate();
      onClose();
    } catch (e) {
      console.error("Unsplit failed:", e);
    }
  };

  const handleDelete = async () => {
    try {
      await deleteTransaction(tx.id);
      invalidateTransactionGraph();
      onUpdate();
      onClose();
    } catch (e) {
      console.error("Delete failed:", e);
    }
  };

  const handleRecategorize = async () => {
    try {
      setRecategorizing(true);
      await recategorizeTransaction(tx.id);
      onUpdate();
    } catch (e) {
      console.error("Recategorize failed:", e);
    } finally {
      setRecategorizing(false);
    }
  };

  const typeInfo = TYPE_LABELS[tx.type] || { label: tx.type, color: "bg-muted" };

  return (
    <div className="fixed inset-0 z-50 flex">
      {/* Backdrop */}
      <div className="fixed inset-0 bg-black/40 backdrop-blur-sm" onClick={onClose} />

      {/* Panel */}
      <div className="fixed right-0 top-0 bottom-0 w-full max-w-md bg-card border-l border-border overflow-y-auto shadow-2xl">
        <div className="p-6">
          {/* Header */}
          <div className="flex items-center justify-between mb-6">
            <h2 className="text-lg font-semibold">交易详情</h2>
            <button
              onClick={onClose}
              className="p-2 rounded-lg hover:bg-muted transition-colors text-muted-foreground"
            >
              <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          {isEditing ? (
            <TransactionForm
              accounts={accounts}
              categories={categories}
              onClose={() => setIsEditing(false)}
              onSuccess={() => {
                setIsEditing(false);
                onUpdate();
              }}
              // After unbind, the read-only DetailRow underneath still
              // renders the now-stale counter_account_id from the parent's
              // prop. Closing the whole panel forces a fresh open if the
              // user wants to inspect the updated state.
              onUnbindSuccess={() => {
                onUpdate();
                onClose();
              }}
              initialData={{
                id: tx.id,
                account_id: tx.account_id,
                counter_account_id: tx.counter_account_id,
                category_id: tx.category_id,
                occurred_at: tx.occurred_at,
                amount: tx.amount,
                currency: tx.currency,
                type: tx.type,
                description: tx.description,
                counterparty: tx.counterparty,
                tags: tx.tags,
                is_pending: tx.is_pending,
              }}
              isEdit
            />
          ) : isSplitting ? (
            <SplitTransactionForm
              tx={tx}
              categories={categories}
              onClose={() => setIsSplitting(false)}
              onSuccess={() => {
                setIsSplitting(false);
                onUpdate();
                onClose();
              }}
            />
          ) : (
            <div className="space-y-5">
              {/* Amount */}
              <div className="text-center py-4">
                <p className="text-3xl font-bold" style={{
                  color: tx.type === "expense" ? "hsl(340, 70%, 55%)"
                    : tx.type === "income" ? "hsl(160, 60%, 45%)"
                    : undefined
                }}>
                  {tx.type === "expense" ? "-" : tx.type === "income" ? "+" : ""}
                  {formatCurrency(parseFloat(tx.amount), tx.currency)}
                </p>
                <div className="flex items-center justify-center gap-2 mt-2">
                  <span className={cn("px-2 py-0.5 text-xs font-medium rounded-full", typeInfo.color)}>
                    {typeInfo.label}
                  </span>
                  {tx.is_pending && (
                    <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400">
                      待确认
                    </span>
                  )}
                </div>
              </div>

              {/* Details grid */}
              <div className="space-y-3">
                <DetailRow label="日期" value={formatDate(tx.occurred_at)} />
                {tx.posted_at && (
                  <DetailRow label="记账日期" value={formatDate(tx.posted_at)} />
                )}
                <DetailRow label="账户" value={tx.account_name || `#${tx.account_id}`} />
                <DetailRow
                  label="分类"
                  value={tx.category_name || "未分类"}
                  empty={!!tx.category_id}
                />
                {tx.description && <DetailRow label="描述" value={tx.description} />}
                {tx.raw_description && tx.raw_description !== tx.description && (
                  <DetailRow label="原始描述" value={tx.raw_description} />
                )}
                {tx.counterparty && <DetailRow label="对方姓名" value={tx.counterparty} />}
                {tx.type === "transfer" && tx.counter_account_id && (
                  <DetailRow
                    label="对手账户"
                    value={
                      accounts.find((a) => a.id === tx.counter_account_id)?.name
                      ?? `#${tx.counter_account_id}`
                    }
                  />
                )}
                {tx.location && <DetailRow label="地点" value={tx.location} />}
                <DetailRow label="来源" value={SOURCE_LABELS[tx.source] || tx.source} />
                {tx.tags.length > 0 && (
                  <DetailRow label="标签" value={tx.tags.join(", ")} />
                )}
                {tx.external_id && <DetailRow label="外部ID" value={tx.external_id} />}
                {tx.base_amount && (
                  <DetailRow
                    label="基础货币金额"
                    value={formatCurrency(parseFloat(tx.base_amount))}
                  />
                )}
                {tx.fx_rate_to_base && (
                  <DetailRow label="汇率" value={tx.fx_rate_to_base} />
                )}
              </div>

              {/* Timestamps */}
              <div className="pt-3 border-t border-border">
                <p className="text-xs text-muted-foreground">
                  创建: {new Date(tx.created_at).toLocaleString("zh-CN")}
                </p>
                <p className="text-xs text-muted-foreground">
                  更新: {new Date(tx.updated_at).toLocaleString("zh-CN")}
                </p>
              </div>

              {/* Actions */}
              <div className="space-y-3 pt-3 border-t border-border">
                {/* Edit */}
                <button
                  onClick={() => setIsEditing(true)}
                  className="w-full flex items-center justify-center gap-2 px-4 py-2.5 text-sm font-medium rounded-lg border border-border hover:bg-muted transition-colors"
                >
                  <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                  </svg>
                  编辑
                </button>

                {/* Recategorize */}
                {tx.category_id === null && (
                  <button
                    onClick={handleRecategorize}
                    disabled={recategorizing}
                    className="w-full flex items-center justify-center gap-2 px-4 py-2.5 text-sm font-medium rounded-lg border border-border hover:bg-muted transition-colors disabled:opacity-50"
                  >
                    <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z" />
                    </svg>
                    {recategorizing ? "自动分类中…" : "智能分类"}
                  </button>
                )}

                {/* Split (AA / 代付): only for non-adjustment, non-split rows */}
                {!isSplit && tx.type !== "adjustment" && (
                  <button
                    onClick={() => setIsSplitting(true)}
                    className="w-full flex items-center justify-center gap-2 px-4 py-2.5 text-sm font-medium rounded-lg border border-border hover:bg-muted transition-colors"
                  >
                    <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 7h12m0 0l-4-4m4 4l-4 4m4 6H4m0 0l4 4m-4-4l4-4" />
                    </svg>
                    拆分(AA/代付)
                  </button>
                )}

                {/* Unsplit: restore the original, remove siblings */}
                {isSplit && (
                  <button
                    onClick={handleUnsplit}
                    className="w-full flex items-center justify-center gap-2 px-4 py-2.5 text-sm font-medium rounded-lg border border-border hover:bg-muted transition-colors"
                  >
                    取消拆分(还原为原始整笔)
                  </button>
                )}

                {/* Delete */}
                {showDeleteConfirm ? (
                  <div className="flex gap-2">
                    <button
                      onClick={handleDelete}
                      className="flex-1 px-4 py-2.5 text-sm font-medium rounded-lg bg-destructive text-destructive-foreground hover:bg-destructive/90 transition-colors"
                    >
                      确认删除
                    </button>
                    <button
                      onClick={() => setShowDeleteConfirm(false)}
                      className="flex-1 px-4 py-2.5 text-sm font-medium rounded-lg border border-border hover:bg-muted transition-colors"
                    >
                      取消
                    </button>
                  </div>
                ) : (
                  <button
                    onClick={() => setShowDeleteConfirm(true)}
                    className="w-full flex items-center justify-center gap-2 px-4 py-2.5 text-sm font-medium rounded-lg border border-destructive/30 text-destructive hover:bg-destructive/5 transition-colors"
                  >
                    <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                    </svg>
                    删除
                  </button>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function DetailRow({
  label,
  value,
  empty,
}: {
  label: string;
  value: string;
  empty?: boolean;
}) {
  return (
    <div className="flex justify-between items-start gap-4">
      <span className="text-sm text-muted-foreground shrink-0">{label}</span>
      <span className={cn("text-sm text-right", empty ? "text-muted-foreground italic" : "text-foreground font-medium")}>
        {value}
      </span>
    </div>
  );
}
