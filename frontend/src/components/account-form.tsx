"use client";

import { useEffect, useRef, useState } from "react";
import {
  type AccountOut,
  ApiError,
  createAccount,
  updateAccount,
} from "@/lib/api";
import { cn, CURRENCY_GROUPS } from "@/lib/utils";
import { ChainAddressesEditor } from "@/components/chain-addresses-editor";
import { ExchangeConnectionEditor } from "@/components/exchange-connection-editor";
import { BrokerConnectionEditor } from "@/components/broker-connection-editor";

export const ACCOUNT_TYPE_OPTIONS: Array<{
  value: string;
  label: string;
  icon: string;
}> = [
  { value: "bank", label: "银行账户", icon: "🏦" },
  { value: "credit_card", label: "信用卡", icon: "💳" },
  { value: "brokerage", label: "证券账户", icon: "📈" },
  { value: "crypto_wallet", label: "加密钱包", icon: "₿" },
  { value: "exchange", label: "交易所 (CEX)", icon: "🪙" },
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

// IBAN is meaningful only for fiat banking accounts where PDF/transfer
// matching needs it. Hidden for crypto / cash / brokerage / etc.
const IBAN_TYPES = new Set(["bank", "credit_card"]);

// Initial balance is user-typed only for fiat / cash accounts. Snapshot
// accounts (brokerage / crypto_wallet / exchange) derive their worth from the
// sync pipeline (holdings × price), so a typed cash balance would be
// double-counted against net worth — hide it (review V7 §P1-2).
const HIDE_INITIAL_BALANCE_TYPES = new Set(["brokerage", "crypto_wallet", "exchange"]);

// Types whose holdings live in USDT (per project decision 2026-05-18).
const CRYPTO_TYPES = new Set(["crypto_wallet", "exchange"]);

// Types that have a post-create connection editor (addresses / API creds /
// Flex token). For these we keep the modal open after the initial create so
// the user can finish setup inline instead of reopening the edit panel.
const CONNECTION_SETUP_TYPES = new Set(["crypto_wallet", "exchange", "brokerage"]);

// Types that legitimately hold ASSETS (positions you have a quantity of:
// stocks, crypto, gold, …). Bank / credit_card / cash / other only hold
// transactions, so "add holding" / "holdings management" UI is hidden
// for them.
export const INVESTMENT_TYPES: ReadonlySet<string> = new Set([
  "brokerage",
  "crypto_wallet",
  "exchange",
]);

// Supported CEX list — must mirror the backend's
// `ck_exchange_conn_exchange` CHECK + ExchangeProvider dispatcher. When
// adding a new exchange there, extend this list and the
// ExchangeConnectionEditor's `EXCHANGES` constant in lockstep.
const EXCHANGE_INSTITUTIONS: Array<{ value: string; label: string }> = [
  { value: "Binance", label: "Binance" },
  { value: "Bitget", label: "Bitget" },
];

// Supported brokers — mirror the backend's `broker_connections.provider`
// CHECK + BrokerProvider dispatcher. The stored `institution` is a display
// label; the actual provider is chosen again in BrokerConnectionEditor
// (same two-place pattern as exchange). Keep these in lockstep when adding
// a broker.
const BROKER_INSTITUTIONS: Array<{ value: string; label: string }> = [
  { value: "Interactive Brokers", label: "Interactive Brokers (Flex)" },
  { value: "Trade Republic", label: "Trade Republic" },
];


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
  // After a fresh create, we KEEP the modal open and switch into a
  // "now add your addresses / API credentials" mode so the user doesn't
  // have to reopen the edit panel just to finish setup. `created` holds
  // the freshly-minted Account once the create POST succeeds.
  const [created, setCreated] = useState<AccountOut | null>(null);
  const [name, setName] = useState(initial?.name ?? "");
  const [type, setTypeRaw] = useState(initial?.type ?? "bank");
  const [institution, setInstitution] = useState(initial?.institution ?? "");
  const [iban, setIban] = useState(initial?.iban ?? "");
  const [currency, setCurrency] = useState(initial?.currency ?? "EUR");
  // Whether the user has explicitly picked a currency this session — used
  // so an auto-snap to USDT on crypto-type selection only fires when the
  // currency is still at its default.
  const [currencyTouched, setCurrencyTouched] = useState(false);

  // Wrap setType so picking crypto_wallet / exchange auto-snaps the
  // currency to USDT (unless the user already chose one) and, for
  // `exchange`, defaults `institution` to the first supported CEX so the
  // dropdown lands on a valid value the moment it renders.
  function setType(next: string) {
    setTypeRaw(next);
    if (!isEdit && !currencyTouched && CRYPTO_TYPES.has(next)) {
      setCurrency("USDT");
    }
    if (!isEdit && next === "exchange") {
      const valid = EXCHANGE_INSTITUTIONS.some((o) => o.value === institution);
      if (!valid) setInstitution(EXCHANGE_INSTITUTIONS[0].value);
    }
    if (!isEdit && next === "brokerage") {
      const valid = BROKER_INSTITUTIONS.some((o) => o.value === institution);
      if (!valid) setInstitution(BROKER_INSTITUTIONS[0].value);
    }
  }
  const [initialBalance, setInitialBalance] = useState(
    initial?.initial_balance ?? "0",
  );
  const [notes, setNotes] = useState(initial?.notes ?? "");
  // Whether this account is included in the grand-total (net worth /
  // balances summary). Defaults to true; user opts out for shared /
  // business / experimental accounts they don't want in their personal
  // total.
  const [includeInTotal, setIncludeInTotal] = useState(
    initial?.include_in_total ?? true,
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Auto-scroll to the just-revealed editor section once `created` flips
  // true, so the user can SEE the form moved into "now add addresses"
  // mode instead of hunting for it below the fold.
  const editorAnchorRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (created && editorAnchorRef.current) {
      editorAnchorRef.current.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }
  }, [created]);

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
      // IBAN only travels for types where the field was visible; other
      // types must POST `null` so a typo before switching type doesn't
      // sneak through.
      const ibanClean = IBAN_TYPES.has(type)
        ? iban.trim().replace(/\s+/g, "").toUpperCase() || null
        : null;
      const balance = HIDE_INITIAL_BALANCE_TYPES.has(type)
        ? "0"
        : initialBalance || "0";
      if (isEdit && initial) {
        result = await updateAccount(initial.id, {
          name: name.trim(),
          type,
          institution: institution.trim() || undefined,
          iban: ibanClean,
          include_in_total: includeInTotal,
          notes: notes.trim() || undefined,
        });
      } else if (created) {
        // We're in the post-create "finish setup" phase. Allow rename /
        // notes / include-in-total edits but keep id stable.
        result = await updateAccount(created.id, {
          name: name.trim(),
          type,
          institution: institution.trim() || undefined,
          iban: ibanClean,
          include_in_total: includeInTotal,
          notes: notes.trim() || undefined,
        });
      } else {
        // Single POST with include_in_total — backend now accepts it
        // on create, removing the previous two-step pattern where a
        // failed PATCH would silently drop the flag (FE-M5 / 2026-05-19).
        result = await createAccount({
          name: name.trim(),
          type,
          institution: institution.trim() || undefined,
          iban: ibanClean ?? undefined,
          currency,
          initial_balance: balance,
          include_in_total: includeInTotal,
          notes: notes.trim() || undefined,
        });
      }
      // For crypto_wallet / exchange we keep the modal open after the
      // initial create so the user can immediately add addresses / API
      // creds via the inline editors. Fiat types still close right away.
      if (
        !isEdit
        && !created
        && CONNECTION_SETUP_TYPES.has(type)
      ) {
        setCreated(result);
        // Don't bubble up yet — the parent will get onSuccess when the
        // user clicks 完成 at the end. Doing it now would re-open the
        // form in edit mode and lose local state.
        return;
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
        onClick={() => {
          if (submitting) return;
          // Once `created` is set, the user has data mid-flight (an
          // account was POSTed and we're showing the post-create editor).
          // Backdrop-close at this point silently discards that work, so
          // we require an explicit 完成 / 稍后再填 click.
          if (created) {
            onSuccess(created);
          } else {
            onClose();
          }
        }}
      />
      <div className="relative w-full max-w-md rounded-xl border border-border bg-card p-6 shadow-xl max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold">
            {isEdit ? "编辑账户" : created ? "添加地址 / API 凭据" : "添加账户"}
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
                  disabled={!!created || isEdit}
                  className={cn(
                    "flex flex-col items-center gap-1 px-2 py-3 text-xs font-medium rounded-lg border-2 transition-all",
                    type === opt.value
                      ? "border-primary bg-primary/5 text-foreground"
                      : "border-border hover:border-muted-foreground/30 text-muted-foreground",
                    (!!created || isEdit) && "opacity-60 cursor-not-allowed hover:border-border",
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

          {(() => {
            // exchange / brokerage pick from a fixed list of supported
            // providers (dropdown); everything else is free text.
            const institutionOptions =
              type === "exchange"
                ? EXCHANGE_INSTITUTIONS
                : type === "brokerage"
                  ? BROKER_INSTITUTIONS
                  : null;
            return (
              <div>
                <label className="block text-sm font-medium mb-2">
                  机构
                  {institutionOptions && (
                    <span className="ml-1.5 text-[10px] font-normal text-muted-foreground">
                      （仅支持已对接的{type === "exchange" ? "交易所" : "券商"}）
                    </span>
                  )}
                </label>
                {institutionOptions ? (
                  <select
                    value={
                      institutionOptions.some((o) => o.value === institution)
                        ? institution
                        : institutionOptions[0].value
                    }
                    onChange={(e) => setInstitution(e.target.value)}
                    disabled={isEdit || !!created}
                    className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-60 disabled:cursor-not-allowed"
                  >
                    {institutionOptions.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    type="text"
                    value={institution}
                    onChange={(e) => setInstitution(e.target.value)}
                    placeholder={
                      type === "crypto_wallet"
                        ? "如：MetaMask、Ledger 主钱包"
                        : "如：N26 Bank、American Express"
                    }
                    className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                  />
                )}
              </div>
            );
          })()}

          {IBAN_TYPES.has(type) && (
            <div>
              <label className="block text-sm font-medium mb-2">
                IBAN
                <span className="ml-1.5 text-[10px] font-normal text-muted-foreground">
                  （用于自动识别内部转账，PDF 中含此 IBAN 的转账会被认作转给本账户）
                </span>
              </label>
              <input
                type="text"
                value={iban}
                onChange={(e) => setIban(e.target.value)}
                placeholder="例：DE00 0000 0000 0000 0000 00（空格大小写无所谓）"
                className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring tabular-nums uppercase"
                spellCheck={false}
              />
            </div>
          )}

          <div
            className={cn(
              "grid gap-3",
              HIDE_INITIAL_BALANCE_TYPES.has(type)
                ? "grid-cols-1"
                : "grid-cols-2",
            )}
          >
            <div>
              <label className="block text-sm font-medium mb-2">
                币种 <span className="text-destructive">*</span>
              </label>
              <select
                // Crypto / exchange accounts MUST use USDT — backend
                // schema validator rejects anything else (V6-P1-3). Lock
                // the picker rather than letting the user pick EUR and
                // get a 422.
                value={CRYPTO_TYPES.has(type) ? "USDT" : currency}
                onChange={(e) => {
                  setCurrency(e.target.value);
                  setCurrencyTouched(true);
                }}
                disabled={isEdit || !!created || CRYPTO_TYPES.has(type)}
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
              {CRYPTO_TYPES.has(type) && (
                <p className="text-[10px] text-muted-foreground mt-1">
                  加密类账户的持仓估值统一记账到 USDT
                </p>
              )}
            </div>
            {!HIDE_INITIAL_BALANCE_TYPES.has(type) && (
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
                  disabled={isEdit || !!created}
                  className="w-full px-3 py-2.5 text-sm rounded-lg border border-border bg-background text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-60 disabled:cursor-not-allowed"
                />
              </div>
            )}
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

          <label className="flex items-start gap-2 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={includeInTotal}
              onChange={(e) => setIncludeInTotal(e.target.checked)}
              className="mt-0.5 h-4 w-4 rounded border-border cursor-pointer"
            />
            <span className="text-sm">
              纳入总资产统计
              <span className="ml-1.5 text-[10px] text-muted-foreground">
                （关闭后该账户的余额 / 持仓不会进入总资产汇总，但其它视图照常显示）
              </span>
            </span>
          </label>

          {isEdit && (
            <p className="text-xs text-muted-foreground">
              币种与初始余额创建后不可修改。如需调整当前余额，请在投资组合页使用"调整余额"。
            </p>
          )}

          {created && (
            <div
              ref={editorAnchorRef}
              className="px-3 py-2 rounded-lg border border-emerald-500/40 bg-emerald-500/5 text-xs text-emerald-700 dark:text-emerald-300"
            >
              ✓ 账户已创建。
              {CRYPTO_TYPES.has(type)
                ? "现在添加链上地址或 API 凭据后点「完成」。"
                : type === "brokerage"
                  ? `现在填入${institution === "Trade Republic" ? " Trade Republic" : " IBKR Flex"}凭据后点「完成」。`
                  : ""}
            </div>
          )}

          {(isEdit ? initial?.type === "crypto_wallet"
             : created?.type === "crypto_wallet") && (
            <div className="pt-3 border-t border-border">
              <h3 className="text-sm font-semibold mb-2">链上地址</h3>
              <ChainAddressesEditor accountId={(initial ?? created)!.id} />
            </div>
          )}

          {(isEdit ? initial?.type === "exchange"
             : created?.type === "exchange") && (
            <div className="pt-3 border-t border-border">
              <h3 className="text-sm font-semibold mb-2">交易所 API 凭据</h3>
              <ExchangeConnectionEditor accountId={(initial ?? created)!.id} />
            </div>
          )}

          {(isEdit ? initial?.type === "brokerage"
             : created?.type === "brokerage") && (() => {
            const brokerInstitution = (initial ?? created)?.institution ?? institution;
            const isTR = brokerInstitution === "Trade Republic";
            return (
              <div className="pt-3 border-t border-border">
                <h3 className="text-sm font-semibold mb-2">
                  {isTR ? "Trade Republic 凭据" : "IBKR Flex 凭据"}
                </h3>
                <BrokerConnectionEditor
                  accountId={(initial ?? created)!.id}
                  initialProvider={isTR ? "traderepublic" : "ibkr"}
                />
              </div>
            );
          })()}

          {error && (
            <div className="p-3 rounded-lg bg-destructive/10 border border-destructive/20 text-sm text-destructive">
              {error}
            </div>
          )}

          <div className="flex gap-3 pt-2">
            <button
              type="button"
              onClick={() => {
                // After the create-then-configure flow, "取消" still needs
                // to surface the new account so the parent's account list
                // refreshes.
                if (created) {
                  onSuccess(created);
                } else {
                  onClose();
                }
              }}
              disabled={submitting}
              className="flex-1 px-4 py-2.5 text-sm font-medium rounded-lg border border-border hover:bg-muted transition-colors disabled:opacity-50"
            >
              {created ? "稍后再填" : "取消"}
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="flex-1 px-4 py-2.5 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
            >
              {submitting
                ? "保存中…"
                : isEdit
                  ? "保存修改"
                  : created
                    ? "完成"
                    : "创建账户"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
