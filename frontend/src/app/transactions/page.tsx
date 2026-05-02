"use client";

import { useState, useCallback, useMemo } from "react";
import {
  useTransactions,
  useCategories,
  useAccounts,
} from "@/lib/hooks";
import {
  type TransactionFilters,
  type TransactionOut,
  deleteTransaction,
  ApiError,
} from "@/lib/api";
import { formatCurrency, formatDate, cn } from "@/lib/utils";
import { LoadingSpinner, ErrorDisplay } from "@/components/ui-common";
import { TransactionForm } from "@/components/transaction-form";
import { TransactionDetail } from "@/components/transaction-detail";
import { CategoryFilter } from "@/components/category-filter";
import { PdfImportPanel } from "@/components/pdf-import-panel";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";

type SortField = "occurred_at" | "amount" | "category";
type SortDir = "asc" | "desc";

const TYPE_OPTIONS = [
  { value: "", label: "全部类型" },
  { value: "expense", label: "支出" },
  { value: "income", label: "收入" },
  { value: "transfer", label: "转账" },
];

const ACCOUNT_TYPE_LABELS: Record<string, string> = {
  bank: "银行",
  credit_card: "信用卡",
  brokerage: "券商",
  crypto_wallet: "加密钱包",
  cash: "现金",
  other: "其他",
};

export default function TransactionsPage() {
  // ─── Filter state ─────────────────────────────────────────────────────
  const [search, setSearch] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [accountId, setAccountId] = useState<number | undefined>();
  const [categoryId, setCategoryId] = useState<number | undefined>();
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [sortField, setSortField] = useState<SortField>("occurred_at");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  // ─── UI state ─────────────────────────────────────────────────────────
  const [selectedTx, setSelectedTx] = useState<TransactionOut | null>(null);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState<number | null>(null);

  // ─── Data fetching ────────────────────────────────────────────────────
  const filters = useMemo<TransactionFilters>(() => {
    const f: TransactionFilters = {
      search: search || undefined,
      type: typeFilter || undefined,
      account_id: accountId,
      category_id: categoryId,
      from_date: fromDate || undefined,
      to_date: toDate || undefined,
      source: sourceFilter || undefined,
      limit: 100,
    };
    return f;
  }, [search, typeFilter, accountId, categoryId, fromDate, toDate, sourceFilter]);

  const {
    data: txResponse,
    error,
    isLoading,
    mutate: refresh,
  } = useTransactions(filters);

  const { data: categories } = useCategories();
  const { data: accounts } = useAccounts(true);

  // ─── Derived data ─────────────────────────────────────────────────────
  const transactions = useMemo(() => {
    if (!txResponse?.data) return [];
    let items = [...txResponse.data];
    items.sort((a, b) => {
      let cmp = 0;
      switch (sortField) {
        case "occurred_at":
          cmp = a.occurred_at.localeCompare(b.occurred_at);
          break;
        case "amount":
          cmp = parseFloat(a.amount) - parseFloat(b.amount);
          break;
        case "category":
          cmp = (a.category_name || "").localeCompare(b.category_name || "");
          break;
      }
      return sortDir === "desc" ? -cmp : cmp;
    });
    return items;
  }, [txResponse, sortField, sortDir]);

  const totalFiltered = txResponse?.meta?.total || 0;

  // ─── Handlers ─────────────────────────────────────────────────────────
  const handleSearch = useCallback(() => {
    setSearch(searchInput);
  }, [searchInput]);

  const handleClearFilters = useCallback(() => {
    setSearch("");
    setSearchInput("");
    setTypeFilter("");
    setAccountId(undefined);
    setCategoryId(undefined);
    setFromDate("");
    setToDate("");
    setSourceFilter("");
  }, []);

  const handleSort = useCallback(
    (field: SortField) => {
      if (sortField === field) {
        setSortDir((d) => (d === "asc" ? "desc" : "asc"));
      } else {
        setSortField(field);
        setSortDir("desc");
      }
    },
    [sortField]
  );

  const handleDelete = useCallback(
    async (id: number) => {
      try {
        await deleteTransaction(id);
        setDeleteConfirm(null);
        setSelectedTx(null);
        refresh();
      } catch (e) {
        console.error("Delete failed:", e);
      }
    },
    [refresh]
  );

  // ─── Category filter by kind ──────────────────────────────────────────
  const activeKind = typeFilter === "income" ? "income" : typeFilter === "transfer" ? "transfer" : "expense";
  const filteredCategories = categories?.filter((c) => c.kind === activeKind) || [];

  return (
    <div className="min-h-screen bg-background text-foreground pb-16 md:pb-0">
      <div className="mx-auto max-w-6xl px-4 py-6 md:px-6 lg:px-8">
        {/* ─── Header ──────────────────────────────────────────────── */}
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 mb-6">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">📝 交易记录</h1>
            <p className="text-sm text-muted-foreground mt-1">
              {totalFiltered} 笔交易
              {accountId && accounts?.find((a) => a.id === accountId)
                ? ` · ${accounts.find((a) => a.id === accountId)!.name}`
                : ""}
            </p>
          </div>
          <button
            onClick={() => setShowCreateForm(true)}
            className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
          >
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
            </svg>
            手动记账
          </button>
        </div>

        {/* ─── Tabs ────────────────────────────────────────────────── */}
        <Tabs defaultValue="list" className="w-full">
          <TabsList>
            <TabsTrigger value="list">交易记录</TabsTrigger>
            <TabsTrigger value="import">PDF 导入</TabsTrigger>
          </TabsList>

          <TabsContent value="list">
        {/* ─── Filters ─────────────────────────────────────────────── */}
        <div className="rounded-xl border border-border bg-card p-4 mb-4 space-y-3">
          {/* Row 1: Search + Type + Account */}
          <div className="flex flex-col sm:flex-row gap-3">
            <div className="flex flex-1 gap-2">
              <input
                type="text"
                placeholder="搜索描述、对方、备注…"
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSearch()}
                className="flex-1 px-3 py-2 text-sm rounded-lg border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              />
              <button
                onClick={handleSearch}
                className="px-3 py-2 text-sm font-medium rounded-lg border border-border hover:bg-muted transition-colors"
              >
                搜索
              </button>
            </div>
            <select
              value={typeFilter}
              onChange={(e) => {
                setTypeFilter(e.target.value);
                setCategoryId(undefined);
              }}
              className="px-3 py-2 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
            >
              {TYPE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
            <select
              value={accountId || ""}
              onChange={(e) => setAccountId(e.target.value ? Number(e.target.value) : undefined)}
              className="px-3 py-2 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
            >
              <option value="">全部账户</option>
              {accounts?.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name} ({a.currency})
                </option>
              ))}
            </select>
          </div>

          {/* Row 2: Date range + Source + Category */}
          <div className="flex flex-col sm:flex-row gap-3">
            <div className="flex items-center gap-2">
              <input
                type="date"
                value={fromDate}
                onChange={(e) => setFromDate(e.target.value)}
                className="px-3 py-2 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              />
              <span className="text-muted-foreground text-sm">—</span>
              <input
                type="date"
                value={toDate}
                onChange={(e) => setToDate(e.target.value)}
                className="px-3 py-2 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              />
            </div>
            <select
              value={sourceFilter}
              onChange={(e) => setSourceFilter(e.target.value)}
              className="px-3 py-2 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
            >
              <option value="">全部来源</option>
              <option value="manual">手动</option>
              <option value="pdf_import">PDF 导入</option>
              <option value="bank_api">银行 API</option>
              <option value="mcp_agent">MCP Agent</option>
            </select>
            <button
              onClick={handleClearFilters}
              className="px-3 py-2 text-sm text-muted-foreground hover:text-foreground transition-colors"
            >
              清除筛选
            </button>
          </div>

          {/* Row 3: Category quick filters (chips) */}
          {filteredCategories.length > 0 && (
            <CategoryFilter
              categories={filteredCategories}
              selected={categoryId}
              onSelect={setCategoryId}
            />
          )}
        </div>

        {/* ─── Error state ─────────────────────────────────────────── */}
        {error && !isLoading && (
          <ErrorDisplay
            message={error instanceof ApiError ? error.message : "加载失败"}
            onRetry={() => refresh()}
          />
        )}

        {/* ─── Loading state ───────────────────────────────────────── */}
        {isLoading && <LoadingSpinner />}

        {/* ─── Transaction list ────────────────────────────────────── */}
        {!isLoading && !error && (
          <>
            {transactions.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
                <svg className="h-12 w-12 mb-3 opacity-30" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
                </svg>
                <p className="text-sm">暂无交易记录</p>
                <button
                  onClick={() => setShowCreateForm(true)}
                  className="mt-3 px-4 py-1.5 text-sm text-primary hover:underline"
                >
                  添加第一笔交易
                </button>
              </div>
            ) : (
              <div className="rounded-xl border border-border bg-card overflow-hidden">
                {/* Table header */}
                <div className="hidden md:grid grid-cols-12 gap-2 px-4 py-3 text-xs font-medium text-muted-foreground border-b border-border bg-muted/30">
                  <button
                    onClick={() => handleSort("occurred_at")}
                    className="col-span-2 flex items-center gap-1 hover:text-foreground transition-colors text-left"
                  >
                    日期
                    {sortField === "occurred_at" && (
                      <span>{sortDir === "desc" ? "↓" : "↑"}</span>
                    )}
                  </button>
                  <div className="col-span-3">描述</div>
                  <button
                    onClick={() => handleSort("category")}
                    className="col-span-2 flex items-center gap-1 hover:text-foreground transition-colors text-left"
                  >
                    分类
                    {sortField === "category" && (
                      <span>{sortDir === "desc" ? "↓" : "↑"}</span>
                    )}
                  </button>
                  <div className="col-span-1 text-right">账户</div>
                  <button
                    onClick={() => handleSort("amount")}
                    className="col-span-2 text-right flex items-center justify-end gap-1 hover:text-foreground transition-colors"
                  >
                    金额
                    {sortField === "amount" && (
                      <span>{sortDir === "desc" ? "↓" : "↑"}</span>
                    )}
                  </button>
                  <div className="col-span-2 text-right">操作</div>
                </div>

                {/* Table body */}
                {transactions.map((tx) => (
                  <TransactionRow
                    key={tx.id}
                    tx={tx}
                    isSelected={selectedTx?.id === tx.id}
                    onSelect={() => setSelectedTx(tx)}
                    onDelete={() => setDeleteConfirm(tx.id)}
                    isDeleteConfirm={deleteConfirm === tx.id}
                    onConfirmDelete={() => handleDelete(tx.id)}
                    onCancelDelete={() => setDeleteConfirm(null)}
                  />
                ))}
              </div>
            )}
          </>
        )}
          </TabsContent>

          <TabsContent value="import">
            <PdfImportPanel />
          </TabsContent>
        </Tabs>
      </div>

      {/* ─── Sliding panels ───────────────────────────────────────────── */}
      {showCreateForm && (
        <TransactionForm
          accounts={accounts || []}
          categories={categories || []}
          onClose={() => setShowCreateForm(false)}
          onSuccess={() => {
            setShowCreateForm(false);
            refresh();
          }}
        />
      )}

      {selectedTx && (
        <TransactionDetail
          tx={selectedTx}
          accounts={accounts || []}
          categories={categories || []}
          onClose={() => setSelectedTx(null)}
          onUpdate={() => refresh()}
        />
      )}
    </div>
  );
}

// ─── Transaction Row ─────────────────────────────────────────────────────

function TransactionRow({
  tx,
  isSelected,
  onSelect,
  onDelete,
  isDeleteConfirm,
  onConfirmDelete,
  onCancelDelete,
}: {
  tx: TransactionOut;
  isSelected: boolean;
  onSelect: () => void;
  onDelete: () => void;
  isDeleteConfirm: boolean;
  onConfirmDelete: () => void;
  onCancelDelete: () => void;
}) {
  const typeColors: Record<string, string> = {
    expense: "text-red-500",
    income: "text-green-500",
    transfer: "text-blue-500",
    adjustment: "text-yellow-500",
  };

  const typePrefix: Record<string, string> = {
    expense: "-",
    income: "+",
    transfer: "",
    adjustment: "",
  };

  const sourceLabels: Record<string, string> = {
    manual: "手动",
    pdf_import: "PDF",
    bank_api: "API",
    mcp_agent: "MCP",
  };

  return (
    <div
      onClick={onSelect}
      className={cn(
        "grid grid-cols-1 md:grid-cols-12 gap-1 md:gap-2 px-4 py-3 border-b border-border last:border-b-0 cursor-pointer transition-colors hover:bg-muted/50",
        isSelected && "bg-muted"
      )}
    >
      {/* Date */}
      <div className="md:col-span-2 text-sm text-muted-foreground">
        {formatDate(tx.occurred_at)}
        {tx.is_pending && (
          <span className="ml-1.5 inline-block px-1.5 py-0.5 text-[10px] font-medium rounded bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400">
            待确认
          </span>
        )}
      </div>

      {/* Description */}
      <div className="md:col-span-3 text-sm font-medium text-foreground truncate">
        {tx.description || tx.raw_description || "—"}
        {tx.counterparty && (
          <span className="block text-xs text-muted-foreground truncate">
            {tx.counterparty}
          </span>
        )}
      </div>

      {/* Category */}
      <div className="md:col-span-2 text-sm">
        {tx.category_name ? (
          <span className="inline-block px-2 py-0.5 text-xs rounded-full bg-muted text-foreground">
            {tx.category_name}
          </span>
        ) : (
          <span className="text-muted-foreground text-xs">未分类</span>
        )}
      </div>

      {/* Account */}
      <div className="hidden md:block md:col-span-1 text-sm text-muted-foreground text-right">
        {tx.account_name || `#${tx.account_id}`}
      </div>

      {/* Amount */}
      <div className="md:col-span-2 text-sm font-semibold text-right">
        <span className={typeColors[tx.type] || ""}>
          {typePrefix[tx.type]}
          {formatCurrency(parseFloat(tx.amount), tx.currency)}
        </span>
      </div>

      {/* Actions */}
      <div className="hidden md:flex md:col-span-2 items-center justify-end gap-1" onClick={(e) => e.stopPropagation()}>
        {isDeleteConfirm ? (
          <div className="flex items-center gap-1">
            <span className="text-xs text-destructive">删除？</span>
            <button
              onClick={onConfirmDelete}
              className="px-2 py-1 text-xs font-medium rounded bg-destructive text-destructive-foreground hover:bg-destructive/90 transition-colors"
            >
              确认
            </button>
            <button
              onClick={onCancelDelete}
              className="px-2 py-1 text-xs rounded border border-border hover:bg-muted transition-colors"
            >
              取消
            </button>
          </div>
        ) : (
          <>
            <button
              onClick={onSelect}
              className="p-1.5 rounded-md hover:bg-muted transition-colors text-muted-foreground hover:text-foreground"
              title="查看详情"
            >
              <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
              </svg>
            </button>
            <button
              onClick={onDelete}
              className="p-1.5 rounded-md hover:bg-red-50 dark:hover:bg-red-950/30 transition-colors text-muted-foreground hover:text-destructive"
              title="删除"
            >
              <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
              </svg>
            </button>
          </>
        )}
      </div>

      {/* Mobile: source badge */}
      <div className="md:hidden flex items-center gap-2 mt-1">
        <span className="text-[10px] text-muted-foreground">
          {sourceLabels[tx.source] || tx.source}
        </span>
        <span className="text-[10px] text-muted-foreground">
          {tx.account_name}
        </span>
      </div>
    </div>
  );
}
