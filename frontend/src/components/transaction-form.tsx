"use client";

import { useState } from "react";
import {
  type AccountOut,
  type CategoryOut,
  type TransactionCreateInput,
  createTransaction,
  type TransactionUpdateInput,
  unbindTransferCounter,
  updateTransaction,
  ApiError,
} from "@/lib/api";
import { invalidateTransactionGraph } from "@/lib/hooks";
import { cn } from "@/lib/utils";

interface TransactionFormProps {
  accounts: AccountOut[];
  categories: CategoryOut[];
  onClose: () => void;
  onSuccess: () => void;
  /** Optional override for unbind success. Defaults to `onSuccess`, but
   *  the parent can route it differently (e.g. close the whole detail
   *  panel since the read-only view would otherwise display the now-stale
   *  counter account from props). */
  onUnbindSuccess?: () => void;
  initialData?: {
    id: number;
    account_id: number;
    counter_account_id?: number | null;
    category_id: number | null;
    occurred_at: string;
    amount: string;
    currency: string;
    type: string;
    description?: string | null;
    counterparty?: string | null;
    location?: string | null;
    tags?: string[];
    is_pending?: boolean;
  };
  isEdit?: boolean;
}

const TYPE_OPTIONS = [
  { value: "expense", label: "支出", color: "text-red-500" },
  { value: "income", label: "收入", color: "text-green-500" },
  { value: "transfer", label: "转账", color: "text-blue-500" },
];

export function TransactionForm({
  accounts,
  categories,
  onClose,
  onSuccess,
  onUnbindSuccess,
  initialData,
  isEdit = false,
}: TransactionFormProps) {
  const [type, setType] = useState(initialData?.type || "expense");
  const [accountId, setAccountId] = useState(initialData?.account_id || (accounts[0]?.id ?? 0));
  const [categoryId, setCategoryId] = useState<number | undefined>(
    initialData?.category_id ?? undefined
  );
  const [amount, setAmount] = useState(initialData?.amount || "");
  const [occurredAt, setOccurredAt] = useState(
    initialData?.occurred_at
      ? initialData.occurred_at.slice(0, 10)
      : new Date().toISOString().slice(0, 10)
  );
  const [description, setDescription] = useState(initialData?.description || "");
  const [counterparty, setCounterparty] = useState(initialData?.counterparty || "");
  const [tagsInput, setTagsInput] = useState(initialData?.tags?.join(", ") || "");
  const [isPending, setIsPending] = useState(initialData?.is_pending ?? false);

  const [counterAccountId, setCounterAccountId] = useState<number | null>(
    initialData?.counter_account_id ?? null,
  );
  const [submitting, setSubmitting] = useState(false);
  const [unbinding, setUnbinding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const counterAccount = counterAccountId
    ? accounts.find((a) => a.id === counterAccountId) ?? null
    : null;

  const handleUnbind = async () => {
    if (!isEdit || !initialData) return;
    if (!confirm("确认解除该转账与对手账户的绑定？解除后会回到「未配对」面板，可重新绑定。")) {
      return;
    }
    setError(null);
    setUnbinding(true);
    try {
      await unbindTransferCounter(initialData.id);
      setCounterAccountId(null);
      invalidateTransactionGraph();
      // Prefer the explicit unbind hook; fall back to onSuccess so existing
      // call sites keep working.
      (onUnbindSuccess ?? onSuccess)();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "解除绑定失败");
    } finally {
      setUnbinding(false);
    }
  };

  const selectedAccount = accounts.find((a) => a.id === accountId);
  const currency = selectedAccount?.currency || "EUR";

  const filteredCategories = categories.filter((c) => c.kind === type);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (!accountId || !amount || !occurredAt) {
      setError("请填写必填字段");
      return;
    }

    const tags = tagsInput
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);

    try {
      setSubmitting(true);

      if (isEdit && initialData) {
        const data: TransactionUpdateInput = {
          account_id: accountId,
          category_id: categoryId,
          occurred_at: occurredAt,
          amount,
          currency,
          type,
          description: description || undefined,
          counterparty: counterparty || undefined,
          tags,
          is_pending: isPending,
        };
        await updateTransaction(initialData.id, data);
      } else {
        const data: TransactionCreateInput = {
          account_id: accountId,
          category_id: categoryId,
          occurred_at: occurredAt,
          amount,
          currency,
          type,
          description: description || undefined,
          counterparty: counterparty || undefined,
          tags,
          is_pending: isPending,
        };
        await createTransaction(data);
      }

      invalidateTransactionGraph();
      onSuccess();
    } catch (e) {
      if (e instanceof ApiError) {
        setError(e.message);
      } else {
        setError("操作失败，请重试");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex">
      {/* Backdrop */}
      <div className="fixed inset-0 bg-black/40 backdrop-blur-sm" onClick={onClose} />

      {/* Panel */}
      <div className="fixed right-0 top-0 bottom-0 w-full max-w-md bg-card border-l border-border overflow-y-auto shadow-2xl">
        <div className="p-6">
          {/* Header */}
          <div className="flex items-center justify-between mb-6">
            <h2 className="text-lg font-semibold">
              {isEdit ? "编辑交易" : "新增交易"}
            </h2>
            <button
              onClick={onClose}
              className="p-2 rounded-lg hover:bg-muted transition-colors text-muted-foreground"
            >
              <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          {/* Form */}
          <form onSubmit={handleSubmit} className="space-y-5">
            {/* Type selector */}
            <div>
              <label className="block text-sm font-medium mb-2">类型</label>
              <div className="flex gap-2">
                {TYPE_OPTIONS.map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => {
                      setType(opt.value);
                      setCategoryId(undefined);
                    }}
                    className={cn(
                      "flex-1 py-2.5 text-sm font-medium rounded-lg border-2 transition-all",
                      type === opt.value
                        ? "border-primary bg-primary/5 " + opt.color
                        : "border-border hover:border-muted-foreground/30 text-muted-foreground"
                    )}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
            </div>

            {/* Amount */}
            <div>
              <label className="block text-sm font-medium mb-2">
                金额 <span className="text-destructive">*</span>
                <span className="text-muted-foreground font-normal ml-2">
                  ({currency})
                </span>
              </label>
              <input
                type="number"
                step="0.01"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                placeholder="0.00"
                required
                className="w-full px-4 py-3 text-lg font-semibold rounded-lg border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                autoFocus
              />
            </div>

            {/* Date */}
            <div>
              <label className="block text-sm font-medium mb-2">
                日期 <span className="text-destructive">*</span>
              </label>
              <input
                type="date"
                value={occurredAt}
                onChange={(e) => setOccurredAt(e.target.value)}
                required
                className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              />
            </div>

            {/* Account */}
            <div>
              <label className="block text-sm font-medium mb-2">
                账户 <span className="text-destructive">*</span>
              </label>
              <select
                value={accountId}
                onChange={(e) => setAccountId(Number(e.target.value))}
                required
                className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              >
                <option value="">选择账户</option>
                {accounts.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name} ({a.currency})
                  </option>
                ))}
              </select>
            </div>

            {/* Category */}
            <div>
              <label className="block text-sm font-medium mb-2">分类</label>
              <select
                value={categoryId || ""}
                onChange={(e) =>
                  setCategoryId(e.target.value ? Number(e.target.value) : undefined)
                }
                className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              >
                <option value="">未分类</option>
                {filteredCategories.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </div>

            {/* Description */}
            <div>
              <label className="block text-sm font-medium mb-2">描述</label>
              <input
                type="text"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="交易描述"
                className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              />
            </div>

            {/* Counterparty (free-text — merchant or person on the other side) */}
            <div>
              <label className="block text-sm font-medium mb-2">对方姓名 / 商户</label>
              <input
                type="text"
                value={counterparty}
                onChange={(e) => setCounterparty(e.target.value)}
                placeholder="例如：Amazon、张三"
                className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              />
            </div>

            {/* Counter account (only meaningful for transfers; read-only —
                changing the binding goes through the 转账建议 panel) */}
            {type === "transfer" && (
              <div>
                <label className="block text-sm font-medium mb-2">对手账户</label>
                {counterAccount ? (
                  <div className="flex items-center gap-2 px-3 py-2 text-sm rounded-lg border border-border bg-muted/30">
                    <span className="flex-1 text-foreground">
                      {counterAccount.name}
                      <span className="ml-1.5 text-xs text-muted-foreground">
                        ({counterAccount.currency})
                      </span>
                    </span>
                    {isEdit && (
                      <button
                        type="button"
                        onClick={handleUnbind}
                        disabled={unbinding || submitting}
                        className="text-xs px-2 py-1 rounded-md text-destructive hover:bg-destructive/10 transition-colors disabled:opacity-50"
                      >
                        {unbinding ? "解除中…" : "解除绑定"}
                      </button>
                    )}
                  </div>
                ) : (
                  <p className="text-xs text-muted-foreground italic px-3 py-2 rounded-lg border border-dashed border-border">
                    未绑定。去「转账建议 → 未配对转账」面板可手动绑定对手账户。
                  </p>
                )}
              </div>
            )}

            {/* Tags */}
            <div>
              <label className="block text-sm font-medium mb-2">标签</label>
              <input
                type="text"
                value={tagsInput}
                onChange={(e) => setTagsInput(e.target.value)}
                placeholder="逗号分隔，如：餐饮, 周末"
                className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              />
            </div>

            {/* Pending toggle */}
            <label className="flex items-center gap-3 cursor-pointer">
              <input
                type="checkbox"
                checked={isPending}
                onChange={(e) => setIsPending(e.target.checked)}
                className="h-4 w-4 rounded border-border accent-primary"
              />
              <span className="text-sm">标记为待确认</span>
            </label>

            {/* Error */}
            {error && (
              <div className="p-3 rounded-lg bg-destructive/10 border border-destructive/20 text-sm text-destructive">
                {error}
              </div>
            )}

            {/* Actions */}
            <div className="flex gap-3 pt-2">
              <button
                type="button"
                onClick={onClose}
                className="flex-1 px-4 py-2.5 text-sm font-medium rounded-lg border border-border hover:bg-muted transition-colors"
              >
                取消
              </button>
              <button
                type="submit"
                disabled={submitting}
                className="flex-1 px-4 py-2.5 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
              >
                {submitting ? "保存中…" : isEdit ? "保存修改" : "添加交易"}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}
