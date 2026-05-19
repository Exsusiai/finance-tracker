"use client";

import { useEffect, useState } from "react";
import {
  type ChainAddressOut,
  addChainAddress,
  ApiError,
  deleteChainAddress,
  fetchChainAddresses,
} from "@/lib/api";
import { cn } from "@/lib/utils";

// Display chains that the backend providers support today. Long-tail
// chains (Cosmos / Cardano / Sui / …) land in P2 and will show up here
// once the backend wires them in.
const CHAIN_OPTIONS: Array<{ value: string; label: string; placeholder: string }> = [
  { value: "ethereum",      label: "Ethereum",       placeholder: "0x..." },
  { value: "arbitrum",      label: "Arbitrum",       placeholder: "0x..." },
  { value: "optimism",      label: "Optimism",       placeholder: "0x..." },
  { value: "base",          label: "Base",           placeholder: "0x..." },
  { value: "polygon",       label: "Polygon",        placeholder: "0x..." },
  { value: "polygon-zkevm", label: "Polygon zkEVM",  placeholder: "0x..." },
  { value: "zksync",        label: "zkSync Era",     placeholder: "0x..." },
  { value: "linea",         label: "Linea",          placeholder: "0x..." },
  { value: "scroll",        label: "Scroll",         placeholder: "0x..." },
  { value: "mantle",        label: "Mantle",         placeholder: "0x..." },
  { value: "blast",         label: "Blast",          placeholder: "0x..." },
  { value: "bitcoin",       label: "Bitcoin",        placeholder: "bc1q… / 1… / 3…" },
  { value: "solana",        label: "Solana",         placeholder: "Base58 32-byte pubkey" },
  { value: "tron",          label: "Tron",           placeholder: "T…" },
];

// Stable chain id → friendly label lookup (avoids re-iterating the array on
// every render of an existing-row line).
const CHAIN_LABELS = Object.fromEntries(
  CHAIN_OPTIONS.map((c) => [c.value, c.label]),
);

interface ChainAddressesEditorProps {
  accountId: number;
}

export function ChainAddressesEditor({ accountId }: ChainAddressesEditorProps) {
  const [rows, setRows] = useState<ChainAddressOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Add-form local state.
  const [newChain, setNewChain] = useState(CHAIN_OPTIONS[0].value);
  const [newAddress, setNewAddress] = useState("");
  const [newLabel, setNewLabel] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetchChainAddresses(accountId)
      .then((rs) => {
        if (alive) setRows(rs);
      })
      .catch((e) => {
        if (alive) setError(e instanceof ApiError ? e.message : "加载失败");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [accountId]);

  // NOTE: this is intentionally NOT a form `onSubmit` handler. The
  // editor is rendered INSIDE AccountForm's <form>, and nested <form>
  // elements are invalid HTML — the browser silently collapses them, so
  // an inner `type=submit` button ends up firing the OUTER form's
  // submit (account save) and the address never gets POSTed. Trigger
  // this only from explicit button onClick.
  async function handleAdd() {
    setError(null);
    if (!newAddress.trim()) {
      setError("请输入地址");
      return;
    }
    try {
      setSubmitting(true);
      const created = await addChainAddress(accountId, {
        chain: newChain,
        address: newAddress.trim(),
        label: newLabel.trim() || null,
      });
      setRows((prev) => [...prev, created]);
      setNewAddress("");
      setNewLabel("");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "添加失败");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete(id: number) {
    if (!confirm("删除这个地址？相关持仓不会立即清空，但下次同步会标为 0。")) {
      return;
    }
    try {
      await deleteChainAddress(accountId, id);
      setRows((prev) => prev.filter((r) => r.id !== id));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "删除失败");
    }
  }

  const placeholder =
    CHAIN_OPTIONS.find((c) => c.value === newChain)?.placeholder ?? "";

  return (
    <div className="space-y-3">
      <div>
        <label className="block text-sm font-medium mb-1.5">地址列表</label>
        <p className="text-xs text-muted-foreground mb-2">
          每个钱包可以聚合多条链上的地址。点 ↻ 同步会遍历所有地址 → 拉取余额 → 更新持仓。
        </p>
        {loading ? (
          <div className="text-sm text-muted-foreground">加载中…</div>
        ) : rows.length === 0 ? (
          <div className="text-sm text-muted-foreground italic">
            还没有添加任何地址。
          </div>
        ) : (
          <ul className="space-y-1.5">
            {rows.map((r) => (
              <li
                key={r.id}
                className="flex items-center gap-2 px-3 py-2 rounded-lg border border-border bg-muted/30"
              >
                <span className="text-xs font-medium shrink-0 px-2 py-0.5 rounded bg-background border border-border">
                  {CHAIN_LABELS[r.chain] ?? r.chain}
                </span>
                <span className="flex-1 text-xs font-mono truncate" title={r.address}>
                  {r.address}
                </span>
                {r.label && (
                  <span className="text-xs text-muted-foreground shrink-0">
                    {r.label}
                  </span>
                )}
                {r.last_sync_status === "error" && (
                  <span
                    className="text-xs text-destructive shrink-0"
                    title={r.last_sync_error ?? ""}
                  >
                    ⚠
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => handleDelete(r.id)}
                  className="text-xs text-muted-foreground hover:text-destructive shrink-0"
                  aria-label="删除地址"
                >
                  ✕
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="space-y-2 pt-2 border-t border-border">
        <div className="grid grid-cols-[120px_1fr] gap-2">
          <label htmlFor="chain-editor-chain" className="sr-only">链</label>
          <select
            id="chain-editor-chain"
            value={newChain}
            onChange={(e) => setNewChain(e.target.value)}
            className="px-2 py-2 text-sm rounded-lg border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
          >
            {CHAIN_OPTIONS.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
          <label htmlFor="chain-editor-address" className="sr-only">地址</label>
          <input
            id="chain-editor-address"
            type="text"
            value={newAddress}
            onChange={(e) => setNewAddress(e.target.value)}
            // Pressing Enter on the address field should add the row, not
            // submit the surrounding AccountForm (see handleAdd note).
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void handleAdd();
              }
            }}
            placeholder={placeholder}
            className="px-3 py-2 text-sm font-mono rounded-lg border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
            spellCheck={false}
          />
        </div>
        <div className="grid grid-cols-[1fr_auto] gap-2">
          <input
            type="text"
            value={newLabel}
            onChange={(e) => setNewLabel(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void handleAdd();
              }
            }}
            placeholder="备注（可选）"
            className="px-3 py-2 text-sm rounded-lg border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
          />
          <button
            type="button"
            onClick={() => void handleAdd()}
            disabled={submitting}
            className={cn(
              "px-4 py-2 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50",
            )}
          >
            {submitting ? "添加中…" : "添加地址"}
          </button>
        </div>
        {error && (
          <div className="text-xs text-destructive">{error}</div>
        )}
      </div>
    </div>
  );
}
