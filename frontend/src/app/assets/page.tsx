"use client";

import { useMemo, useState } from "react";
import {
  PieChart,
  Pie,
  Cell,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import {
  useHoldings,
  usePortfolioSummary,
  usePortfolioBreakdown,
  useBalances,
  useAccounts,
  useAssets,
  useNetWorth,
} from "@/lib/hooks";
import {
  ASSET_CLASS_LABELS,
  ASSET_CLASS_COLORS,
  CHART_COLORS,
  cn,
  formatCurrency,
  formatNumber,
} from "@/lib/utils";
import {
  adjustAccountBalance,
  ApiError,
  deleteHolding,
  type BalanceOut,
  type HoldingOut,
} from "@/lib/api";
import { ErrorDisplay, LoadingSpinner } from "@/components/ui-common";
import { HoldingForm } from "@/components/holding-form";

type SortKey =
  | "asset_name"
  | "asset_class"
  | "account_name"
  | "quantity"
  | "avg_cost"
  | "current_price"
  | "market_value"
  | "pnl_percent";
type SortDir = "asc" | "desc";

const ASSET_CLASS_BADGE: Record<string, string> = {
  cash: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/20",
  a_share: "bg-rose-500/10 text-rose-600 dark:text-rose-400 border-rose-500/20",
  eu_stock: "bg-cyan-500/10 text-cyan-600 dark:text-cyan-400 border-cyan-500/20",
  us_stock: "bg-blue-500/10 text-blue-600 dark:text-blue-400 border-blue-500/20",
  crypto: "bg-orange-500/10 text-orange-600 dark:text-orange-400 border-orange-500/20",
  gold: "bg-yellow-500/10 text-yellow-700 dark:text-yellow-400 border-yellow-500/20",
  bond: "bg-teal-500/10 text-teal-600 dark:text-teal-400 border-teal-500/20",
  fund: "bg-violet-500/10 text-violet-600 dark:text-violet-400 border-violet-500/20",
  other: "bg-muted text-muted-foreground border-border",
};

function pnlPercent(h: HoldingOut): number | null {
  const cost = parseFloat(h.avg_cost ?? "");
  const price = parseFloat(h.current_price ?? "");
  if (!cost || isNaN(cost) || !price || isNaN(price) || cost === 0) return null;
  return ((price - cost) / cost) * 100;
}

export default function AssetsPage() {
  const [tab, setTab] = useState<"holdings" | "distribution" | "balances">("holdings");
  const [filterClass, setFilterClass] = useState<string>("");
  const [sortKey, setSortKey] = useState<SortKey>("market_value");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<HoldingOut | null>(null);
  const [pendingDelete, setPendingDelete] = useState<HoldingOut | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [distMode, setDistMode] = useState<"class" | "currency">("class");
  const [adjustTarget, setAdjustTarget] = useState<BalanceOut | null>(null);

  const { data: summary, mutate: refreshSummary } = usePortfolioSummary();
  const { data: netWorth, isLoading: netWorthLoading, mutate: refreshNetWorth } = useNetWorth();
  const { data: holdings, error: holdingsError, isLoading: holdingsLoading, mutate: refreshHoldings } = useHoldings();
  const { data: breakdown, isLoading: breakdownLoading, mutate: refreshBreakdown } = usePortfolioBreakdown();
  const { data: balances, isLoading: balancesLoading, mutate: refreshBalances } = useBalances();
  const { data: accounts } = useAccounts(true);
  const { data: assets, mutate: refreshAssets } = useAssets();

  const refreshAll = () => {
    refreshSummary();
    refreshNetWorth();
    refreshHoldings();
    refreshBreakdown();
    refreshBalances();
    refreshAssets();
  };

  const totalUnrealizedPnl = useMemo(() => {
    if (!holdings) return 0;
    return holdings.reduce((sum, h) => {
      const v = parseFloat(h.unrealized_pnl ?? "");
      return sum + (isNaN(v) ? 0 : v);
    }, 0);
  }, [holdings]);

  const distinctClasses = useMemo(() => {
    if (!holdings) return 0;
    return new Set(holdings.map((h) => h.asset_class).filter(Boolean)).size;
  }, [holdings]);

  function sortValue(h: HoldingOut, key: SortKey): number | string | null {
    switch (key) {
      case "asset_name":
        return h.asset_name ?? h.symbol ?? "";
      case "asset_class":
        return h.asset_class ?? "";
      case "account_name":
        return h.account_name ?? "";
      case "quantity":
        return parseFloat(h.quantity) || 0;
      case "avg_cost":
        return h.avg_cost ? parseFloat(h.avg_cost) : null;
      case "current_price":
        return h.current_price ? parseFloat(h.current_price) : null;
      case "market_value":
        return h.market_value ? parseFloat(h.market_value) : 0;
      case "pnl_percent":
        return pnlPercent(h);
    }
  }

  const filteredHoldings = useMemo(() => {
    if (!holdings) return [];
    const list = filterClass
      ? holdings.filter((h) => h.asset_class === filterClass)
      : [...holdings];

    list.sort((a, b) => {
      const dir = sortDir === "asc" ? 1 : -1;
      const av = sortValue(a, sortKey);
      const bv = sortValue(b, sortKey);
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === "number" && typeof bv === "number") {
        return (av - bv) * dir;
      }
      return String(av).localeCompare(String(bv)) * dir;
    });

    return list;
  }, [holdings, filterClass, sortKey, sortDir]);

  function handleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir(sortDir === "asc" ? "desc" : "asc");
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  async function handleConfirmDelete() {
    if (!pendingDelete) return;
    setDeleteError(null);
    try {
      setDeleting(true);
      await deleteHolding(pendingDelete.id);
      setPendingDelete(null);
      refreshAll();
    } catch (e) {
      if (e instanceof ApiError) setDeleteError(e.message);
      else setDeleteError("删除失败，请重试");
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="min-h-screen bg-background text-foreground pb-16 md:pb-0">
      <div className="mx-auto max-w-7xl px-4 py-6 md:px-6 lg:px-8">
        <div className="mb-6 flex items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">💼 资产</h1>
            <p className="text-sm text-muted-foreground mt-1">
              管理您的银行账户、投资和加密资产
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
            添加持仓
          </button>
        </div>

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4 mb-6">
          <div className="rounded-xl border border-border bg-card p-5">
            <p className="text-xs text-muted-foreground mb-1.5">现金总额</p>
            {netWorthLoading ? (
              <div className="h-7 w-32 animate-pulse rounded bg-muted" />
            ) : (
              <p className="text-2xl font-semibold tabular-nums">
                {formatCurrency(netWorth?.cash_total ?? "0", netWorth?.base_currency ?? "EUR")}
              </p>
            )}
          </div>
          <div className="rounded-xl border border-border bg-card p-5">
            <p className="text-xs text-muted-foreground mb-1.5">投资总额</p>
            {netWorthLoading ? (
              <div className="h-7 w-32 animate-pulse rounded bg-muted" />
            ) : (
              <p className="text-2xl font-semibold tabular-nums">
                {formatCurrency(netWorth?.investment_total ?? "0", netWorth?.base_currency ?? "EUR")}
              </p>
            )}
          </div>
          <div className="rounded-xl border-2 border-primary/40 bg-gradient-to-br from-primary/10 to-primary/[0.02] p-5 sm:col-span-2 lg:col-span-2">
            <p className="text-xs text-primary/80 mb-1.5 font-medium">总净值</p>
            {netWorthLoading ? (
              <div className="h-9 w-40 animate-pulse rounded bg-muted" />
            ) : (
              <p className="text-3xl font-bold tracking-tight tabular-nums">
                {formatCurrency(netWorth?.net_worth ?? "0", netWorth?.base_currency ?? "EUR")}
              </p>
            )}
            {netWorth?.as_of && (
              <p className="text-xs text-muted-foreground mt-2">
                截至 {new Date(netWorth.as_of).toLocaleString("zh-CN")}
              </p>
            )}
          </div>
          <StatCard label="持仓数" value={holdings ? String(holdings.length) : "—"} loading={holdingsLoading} />
          <StatCard label="资产类型" value={holdings ? String(distinctClasses) : "—"} loading={holdingsLoading} />
          <div className="rounded-xl border border-border bg-card p-5 sm:col-span-2 lg:col-span-2">
            <p className="text-xs text-muted-foreground mb-1.5">未实现盈亏</p>
            {holdingsLoading ? (
              <div className="h-7 w-32 animate-pulse rounded bg-muted" />
            ) : (
              <p
                className={cn(
                  "text-2xl font-semibold tabular-nums",
                  totalUnrealizedPnl > 0
                    ? "text-emerald-600 dark:text-emerald-400"
                    : totalUnrealizedPnl < 0
                      ? "text-rose-600 dark:text-rose-400"
                      : "text-foreground",
                )}
              >
                {totalUnrealizedPnl >= 0 ? "+" : ""}
                {formatCurrency(totalUnrealizedPnl, summary?.base_currency ?? "EUR")}
              </p>
            )}
          </div>
        </div>

        <div className="flex items-center gap-1 border-b border-border mb-6 overflow-x-auto">
          {([
            { value: "holdings", label: "持仓" },
            { value: "distribution", label: "资产分布" },
            { value: "balances", label: "账户余额" },
          ] as const).map((opt) => (
            <button
              key={opt.value}
              onClick={() => setTab(opt.value)}
              className={cn(
                "px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px whitespace-nowrap",
                tab === opt.value
                  ? "border-primary text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              {opt.label}
            </button>
          ))}
        </div>

        {tab === "holdings" && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-3">
              <label className="text-sm text-muted-foreground">按类型筛选：</label>
              <select
                value={filterClass}
                onChange={(e) => setFilterClass(e.target.value)}
                className="px-3 py-1.5 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              >
                <option value="">全部</option>
                {Object.entries(ASSET_CLASS_LABELS).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
              </select>
              {filterClass && (
                <button
                  onClick={() => setFilterClass("")}
                  className="text-xs text-muted-foreground hover:text-foreground underline"
                >
                  清除
                </button>
              )}
              <span className="text-xs text-muted-foreground ml-auto">
                共 {filteredHoldings.length} 项
              </span>
            </div>

            {holdingsLoading ? (
              <LoadingSpinner />
            ) : holdingsError ? (
              <ErrorDisplay message="加载持仓失败" onRetry={refreshHoldings} />
            ) : filteredHoldings.length === 0 ? (
              <EmptyHoldings
                hasAny={(holdings?.length ?? 0) > 0}
                onAdd={() => {
                  setEditing(null);
                  setShowForm(true);
                }}
              />
            ) : (
              <div className="rounded-xl border border-border bg-card overflow-hidden">
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead className="bg-muted/40 text-xs uppercase text-muted-foreground">
                      <tr>
                        <Th label="资产" sortKey="asset_name" current={sortKey} dir={sortDir} onSort={handleSort} />
                        <Th label="类型" sortKey="asset_class" current={sortKey} dir={sortDir} onSort={handleSort} />
                        <Th label="账户" sortKey="account_name" current={sortKey} dir={sortDir} onSort={handleSort} />
                        <Th label="数量" sortKey="quantity" current={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                        <Th label="成本价" sortKey="avg_cost" current={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                        <Th label="当前价" sortKey="current_price" current={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                        <Th label="市值" sortKey="market_value" current={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                        <Th label="盈亏%" sortKey="pnl_percent" current={sortKey} dir={sortDir} onSort={handleSort} align="right" />
                        <th className="px-4 py-3 text-right">操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filteredHoldings.map((h) => {
                        const pnl = pnlPercent(h);
                        const cls = h.asset_class ?? "other";
                        return (
                          <tr key={h.id} className="border-t border-border hover:bg-muted/30 transition-colors">
                            <td className="px-4 py-3">
                              <div className="font-medium text-foreground">{h.asset_name || "—"}</div>
                              {h.symbol && <div className="text-xs text-muted-foreground">{h.symbol}</div>}
                            </td>
                            <td className="px-4 py-3">
                              <span
                                className={cn(
                                  "inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-md border",
                                  ASSET_CLASS_BADGE[cls] || ASSET_CLASS_BADGE.other,
                                )}
                              >
                                {ASSET_CLASS_LABELS[cls] || cls}
                              </span>
                            </td>
                            <td className="px-4 py-3 text-muted-foreground">{h.account_name || "—"}</td>
                            <td className="px-4 py-3 text-right tabular-nums">{formatNumber(h.quantity)}</td>
                            <td className="px-4 py-3 text-right tabular-nums text-muted-foreground">
                              {h.avg_cost ? formatCurrency(h.avg_cost, h.cost_currency || "EUR") : "—"}
                            </td>
                            <td className="px-4 py-3 text-right tabular-nums">
                              {h.current_price ? formatCurrency(h.current_price, h.cost_currency || "EUR") : "—"}
                            </td>
                            <td className="px-4 py-3 text-right font-medium tabular-nums">
                              {h.market_value ? formatCurrency(h.market_value, h.cost_currency || "EUR") : "—"}
                            </td>
                            <td
                              className={cn(
                                "px-4 py-3 text-right font-medium tabular-nums",
                                pnl == null
                                  ? "text-muted-foreground"
                                  : pnl > 0
                                    ? "text-emerald-600 dark:text-emerald-400"
                                    : pnl < 0
                                      ? "text-rose-600 dark:text-rose-400"
                                      : "text-muted-foreground",
                              )}
                            >
                              {pnl == null ? "—" : `${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}%`}
                            </td>
                            <td className="px-4 py-3 text-right whitespace-nowrap">
                              <button
                                onClick={() => {
                                  setEditing(h);
                                  setShowForm(true);
                                }}
                                className="text-xs px-2 py-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
                              >
                                编辑
                              </button>
                              <button
                                onClick={() => {
                                  setDeleteError(null);
                                  setPendingDelete(h);
                                }}
                                className="text-xs px-2 py-1 rounded-md text-rose-600 dark:text-rose-400 hover:bg-rose-500/10 transition-colors ml-1"
                              >
                                删除
                              </button>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}

        {tab === "distribution" && (
          <div className="space-y-4">
            <div className="flex items-center gap-3">
              <span className="text-sm text-muted-foreground">视图：</span>
              <div className="inline-flex rounded-lg border border-border bg-card p-1">
                {([
                  { value: "class", label: "按类型" },
                  { value: "currency", label: "按币种" },
                ] as const).map((m) => (
                  <button
                    key={m.value}
                    onClick={() => setDistMode(m.value)}
                    className={cn(
                      "px-3 py-1.5 text-xs font-medium rounded-md transition-colors",
                      distMode === m.value
                        ? "bg-primary text-primary-foreground"
                        : "text-muted-foreground hover:text-foreground",
                    )}
                  >
                    {m.label}
                  </button>
                ))}
              </div>
            </div>

            {breakdownLoading ? (
              <LoadingSpinner />
            ) : (
              <DistributionPanel
                mode={distMode}
                breakdown={breakdown}
                baseCurrency={summary?.base_currency ?? "EUR"}
              />
            )}
          </div>
        )}

        {tab === "balances" && (
          <div className="space-y-4">
            {balancesLoading ? (
              <LoadingSpinner />
            ) : !balances || balances.length === 0 ? (
              <div className="rounded-xl border border-border bg-card p-12 text-center">
                <p className="text-sm text-muted-foreground">暂无账户余额数据</p>
              </div>
            ) : (
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {balances.map((b) => {
                  const account = accounts?.find((a) => a.id === b.account_id);
                  return (
                    <div
                      key={b.account_id}
                      className="rounded-xl border border-border bg-card p-5 hover:border-primary/40 transition-colors"
                    >
                      <div className="flex items-start justify-between mb-4 gap-2">
                        <div className="min-w-0">
                          <p className="text-sm font-medium text-foreground truncate">{b.account_name}</p>
                          {account?.institution && (
                            <p className="text-xs text-muted-foreground mt-0.5 truncate">{account.institution}</p>
                          )}
                        </div>
                        <div className="flex items-center gap-1.5 shrink-0">
                          <button
                            onClick={() => setAdjustTarget(b)}
                            className="text-xs px-2 py-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
                          >
                            调整
                          </button>
                          <span className="text-xs px-2 py-0.5 rounded-md bg-muted text-muted-foreground font-medium">
                            {b.currency}
                          </span>
                        </div>
                      </div>
                      <p className="text-2xl font-bold tabular-nums">
                        {formatCurrency(b.balance, b.currency)}
                      </p>
                      {account?.type && (
                        <p className="text-xs text-muted-foreground mt-2 capitalize">{account.type}</p>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>

      {showForm && accounts && (
        <HoldingForm
          accounts={accounts}
          assets={assets ?? []}
          isEdit={!!editing}
          initialHolding={editing ?? undefined}
          onClose={() => {
            setShowForm(false);
            setEditing(null);
          }}
          onSuccess={() => {
            setShowForm(false);
            setEditing(null);
            refreshAll();
          }}
        />
      )}

      {adjustTarget && (
        <BalanceAdjustDialog
          balance={adjustTarget}
          onClose={() => setAdjustTarget(null)}
          onSuccess={() => {
            setAdjustTarget(null);
            refreshAll();
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
            <h3 className="text-lg font-semibold mb-2">删除持仓</h3>
            <p className="text-sm text-muted-foreground mb-2">
              确定删除「{pendingDelete.asset_name || pendingDelete.symbol || "此持仓"}」？此操作不可撤销。
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

function StatCard({ label, value, loading }: { label: string; value: string; loading: boolean }) {
  return (
    <div className="rounded-xl border border-border bg-card p-5">
      <p className="text-xs text-muted-foreground mb-1.5">{label}</p>
      {loading ? (
        <div className="h-7 w-16 animate-pulse rounded bg-muted" />
      ) : (
        <p className="text-2xl font-semibold tabular-nums">{value}</p>
      )}
    </div>
  );
}

interface ThProps {
  label: string;
  sortKey: SortKey;
  current: SortKey;
  dir: SortDir;
  onSort: (k: SortKey) => void;
  align?: "left" | "right";
}

function Th({ label, sortKey, current, dir, onSort, align = "left" }: ThProps) {
  const active = current === sortKey;
  return (
    <th
      className={cn(
        "px-4 py-3 font-medium select-none cursor-pointer hover:text-foreground transition-colors",
        align === "right" ? "text-right" : "text-left",
      )}
      onClick={() => onSort(sortKey)}
    >
      <span className="inline-flex items-center gap-1">
        {label}
        {active && <span className="text-foreground">{dir === "asc" ? "▲" : "▼"}</span>}
      </span>
    </th>
  );
}

interface DistPanelProps {
  mode: "class" | "currency";
  breakdown:
    | {
        by_class: Record<string, { value: string; count: number; assets: Array<{ symbol: string; name: string; value: string }> }>;
        by_currency: Record<string, { value: string; count: number }>;
      }
    | undefined;
  baseCurrency: string;
}

function DistributionPanel({ mode, breakdown, baseCurrency }: DistPanelProps) {
  const source = mode === "class" ? breakdown?.by_class : breakdown?.by_currency;
  const entries = source ? Object.entries(source) : [];

  if (entries.length === 0) {
    return (
      <div className="rounded-xl border border-border bg-card p-12 text-center">
        <p className="text-sm text-muted-foreground">暂无持仓数据</p>
      </div>
    );
  }

  const total = entries.reduce((s, [, v]) => s + parseFloat(v.value || "0"), 0);

  const pieData = entries.map(([key, val], i) => ({
    name: mode === "class" ? (ASSET_CLASS_LABELS[key] || key) : key,
    value: parseFloat(val.value || "0"),
    count: val.count,
    fill:
      mode === "class"
        ? ASSET_CLASS_COLORS[key] || CHART_COLORS[i % CHART_COLORS.length]
        : CHART_COLORS[i % CHART_COLORS.length],
    percent: total > 0 ? (parseFloat(val.value || "0") / total) * 100 : 0,
  }));

  return (
    <div className="rounded-xl border border-border bg-card p-4 md:p-6">
      <h3 className="text-base font-semibold mb-4">
        {mode === "class" ? "资产分布（按类型）" : "资产分布（按币种）"}
      </h3>
      <div className="flex flex-col lg:flex-row items-center gap-6">
        <ResponsiveContainer width="100%" height={300} className="max-w-[360px]">
          <PieChart>
            <Pie
              data={pieData}
              cx="50%"
              cy="50%"
              innerRadius={70}
              outerRadius={110}
              paddingAngle={2}
              dataKey="value"
              stroke="none"
            >
              {pieData.map((entry, i) => (
                <Cell key={i} fill={entry.fill} />
              ))}
            </Pie>
            <Tooltip
              content={({ active, payload }) => {
                if (!active || !payload?.length) return null;
                const d = payload[0];
                const p = d.payload as { fill: string; percent: number };
                return (
                  <div className="rounded-lg border border-border bg-card px-3 py-2 shadow-lg text-sm">
                    <div className="flex items-center gap-2 mb-1">
                      <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ backgroundColor: p.fill }} />
                      <span className="font-medium text-foreground">{d.name}</span>
                    </div>
                    <p className="text-muted-foreground">
                      {formatCurrency(Number(d.value ?? 0), baseCurrency)} ({(p.percent ?? 0).toFixed(1)}%)
                    </p>
                  </div>
                );
              }}
            />
          </PieChart>
        </ResponsiveContainer>
        <div className="flex flex-col gap-2 text-sm w-full lg:flex-1">
          {pieData.map((d) => (
            <div key={d.name} className="flex items-center justify-between gap-4">
              <div className="flex items-center gap-2">
                <span className="inline-block h-3 w-3 rounded-sm" style={{ backgroundColor: d.fill }} />
                <span className="text-foreground">{d.name}</span>
                <span className="text-xs text-muted-foreground">({d.count}项)</span>
              </div>
              <div className="text-right">
                <span className="font-medium text-foreground">
                  {formatCurrency(d.value, baseCurrency)}
                </span>
                <span className="text-muted-foreground ml-2">({d.percent.toFixed(1)}%)</span>
              </div>
            </div>
          ))}
          <div className="border-t border-border pt-2 flex items-center justify-between gap-4">
            <span className="font-medium">总计</span>
            <span className="font-bold">{formatCurrency(total, baseCurrency)}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

interface BalanceAdjustDialogProps {
  balance: BalanceOut;
  onClose: () => void;
  onSuccess: () => void;
}

function BalanceAdjustDialog({ balance, onClose, onSuccess }: BalanceAdjustDialogProps) {
  const [target, setTarget] = useState(balance.balance);
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    const num = parseFloat(target);
    if (isNaN(num)) {
      setError("请输入有效金额");
      return;
    }
    try {
      setSubmitting(true);
      await adjustAccountBalance(balance.account_id, {
        target_balance: target,
        note: note.trim() || undefined,
      });
      onSuccess();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "调整失败，请重试");
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
      <div className="relative w-full max-w-md rounded-xl border border-border bg-card p-6 shadow-xl">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold">调整余额</h2>
          <button
            type="button"
            onClick={onClose}
            className="p-2 rounded-lg hover:bg-muted transition-colors text-muted-foreground"
            aria-label="关闭"
          >
            <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="mb-4 p-3 rounded-lg bg-muted/40 border border-border">
          <p className="text-xs text-muted-foreground mb-0.5">{balance.account_name}</p>
          <p className="text-sm font-medium tabular-nums">
            当前余额：{formatCurrency(balance.balance, balance.currency)}
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium mb-2">
              目标余额 <span className="text-destructive">*</span>
            </label>
            <input
              type="number"
              step="any"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              placeholder="0.00"
              required
              autoFocus
              className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
            />
            <p className="text-xs text-muted-foreground mt-1.5">
              将创建一笔差额调整交易，使账户余额等于此值（{balance.currency}）
            </p>
          </div>

          <div>
            <label className="block text-sm font-medium mb-2">备注</label>
            <input
              type="text"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="选填，如：月末对账"
              className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>

          {error && (
            <div className="p-3 rounded-lg bg-destructive/10 border border-destructive/20 text-sm text-destructive">
              {error}
            </div>
          )}

          <div className="flex gap-3 pt-1">
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
              {submitting ? "保存中…" : "确认调整"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function EmptyHoldings({ hasAny, onAdd }: { hasAny: boolean; onAdd: () => void }) {
  return (
    <div className="rounded-xl border border-border bg-card p-12 text-center">
      <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-muted">
        <svg className="h-6 w-6 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
      </div>
      <p className="text-base font-medium mb-1">
        {hasAny ? "当前筛选下暂无持仓" : "暂无持仓"}
      </p>
      <p className="text-sm text-muted-foreground mb-5">
        {hasAny ? "试着切换或清除筛选条件" : "添加你的第一个持仓，开始跟踪你的投资组合"}
      </p>
      {!hasAny && (
        <button
          onClick={onAdd}
          className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
        >
          <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
          </svg>
          添加你的第一个持仓
        </button>
      )}
    </div>
  );
}
