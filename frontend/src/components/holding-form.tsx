"use client";

import { useState } from "react";
import {
  type AccountOut,
  type AssetOut,
  type HoldingOut,
  ApiError,
  createAsset,
  createHolding,
  updateHolding,
} from "@/lib/api";
import { ASSET_CLASS_LABELS, cn } from "@/lib/utils";

interface HoldingFormProps {
  accounts: AccountOut[];
  assets: AssetOut[];
  onClose: () => void;
  onSuccess: () => void;
  initialHolding?: HoldingOut;
  isEdit?: boolean;
}

const ASSET_CLASS_OPTIONS: Array<{ value: string; label: string }> = Object.entries(
  ASSET_CLASS_LABELS,
).map(([value, label]) => ({ value, label }));

const CURRENCY_OPTIONS = ["EUR", "USD", "CNY", "GBP", "JPY", "HKD"];

export function HoldingForm({
  accounts,
  assets,
  onClose,
  onSuccess,
  initialHolding,
  isEdit = false,
}: HoldingFormProps) {
  const [mode, setMode] = useState<"existing" | "new">("existing");

  const [assetId, setAssetId] = useState<number | null>(
    initialHolding?.asset_id ?? (assets[0]?.id ?? null),
  );

  const [newSymbol, setNewSymbol] = useState("");
  const [newName, setNewName] = useState("");
  const [newAssetClass, setNewAssetClass] = useState("cash");
  const [newCurrency, setNewCurrency] = useState("EUR");

  const [accountId, setAccountId] = useState<number>(
    initialHolding?.account_id ?? (accounts[0]?.id ?? 0),
  );
  const [quantity, setQuantity] = useState(initialHolding?.quantity ?? "");
  const [avgCost, setAvgCost] = useState(initialHolding?.avg_cost ?? "");
  const [costCurrency, setCostCurrency] = useState(
    initialHolding?.cost_currency ?? "EUR",
  );

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (!accountId) {
      setError("请选择账户");
      return;
    }
    const qNum = parseFloat(quantity);
    if (!quantity || isNaN(qNum) || qNum <= 0) {
      setError("数量必须大于 0");
      return;
    }

    try {
      setSubmitting(true);

      if (isEdit && initialHolding) {
        await updateHolding(initialHolding.id, {
          quantity,
          avg_cost: avgCost || undefined,
          cost_currency: costCurrency || undefined,
        });
      } else {
        let resolvedAssetId: number | null = assetId;

        if (mode === "new") {
          if (!newSymbol.trim() || !newName.trim()) {
            setError("请填写资产代码和名称");
            setSubmitting(false);
            return;
          }
          const asset = await createAsset({
            symbol: newSymbol.trim(),
            name: newName.trim(),
            asset_class: newAssetClass,
            currency: newCurrency,
          });
          resolvedAssetId = asset.id;
        }

        if (!resolvedAssetId) {
          setError("请选择资产");
          setSubmitting(false);
          return;
        }

        await createHolding({
          account_id: accountId,
          asset_id: resolvedAssetId,
          quantity,
          avg_cost: avgCost || undefined,
          cost_currency: costCurrency || undefined,
        });
      }

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
      <div className="fixed inset-0 bg-black/40 backdrop-blur-sm" onClick={onClose} />
      <div className="fixed right-0 top-0 bottom-0 w-full max-w-md bg-card border-l border-border overflow-y-auto shadow-2xl">
        <div className="p-6">
          <div className="flex items-center justify-between mb-6">
            <h2 className="text-lg font-semibold">
              {isEdit ? "编辑持仓" : "添加持仓"}
            </h2>
            <button
              onClick={onClose}
              className="p-2 rounded-lg hover:bg-muted transition-colors text-muted-foreground"
              aria-label="关闭"
            >
              <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          <form onSubmit={handleSubmit} className="space-y-5">
            {!isEdit && (
              <div>
                <label className="block text-sm font-medium mb-2">资产来源</label>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => setMode("existing")}
                    className={cn(
                      "flex-1 py-2.5 text-sm font-medium rounded-lg border-2 transition-all",
                      mode === "existing"
                        ? "border-primary bg-primary/5 text-foreground"
                        : "border-border hover:border-muted-foreground/30 text-muted-foreground",
                    )}
                  >
                    选择已有资产
                  </button>
                  <button
                    type="button"
                    onClick={() => setMode("new")}
                    className={cn(
                      "flex-1 py-2.5 text-sm font-medium rounded-lg border-2 transition-all",
                      mode === "new"
                        ? "border-primary bg-primary/5 text-foreground"
                        : "border-border hover:border-muted-foreground/30 text-muted-foreground",
                    )}
                  >
                    新建资产
                  </button>
                </div>
              </div>
            )}

            {!isEdit && mode === "existing" && (
              <div>
                <label className="block text-sm font-medium mb-2">
                  资产 <span className="text-destructive">*</span>
                </label>
                <select
                  value={assetId ?? ""}
                  onChange={(e) => setAssetId(e.target.value ? Number(e.target.value) : null)}
                  required
                  className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                >
                  <option value="">选择资产</option>
                  {assets.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.symbol} — {a.name} ({ASSET_CLASS_LABELS[a.asset_class] || a.asset_class})
                    </option>
                  ))}
                </select>
                {assets.length === 0 && (
                  <p className="text-xs text-muted-foreground mt-1.5">
                    系统中暂无资产，请新建。
                  </p>
                )}
              </div>
            )}

            {!isEdit && mode === "new" && (
              <div className="space-y-4 p-4 rounded-lg border border-border bg-muted/30">
                <div>
                  <label className="block text-sm font-medium mb-2">
                    资产代码 <span className="text-destructive">*</span>
                  </label>
                  <input
                    type="text"
                    value={newSymbol}
                    onChange={(e) => setNewSymbol(e.target.value)}
                    placeholder="如：AAPL, BTC, 600519"
                    required
                    className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium mb-2">
                    资产名称 <span className="text-destructive">*</span>
                  </label>
                  <input
                    type="text"
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    placeholder="如：Apple Inc."
                    required
                    className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                  />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-sm font-medium mb-2">类型</label>
                    <select
                      value={newAssetClass}
                      onChange={(e) => setNewAssetClass(e.target.value)}
                      className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                    >
                      {ASSET_CLASS_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="block text-sm font-medium mb-2">币种</label>
                    <select
                      value={newCurrency}
                      onChange={(e) => setNewCurrency(e.target.value)}
                      className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                    >
                      {CURRENCY_OPTIONS.map((c) => (
                        <option key={c} value={c}>
                          {c}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
              </div>
            )}

            <div>
              <label className="block text-sm font-medium mb-2">
                关联账户 <span className="text-destructive">*</span>
              </label>
              <select
                value={accountId}
                onChange={(e) => setAccountId(Number(e.target.value))}
                required
                disabled={isEdit}
                className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-60 disabled:cursor-not-allowed"
              >
                <option value="">选择账户</option>
                {accounts.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name} ({a.currency})
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="block text-sm font-medium mb-2">
                持有数量 <span className="text-destructive">*</span>
              </label>
              <input
                type="number"
                step="any"
                min="0"
                value={quantity}
                onChange={(e) => setQuantity(e.target.value)}
                placeholder="0.00"
                required
                className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-sm font-medium mb-2">成本价</label>
                <input
                  type="number"
                  step="any"
                  min="0"
                  value={avgCost}
                  onChange={(e) => setAvgCost(e.target.value)}
                  placeholder="0.00"
                  className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>
              <div>
                <label className="block text-sm font-medium mb-2">成本币种</label>
                <select
                  value={costCurrency}
                  onChange={(e) => setCostCurrency(e.target.value)}
                  className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                >
                  {CURRENCY_OPTIONS.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {error && (
              <div className="p-3 rounded-lg bg-destructive/10 border border-destructive/20 text-sm text-destructive">
                {error}
              </div>
            )}

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
                {submitting ? "保存中…" : isEdit ? "保存修改" : "添加持仓"}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}
