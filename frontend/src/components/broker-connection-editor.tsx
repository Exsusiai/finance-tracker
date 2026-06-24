"use client";

import { useEffect, useState } from "react";
import {
  type BrokerConnectionOut,
  type BrokerProvider,
  ApiError,
  deleteBrokerConnection,
  fetchBrokerConnection,
  upsertBrokerConnection,
  trConnect,
  trVerify,
} from "@/lib/api";

// IDs for label/htmlFor pairing — accessibility.
let _idCounter = 0;
function _useStableId(prefix: string): string {
  // eslint-disable-next-line react-hooks/rules-of-hooks
  const [id] = useState(() => `${prefix}-${++_idCounter}`);
  return id;
}

const PROVIDERS: Array<{ value: BrokerProvider; label: string }> = [
  { value: "ibkr", label: "Interactive Brokers (Flex)" },
  { value: "traderepublic", label: "Trade Republic" },
];

interface BrokerConnectionEditorProps {
  accountId: number;
  /** Pre-select the broker based on the account's institution. Overridden
   *  by an existing saved connection's provider once loaded. */
  initialProvider?: BrokerProvider;
}

export function BrokerConnectionEditor({
  accountId,
  initialProvider = "ibkr",
}: BrokerConnectionEditorProps) {
  const [existing, setExisting] = useState<BrokerConnectionOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [okMsg, setOkMsg] = useState<string | null>(null);

  const [provider, setProvider] = useState<BrokerProvider>(initialProvider);

  // IBKR fields
  const [queryId, setQueryId] = useState("");
  const [token, setToken] = useState("");

  // Trade Republic fields (two-step)
  const [phone, setPhone] = useState("");
  const [pin, setPin] = useState("");
  const [code, setCode] = useState("");
  const [trStep, setTrStep] = useState<"idle" | "code-sent">("idle");
  const [countdown, setCountdown] = useState(0);

  const [submitting, setSubmitting] = useState(false);

  const providerId = _useStableId("broker");
  const queryIdId = _useStableId("qid");
  const tokenId = _useStableId("token");
  const phoneId = _useStableId("phone");
  const pinId = _useStableId("pin");
  const codeId = _useStableId("code");

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setLoadFailed(false);
    fetchBrokerConnection(accountId)
      .then((r) => {
        if (!alive) return;
        setExisting(r);
        if (r) {
          setProvider(r.provider);
          if (r.query_id) setQueryId(r.query_id);
        }
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

  // Countdown ticker for the TR resend hint.
  useEffect(() => {
    if (trStep !== "code-sent" || countdown <= 0) return;
    const t = setInterval(() => setCountdown((c) => Math.max(0, c - 1)), 1000);
    return () => clearInterval(t);
  }, [trStep, countdown]);

  // ─── IBKR ─────────────────────────────────────────────────────────────
  async function handleIbkrSubmit() {
    setError(null);
    setOkMsg(null);
    if (!token.trim()) return setError("Flex Token 必填");
    if (!queryId.trim()) return setError("Query ID 必填");
    try {
      setSubmitting(true);
      const r = await upsertBrokerConnection(accountId, {
        provider: "ibkr",
        token: token.trim(),
        query_id: queryId.trim(),
      });
      setExisting(r);
      setToken("");
      setOkMsg("凭据已加密保存。点 ↻ 同步可拉取持仓（收盘后数据）。");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "保存失败");
    } finally {
      setSubmitting(false);
    }
  }

  // ─── Trade Republic step 1 ────────────────────────────────────────────
  async function handleTrConnect() {
    setError(null);
    setOkMsg(null);
    if (!phone.trim()) return setError("手机号必填（含国家码，如 +49…）");
    if (!pin.trim()) return setError("PIN 必填");
    try {
      setSubmitting(true);
      const r = await trConnect(accountId, { phone: phone.trim(), pin: pin.trim() });
      setTrStep("code-sent");
      setCountdown(r.countdown_seconds);
      setOkMsg(r.message);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "连接失败");
    } finally {
      setSubmitting(false);
    }
  }

  // ─── Trade Republic step 2 ────────────────────────────────────────────
  async function handleTrVerify() {
    setError(null);
    setOkMsg(null);
    if (!code.trim()) return setError("请输入收到的验证码");
    try {
      setSubmitting(true);
      const r = await trVerify(accountId, { code: code.trim() });
      setExisting(r);
      // Wipe secrets from memory.
      setPhone("");
      setPin("");
      setCode("");
      setTrStep("idle");
      setOkMsg("会话已加密保存。点 ↻ 同步可拉取持仓。");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "验证失败");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete() {
    if (!confirm("删除已保存的券商凭据？删除后必须重新连接才能同步。")) return;
    try {
      await deleteBrokerConnection(accountId);
      setExisting(null);
      setTrStep("idle");
      setOkMsg("凭据已删除");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "删除失败");
    }
  }

  if (loading) {
    return <div className="text-sm text-muted-foreground">加载中…</div>;
  }

  // Provider is locked once a connection exists (delete to switch).
  const providerLocked = !!existing;

  return (
    <div className="space-y-3">
      {existing && existing.credentials_stale && (
        <div className="px-3 py-2 rounded-lg border border-destructive/40 bg-destructive/10 text-xs text-destructive">
          ⚠ 已保存的凭据无法解密（加密 key 已变更），请重新{provider === "traderepublic" ? "连接" : "输入 Token"}并保存。
        </div>
      )}
      {existing && existing.has_token && (
        <div className="px-3 py-2 rounded-lg border border-border bg-muted/30 text-xs flex items-center justify-between">
          <span>
            已绑定 <span className="font-medium">{existing.provider}</span>
            {existing.query_id && <> · Query <span className="font-mono">{existing.query_id}</span></>} ·
            最近同步：{existing.last_synced_at ?? "—"}
            {existing.last_sync_status === "error" && (
              <span className="ml-2 text-destructive">同步出错：{existing.last_sync_error}</span>
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

      <div>
        <label htmlFor={providerId} className="block text-xs font-medium mb-1">券商</label>
        <select
          id={providerId}
          value={provider}
          disabled={providerLocked}
          onChange={(e) => {
            setProvider(e.target.value as BrokerProvider);
            setError(null);
            setOkMsg(null);
            setTrStep("idle");
          }}
          className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-70"
        >
          {PROVIDERS.map((b) => (
            <option key={b.value} value={b.value}>{b.label}</option>
          ))}
        </select>
      </div>

      {/* ── IBKR ───────────────────────────────────────────────── */}
      {provider === "ibkr" && (
        <div className="space-y-2">
          <div className="px-3 py-2 rounded-lg border border-amber-500/30 bg-amber-500/5 text-xs text-amber-800 dark:text-amber-200">
            ℹ 在 IBKR Client Portal：<strong>Settings → Reporting → Flex Queries</strong>，启用 Flex Web
            Service 生成 Token，并建 Activity Flex Query（<strong>只需勾 Open Positions</strong>，
            Format=XML，Date Format=yyyyMMdd）记下 Query ID。仅读取持仓，数据为<strong>收盘后快照</strong>。
          </div>
          <div>
            <label htmlFor={queryIdId} className="block text-xs font-medium mb-1">Flex Query ID</label>
            <input
              id={queryIdId}
              type="text"
              value={queryId}
              onChange={(e) => setQueryId(e.target.value)}
              placeholder="如：1234567"
              className="w-full px-3 py-2 text-sm font-mono rounded-lg border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>
          <div>
            <label htmlFor={tokenId} className="block text-xs font-medium mb-1">
              Flex Token {existing && <span className="text-muted-foreground">（提交即覆盖已有）</span>}
            </label>
            <input
              id={tokenId}
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              autoComplete="new-password"
              className="w-full px-3 py-2 text-sm font-mono rounded-lg border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>
          {error && <div className="text-xs text-destructive">{error}</div>}
          {okMsg && <div className="text-xs text-emerald-600 dark:text-emerald-400">{okMsg}</div>}
          <button
            type="button"
            onClick={() => void handleIbkrSubmit()}
            disabled={submitting || loadFailed}
            className="w-full px-4 py-2 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
          >
            {submitting ? "保存中…" : existing ? "更新凭据" : "保存凭据"}
          </button>
        </div>
      )}

      {/* ── Trade Republic (two-step) ──────────────────────────── */}
      {provider === "traderepublic" && (
        <div className="space-y-2">
          <div className="px-3 py-2 rounded-lg border border-amber-500/30 bg-amber-500/5 text-xs text-amber-800 dark:text-amber-200">
            ⚠ Trade Republic <strong>无官方 API</strong>，这里使用社区逆向接口，仅<strong>只读</strong>持仓。
            登录会向你 App / 短信发 4 位验证码。注意：TR 限单设备登录，使用 Web 登录不会登出手机，但会话过期后需重新连接。
          </div>

          {trStep === "idle" && (
            <>
              <div>
                <label htmlFor={phoneId} className="block text-xs font-medium mb-1">
                  手机号（国际格式，无空格，如 +4915123456789）
                </label>
                <input
                  id={phoneId}
                  type="tel"
                  value={phone}
                  onChange={(e) => setPhone(e.target.value)}
                  placeholder="+4915123456789"
                  autoComplete="off"
                  className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>
              <div>
                <label htmlFor={pinId} className="block text-xs font-medium mb-1">App PIN</label>
                <input
                  id={pinId}
                  type="password"
                  value={pin}
                  onChange={(e) => setPin(e.target.value)}
                  autoComplete="new-password"
                  inputMode="numeric"
                  className="w-full px-3 py-2 text-sm font-mono rounded-lg border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>
              {error && <div className="text-xs text-destructive">{error}</div>}
              {okMsg && <div className="text-xs text-emerald-600 dark:text-emerald-400">{okMsg}</div>}
              <button
                type="button"
                onClick={() => void handleTrConnect()}
                disabled={submitting || loadFailed}
                className="w-full px-4 py-2 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
              >
                {submitting ? "发送中…" : existing ? "重新连接（发送验证码）" : "发送验证码"}
              </button>
            </>
          )}

          {trStep === "code-sent" && (
            <>
              <div>
                <label htmlFor={codeId} className="block text-xs font-medium mb-1">
                  验证码{countdown > 0 && <span className="text-muted-foreground"> · 约 {countdown}s 内有效</span>}
                </label>
                <input
                  id={codeId}
                  type="text"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  placeholder="4 位数字"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  className="w-full px-3 py-2 text-sm font-mono rounded-lg border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>
              {error && <div className="text-xs text-destructive">{error}</div>}
              {okMsg && <div className="text-xs text-emerald-600 dark:text-emerald-400">{okMsg}</div>}
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => { setTrStep("idle"); setCode(""); setError(null); }}
                  className="flex-1 px-4 py-2 text-sm rounded-lg border border-border hover:bg-muted transition-colors"
                >
                  返回
                </button>
                <button
                  type="button"
                  onClick={() => void handleTrVerify()}
                  disabled={submitting}
                  className="flex-1 px-4 py-2 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
                >
                  {submitting ? "验证中…" : "验证并连接"}
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
