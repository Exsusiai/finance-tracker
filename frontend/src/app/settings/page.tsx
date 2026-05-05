"use client";

import { useMemo, useState } from "react";
import {
  AccountForm,
  ACCOUNT_TYPE_ICONS,
  ACCOUNT_TYPE_LABELS,
} from "@/components/account-form";
import { mutate as swrMutate } from "swr";
import { useAccounts, useBalances } from "@/lib/hooks";
import { ApiError, deleteAccount, type AccountOut } from "@/lib/api";
import { ErrorDisplay, LoadingSpinner } from "@/components/ui-common";
import { cn, formatCurrency } from "@/lib/utils";
import { CategoryManager } from "@/components/category-manager";
import { SubaccountListEditor } from "@/components/subaccount-list-editor";

export default function SettingsPage() {
  const {
    data: accounts,
    error: accountsError,
    isLoading: accountsLoading,
    mutate: refreshAccounts,
  } = useAccounts(false);
  const { data: balances, mutate: refreshBalances } = useBalances();

  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<AccountOut | null>(null);
  const [pendingDelete, setPendingDelete] = useState<AccountOut | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const balanceMap = useMemo(() => {
    const m = new Map<number, string>();
    balances?.forEach((b) => m.set(b.account_id, b.balance));
    return m;
  }, [balances]);

  const handleConfirmDelete = async () => {
    if (!pendingDelete) return;
    setDeleteError(null);
    try {
      setDeleting(true);
      await deleteAccount(pendingDelete.id);
      setPendingDelete(null);
      refreshAccounts();
      refreshBalances();
      swrMutate((k) => typeof k === "string" && k.startsWith("accounts"), undefined, { revalidate: true });
    } catch (e) {
      setDeleteError(e instanceof ApiError ? e.message : "删除失败，请重试");
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="min-h-screen bg-background text-foreground pb-16 md:pb-0">
      <div className="mx-auto max-w-4xl px-4 py-6 md:px-6 lg:px-8">
        <div className="mb-6 flex items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">⚙️ 设置</h1>
            <p className="text-sm text-muted-foreground mt-1">
              管理银行账户、信用卡、券商、加密钱包等
            </p>
          </div>
          <button
            onClick={() => {
              setEditing(null);
              setShowForm(true);
            }}
            className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors shadow-sm"
          >
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
            </svg>
            添加账户
          </button>
        </div>

        <section>
          <div className="mb-3 flex items-center justify-between gap-2">
            <h2 className="text-base font-semibold">账户管理</h2>
            <a
              href="/assets"
              className="text-xs text-primary hover:underline"
            >
              也可在「资产」页管理 →
            </a>
          </div>
          {accountsLoading ? (
            <LoadingSpinner />
          ) : accountsError ? (
            <ErrorDisplay message="加载账户失败" onRetry={refreshAccounts} />
          ) : !accounts || accounts.length === 0 ? (
            <div className="rounded-xl border border-border bg-card p-12 text-center">
              <p className="text-base font-medium mb-1">暂无账户</p>
              <p className="text-sm text-muted-foreground mb-5">
                添加你的第一个账户，开始记录余额与交易
              </p>
              <button
                onClick={() => {
                  setEditing(null);
                  setShowForm(true);
                }}
                className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
              >
                <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
                </svg>
                添加账户
              </button>
            </div>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2">
              {accounts.map((a) => {
                const icon = ACCOUNT_TYPE_ICONS[a.type] ?? "📋";
                const typeLabel = ACCOUNT_TYPE_LABELS[a.type] ?? a.type;
                const balance = balanceMap.get(a.id) ?? a.initial_balance;
                return (
                  <div
                    key={a.id}
                    className={cn(
                      "rounded-xl border border-border bg-card p-5 transition-colors",
                      a.is_active
                        ? "hover:border-primary/40"
                        : "opacity-60",
                    )}
                  >
                    <div className="flex items-start justify-between mb-3 gap-3">
                      <div className="flex items-start gap-3 min-w-0">
                        <span className="text-2xl shrink-0" aria-hidden>
                          {icon}
                        </span>
                        <div className="min-w-0">
                          <p className="text-sm font-medium text-foreground truncate">
                            {a.name}
                          </p>
                          {a.institution && (
                            <p className="text-xs text-muted-foreground truncate">
                              {a.institution}
                            </p>
                          )}
                        </div>
                      </div>
                      <span className="text-[10px] px-2 py-0.5 rounded-md bg-muted text-muted-foreground font-medium shrink-0">
                        {typeLabel}
                      </span>
                    </div>
                    <div className="flex items-end justify-between gap-3">
                      <div>
                        <p className="text-xs text-muted-foreground mb-0.5">
                          当前余额 · {a.currency}
                        </p>
                        <p className="text-xl font-bold tabular-nums">
                          {formatCurrency(balance, a.currency)}
                        </p>
                      </div>
                      <div className="flex gap-1.5">
                        <button
                          onClick={() => {
                            setEditing(a);
                            setShowForm(true);
                          }}
                          className="text-xs px-2.5 py-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
                        >
                          编辑
                        </button>
                        <button
                          onClick={() => {
                            setDeleteError(null);
                            setPendingDelete(a);
                          }}
                          className="text-xs px-2.5 py-1.5 rounded-md text-rose-600 dark:text-rose-400 hover:bg-rose-500/10 transition-colors"
                        >
                          删除
                        </button>
                      </div>
                    </div>
                    {!a.is_active && (
                      <p className="text-[10px] text-muted-foreground mt-2">
                        已停用
                      </p>
                    )}
                    <SubaccountListEditor account={a} />
                  </div>
                );
              })}
            </div>
          )}
        </section>

        <section className="mt-10">
          <div className="mb-3">
            <h2 className="text-base font-semibold">分类管理</h2>
            <p className="text-xs text-muted-foreground mt-0.5">
              一级分类 → 二级分类两层结构。系统已预置 9 大类（住家 / 日常生活 / …）共 30 个二级，可随意增删改。
            </p>
          </div>
          <CategoryManager />
        </section>
      </div>

      {showForm && (
        <AccountForm
          initial={editing ?? undefined}
          isEdit={!!editing}
          onClose={() => {
            setShowForm(false);
            setEditing(null);
          }}
          onSuccess={() => {
            setShowForm(false);
            setEditing(null);
            refreshAccounts();
            refreshBalances();
            swrMutate((k) => typeof k === "string" && k.startsWith("accounts"), undefined, { revalidate: true });
          }}
        />
      )}

      {pendingDelete && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div
            className="fixed inset-0 bg-black/50 backdrop-blur-sm"
            onClick={() => !deleting && setPendingDelete(null)}
          />
          <div className="relative w-full max-w-sm rounded-xl border border-border bg-card p-6 shadow-xl">
            <h3 className="text-lg font-semibold mb-2">删除账户</h3>
            <p className="text-sm text-muted-foreground mb-2">
              确定删除「{pendingDelete.name}」？此操作不可撤销，相关交易记录将保留但失去账户关联。
            </p>
            {deleteError && (
              <div className="mb-3 p-2.5 rounded-md bg-destructive/10 border border-destructive/20 text-xs text-destructive">
                {deleteError}
              </div>
            )}
            <div className="flex gap-3 mt-5">
              <button
                onClick={() => setPendingDelete(null)}
                disabled={deleting}
                className="flex-1 px-4 py-2 text-sm font-medium rounded-lg border border-border hover:bg-muted transition-colors disabled:opacity-50"
              >
                取消
              </button>
              <button
                onClick={handleConfirmDelete}
                disabled={deleting}
                className="flex-1 px-4 py-2 text-sm font-medium rounded-lg bg-destructive text-destructive-foreground hover:bg-destructive/90 transition-colors disabled:opacity-50"
              >
                {deleting ? "删除中…" : "确认删除"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
