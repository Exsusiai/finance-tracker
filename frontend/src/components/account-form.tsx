"use client";

import { useState } from "react";
import {
  type AccountOut,
  ApiError,
  createAccount,
  updateAccount,
} from "@/lib/api";
import { cn, CURRENCY_GROUPS } from "@/lib/utils";

export const ACCOUNT_TYPE_OPTIONS: Array<{
  value: string;
  label: string;
  icon: string;
}> = [
  { value: "bank", label: "银行账户", icon: "🏦" },
  { value: "credit_card", label: "信用卡", icon: "💳" },
  { value: "brokerage", label: "证券账户/交易所", icon: "📈" },
  { value: "crypto_wallet", label: "加密钱包", icon: "₿" },
  { value: "cash", label: "现金", icon: "💵" },
  { value: "other", label: "其他", icon: "📋" },
];

export const ACCOUNT_TYPE_LABELS: Record<string, string> =
  ACCOUNT_TYPE_OPTIONS.reduce(
    (acc, o) => ({ ...acc, [o.value]: o.label }),
    {} as Record<string, string>,
  );

export const ACCOUNT_TYPE_ICONS: Record<string, string> =
  ACCOUNT_TYPE_OPTIONS.reduce(
    (acc, o) => ({ ...acc, [o.value]: o.icon }),
    {} as Record<string, string>,
  );


interface AccountFormProps {
  initial?: AccountOut;
  isEdit?: boolean;
  onClose: () => void;
  onSuccess: (account: AccountOut) => void;
}

export function AccountForm({
  initial,
  isEdit = false,
  onClose,
  onSuccess,
}: AccountFormProps) {
  const [name, setName] = useState(initial?.name ?? "");
  const [type, setType] = useState(initial?.type ?? "bank");
  const [institution, setInstitution] = useState(initial?.institution ?? "");
  const [currency, setCurrency] = useState(initial?.currency ?? "EUR");
  const [initialBalance, setInitialBalance] = useState(
    initial?.initial_balance ?? "0",
  );
  const [notes, setNotes] = useState(initial?.notes ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (!name.trim()) {
      setError("请输入账户名称");
      return;
    }

    try {
      setSubmitting(true);
      let result: AccountOut;
      if (isEdit && initial) {
        result = await updateAccount(initial.id, {
          name: name.trim(),
          type,
          institution: institution.trim() || undefined,
          notes: notes.trim() || undefined,
        });
      } else {
        result = await createAccount({
          name: name.trim(),
          type,
          institution: institution.trim() || undefined,
          currency,
          initial_balance: initialBalance || "0",
          notes: notes.trim() || undefined,
        });
      }
      onSuccess(result);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "操作失败，请重试");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        className="fixed inset-0 bg-black/50 backdrop-blur-sm"
        onClick={() => !submitting && onClose()}
      />
      <div className="relative w-full max-w-md rounded-xl border border-border bg-card p-6 shadow-xl max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold">
            {isEdit ? "编辑账户" : "添加账户"}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="p-2 rounded-lg hover:bg-muted transition-colors text-muted-foreground"
            aria-label="关闭"
          >
            <svg
              className="h-5 w-5"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M6 18L18 6M6 6l12 12"
              />
            </svg>
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium mb-2">
              账户类型 <span className="text-destructive">*</span>
            </label>
            <div className="grid grid-cols-3 gap-2">
              {ACCOUNT_TYPE_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => setType(opt.value)}
                  className={cn(
                    "flex flex-col items-center gap-1 px-2 py-3 text-xs font-medium rounded-lg border-2 transition-all",
                    type === opt.value
                      ? "border-primary bg-primary/5 text-foreground"
                      : "border-border hover:border-muted-foreground/30 text-muted-foreground",
                  )}
                >
                  <span className="text-xl">{opt.icon}</span>
                  <span>{opt.label}</span>
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium mb-2">
              账户名称 <span className="text-destructive">*</span>
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="如：N26 主账户、Amex Gold"
              required
              className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-2">机构</label>
            <input
              type="text"
              value={institution}
              onChange={(e) => setInstitution(e.target.value)}
              placeholder="如：N26 Bank、American Express"
              className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium mb-2">
                币种 <span className="text-destructive">*</span>
              </label>
              <select
                value={currency}
                onChange={(e) => setCurrency(e.target.value)}
                disabled={isEdit}
                className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {CURRENCY_GROUPS.map((g) => (
                  <optgroup key={g.label} label={g.label}>
                    {g.values.map((c) => (
                      <option key={c} value={c}>
                        {c}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium mb-2">
                初始余额
              </label>
              <input
                type="number"
                step="any"
                value={initialBalance}
                onChange={(e) => setInitialBalance(e.target.value)}
                placeholder="0.00"
                disabled={isEdit}
                className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-60 disabled:cursor-not-allowed"
              />
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium mb-2">备注</label>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={2}
              placeholder="选填"
              className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring resize-none"
            />
          </div>

          {isEdit && (
            <p className="text-xs text-muted-foreground">
              币种与初始余额创建后不可修改。如需调整当前余额，请在投资组合页使用"调整余额"。
            </p>
          )}

          {error && (
            <div className="p-3 rounded-lg bg-destructive/10 border border-destructive/20 text-sm text-destructive">
              {error}
            </div>
          )}

          <div className="flex gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              disabled={submitting}
              className="flex-1 px-4 py-2.5 text-sm font-medium rounded-lg border border-border hover:bg-muted transition-colors disabled:opacity-50"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="flex-1 px-4 py-2.5 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
            >
              {submitting ? "保存中…" : isEdit ? "保存修改" : "创建账户"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
