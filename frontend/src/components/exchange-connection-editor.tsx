"use client";

import { useEffect, useState } from "react";
import {
  type ExchangeConnectionOut,
  ApiError,
  deleteExchangeConnection,
  fetchExchangeConnection,
  upsertExchangeConnection,
} from "@/lib/api";

// IDs for label/htmlFor pairing — accessibility (a11y M-3).
let _idCounter = 0;
function _useStableId(prefix: string): string {
  // useState lazy initializer keeps the id stable across renders
  // without pulling in useId (Next 15 supports useId too; this avoids
  // SSR hydration mismatch risk on a leaf form).
  // eslint-disable-next-line react-hooks/rules-of-hooks
  const [id] = useState(() => `${prefix}-${++_idCounter}`);
  return id;
}

const EXCHANGES: Array<{ value: "binance" | "bitget"; label: string; passphrase: boolean }> = [
  { value: "binance", label: "Binance",      passphrase: false },
  { value: "bitget",  label: "Bitget",       passphrase: true },
];

interface ExchangeConnectionEditorProps {
  accountId: number;
}

export function ExchangeConnectionEditor({ accountId }: ExchangeConnectionEditorProps) {
  const [existing, setExisting] = useState<ExchangeConnectionOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [okMsg, setOkMsg] = useState<string | null>(null);

  // Form state.
  const [exchange, setExchange] = useState<"binance" | "bitget">("binance");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const exchangeId = _useStableId("exch");
  const keyId = _useStableId("key");
  const secretId = _useStableId("secret");
  const passphraseId = _useStableId("pass");

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setLoadFailed(false);
    fetchExchangeConnection(accountId)
      .then((r) => {
        if (!alive) return;
        setExisting(r);
        // Backend's CHECK constraint guarantees `r.exchange` is one of
        // the literal union members (`'binance' | 'bitget'`), so the
        // assignment is safe without runtime narrowing. If a future
        // exchange is added this assignment becomes a type error which
        // is the right signal to update the UI options.
        if (r) setExchange(r.exchange);
      })
      .catch((e) => {
        if (!alive) return;
        setError(e instanceof ApiError ? e.message : "加载失败");
        setLoadFailed(true);
      })
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [accountId]);

  const needsPassphrase = EXCHANGES.find((e) => e.value === exchange)?.passphrase ?? false;

  // NOT a form `onSubmit` — this editor is rendered inside AccountForm's
  // own <form>, and nested forms are invalid HTML (the browser flattens
  // them so an inner type=submit fires the OUTER form). Trigger only
  // via explicit button onClick. See chain-addresses-editor.tsx for the
  // same rationale.
  async function handleSubmit() {
    setError(null);
    setOkMsg(null);
    if (!apiKey.trim() || !apiSecret.trim()) {
      setError("API Key 和 Secret 都必填");
      return;
    }
    if (needsPassphrase && !passphrase.trim()) {
      setError("Bitget 必须填 Passphrase（创建 API key 时设置的）");
      return;
    }
    try {
      setSubmitting(true);
      const r = await upsertExchangeConnection(accountId, {
        exchange,
        api_key: apiKey.trim(),
        api_secret: apiSecret.trim(),
        passphrase: needsPassphrase ? passphrase.trim() : null,
      });
      setExisting(r);
      // Wipe local secret state immediately — never linger in memory.
      setApiKey("");
      setApiSecret("");
      setPassphrase("");
      setOkMsg("凭据已加密保存。点 ↻ 同步可拉取余额。");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "保存失败");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete() {
    if (!confirm("删除已保存的 API 凭据？删除后必须重新填写才能同步。")) return;
    try {
      await deleteExchangeConnection(accountId);
      setExisting(null);
      setOkMsg("凭据已删除");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "删除失败");
    }
  }

  if (loading) {
    return <div className="text-sm text-muted-foreground">加载中…</div>;
  }

  return (
    <div className="space-y-3">
      {existing && existing.credentials_stale && (
        <div className="px-3 py-2 rounded-lg border border-destructive/40 bg-destructive/10 text-xs text-destructive">
          ⚠ 已保存的 API 凭据无法解密（加密 key 已变更），请重新输入下方凭据并保存。
        </div>
      )}
      {existing && existing.has_credentials && (
        <div className="px-3 py-2 rounded-lg border border-border bg-muted/30 text-xs flex items-center justify-between">
          <span>
            已绑定 <span className="font-medium">{existing.exchange}</span>
            {existing.has_passphrase && "（含 passphrase）"} ·
            最近同步：{existing.last_synced_at ?? "—"}
            {existing.last_sync_status === "error" && (
              <span className="ml-2 text-destructive">
                同步出错：{existing.last_sync_error}
              </span>
            )}
          </span>
          <button
            type="button"
            onClick={handleDelete}
            className="text-xs text-muted-foreground hover:text-destructive ml-2"
          >
            删除凭据
          </button>
        </div>
      )}

      <div className="px-3 py-2 rounded-lg border border-amber-500/30 bg-amber-500/5 text-xs text-amber-800 dark:text-amber-200">
        ⚠ 安全提示：请在交易所后台创建 API key 时<strong>只勾选「Read / 查看」权限</strong>，
        禁用「交易」「提币」「划转」。本系统只读取余额，不发起任何资金操作。
      </div>

      <div className="space-y-2">
        <div>
          <label htmlFor={exchangeId} className="block text-xs font-medium mb-1">交易所</label>
          <select
            id={exchangeId}
            value={exchange}
            onChange={(e) => setExchange(e.target.value as "binance" | "bitget")}
            className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
          >
            {EXCHANGES.map((e) => (
              <option key={e.value} value={e.value}>
                {e.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor={keyId} className="block text-xs font-medium mb-1">
            API Key {existing && <span className="text-muted-foreground">（提交即覆盖已有）</span>}
          </label>
          <input
            id={keyId}
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            autoComplete="new-password"
            className="w-full px-3 py-2 text-sm font-mono rounded-lg border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <div>
          <label htmlFor={secretId} className="block text-xs font-medium mb-1">API Secret</label>
          <input
            id={secretId}
            type="password"
            value={apiSecret}
            onChange={(e) => setApiSecret(e.target.value)}
            autoComplete="new-password"
            className="w-full px-3 py-2 text-sm font-mono rounded-lg border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        {needsPassphrase && (
          <div>
            <label htmlFor={passphraseId} className="block text-xs font-medium mb-1">Passphrase（Bitget 创建 key 时设置的）</label>
            <input
              id={passphraseId}
              type="password"
              value={passphrase}
              onChange={(e) => setPassphrase(e.target.value)}
              autoComplete="new-password"
              className="w-full px-3 py-2 text-sm font-mono rounded-lg border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>
        )}

        {error && <div className="text-xs text-destructive">{error}</div>}
        {okMsg && <div className="text-xs text-emerald-600 dark:text-emerald-400">{okMsg}</div>}

        <button
          type="button"
          onClick={() => void handleSubmit()}
          // FE-H4: don't allow save when the initial GET failed —
          // we don't know whether a connection already exists so a
          // PUT could overwrite blindly. User can retry by refreshing
          // the panel.
          disabled={submitting || loadFailed}
          title={loadFailed ? "初始加载失败，请刷新页面后重试" : undefined}
          className="w-full px-4 py-2 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {submitting ? "保存中…" : existing ? "更新凭据" : "保存凭据"}
        </button>
      </div>
    </div>
  );
}
