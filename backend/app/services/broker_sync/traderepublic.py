"""Trade Republic provider (read-only, unofficial).

Trade Republic has **no official API**. This wraps the community ``pytr``
library, which speaks TR's private web API. We use it strictly read-only
(portfolio + cash) — never to place orders.

Why this is structurally different from IBKR (see CLAUDE.md):
- Auth is interactive: phone + PIN triggers a 4-digit code (app/SMS); a
  second call with that code yields a cookie **session**. There is no
  static token. So the connect flow is two-step and stateful — handled in
  the API layer via an in-memory pending-login store; this module exposes
  ``initiate_login`` / ``complete_login`` for it.
- The session is a cookie jar, stored encrypted in
  ``broker_connections.token_enc`` and restored per sync.
- Reads go over a WebSocket (``portfolio`` topic gives per-position
  ``netValue`` = current market value).
- Login is protected by AWS WAF; we clear it with pytr's pure-Python
  ``awswaf`` path (``curl_cffi``) so **no browser binary is needed** — this
  matters for headless Ubuntu deployment (no ``playwright install``).

Identity: TR positions are keyed by **ISIN**, stored on
``Asset.data_source_id`` (``data_source='traderepublic'``). Asset class is
derived from the ISIN country prefix (best-effort heuristic).
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

import structlog

from app.services.broker_sync import BrokerPosition, BrokerSyncError

log = structlog.get_logger(__name__)

# How pytr clears TR's AWS WAF challenge at *login* time:
#   - "playwright": real headless Chromium (default). As of 2026-06 this is
#     the ONLY method TR's WAF accepts — the pure-Python "awswaf" solver
#     produces a token the WAF rejects with HTTP 405. Requires the browser
#     binary: `python -m playwright install chromium` (Ubuntu headless:
#     `python -m playwright install --with-deps chromium`).
#   - "awswaf": pure-Python (curl_cffi), no browser. Kept as an override in
#     case a future pytr release fixes it — set FINANCE_TR_WAF_METHOD=awswaf.
# Only the LOGIN step needs this; routine balance syncs resume from the
# stored session cookies and never launch a browser.
_WAF_TOKEN = os.environ.get("FINANCE_TR_WAF_METHOD", "playwright").strip() or "playwright"
_SOURCE = "traderepublic"
# TR is a EUR brokerage; its portfolio feed quotes values in EUR.
_TR_CURRENCY = "EUR"
# Placeholder identity used when *resuming* a session — resume only needs the
# stored cookies; phone/pin are never sent, but the pytr constructor requires
# them to be truthy.
_RESUME_PLACEHOLDER = "session"


# ─── instrument type → asset class ───────────────────────────────────────────

# TR's compactPortfolioByType carries an `instrumentType` per position
# ("stock" / "fund" / "bond" / "crypto" / "derivative" / …) — far more
# reliable than guessing from the ISIN. Stocks are split US vs EU by the
# ISIN country prefix to land on the project's regional equity classes.
def map_instrument_type_to_asset_class(instrument_type: str, isin: str) -> str:
    t = (instrument_type or "").strip().lower()
    if t == "fund":
        return "fund"
    if t == "bond":
        return "bond"
    if t == "crypto":
        return "crypto"
    if t == "stock":
        return "us_stock" if (isin or "").strip().upper().startswith("US") else "eu_stock"
    return "other"  # derivative / unknown


# ─── cookie (de)serialization ────────────────────────────────────────────────


def _new_api(cookies_path: Path, *, phone: str, pin: str):
    """Construct a pytr ``TradeRepublicApi`` bound to ``cookies_path``."""
    from pytr.api import TradeRepublicApi

    return TradeRepublicApi(
        phone_no=phone,
        pin=pin,
        save_cookies=True,
        cookies_file=str(cookies_path),
        waf_token=_WAF_TOKEN,
    )


def _dump_cookies(tr) -> str:
    """Persist the live session cookies and return them as text."""
    tr.save_websession()
    path = Path(tr._cookies_file)
    return path.read_text()


# ─── two-step interactive login (driven by the API layer) ────────────────────


def normalize_phone(phone: str) -> str:
    """Coerce a phone number to E.164-ish (``+`` + digits).

    TR rejects anything with spaces / dashes / parens as ``NUMBER_INVALID``,
    so strip them. A leading ``00`` country prefix becomes ``+``. We do NOT
    touch a leading 0 after the country code — that's the user's call.
    """
    p = re.sub(r"[\s\-()]", "", phone.strip())
    if p.startswith("00"):
        p = "+" + p[2:]
    return p


def initiate_login(phone: str, pin: str):
    """Step 1 (blocking): clear WAF, POST phone+pin.

    Returns ``(tr, countdown_seconds)``. The caller MUST keep the returned
    ``tr`` object alive until ``complete_login`` — it holds the WAF cookies
    and the login ``processId``. TR sends a 4-digit code to the app/SMS.
    """
    phone = normalize_phone(phone)
    pin = pin.strip()
    # A temp cookie file the session will be written to on completion.
    fd = tempfile.NamedTemporaryFile(
        prefix="tr_cookies_", suffix=".txt", delete=False
    )
    fd.close()
    # V7-P2-3: if _new_api / initiate_weblogin raises, the request fails before
    # the pending entry is stored, so the verify/cleanup paths never run — the
    # temp file (possibly holding partial WAF/session cookies) would be orphaned
    # on disk. Unlink it on any failure so nothing private lingers.
    try:
        tr = _new_api(Path(fd.name), phone=phone, pin=pin)
        countdown = tr.initiate_weblogin()
    except Exception as exc:  # noqa: BLE001
        Path(fd.name).unlink(missing_ok=True)
        raise BrokerSyncError(_login_error_message(exc)) from exc
    return tr, int(countdown)


def _login_error_message(exc: Exception) -> str:
    """Turn a pytr login failure into a user-facing Chinese message.

    Maps TR's known error codes (from the HTTP response body) to clear hints,
    and flags the WAF-block signature (HTTP 405) which usually means the
    browser WAF solver needs attention.
    """
    resp = getattr(exc, "response", None)
    body = ""
    code = ""
    status = None
    if resp is not None:
        status = getattr(resp, "status_code", None)
        try:
            body = resp.text or ""
            import json as _json

            data = _json.loads(body)
            errs = data.get("errors") or []
            if errs:
                code = (errs[0] or {}).get("errorCode", "") or ""
        except Exception:  # noqa: BLE001
            pass
    hints = {
        "NUMBER_INVALID": "手机号格式无效，请用国际格式（如 +49…）。",
        "PHONE_NUMBER_INVALID": "手机号格式无效，请用国际格式（如 +49…）。",
        "VALIDATION_CODE_INVALID": "PIN 错误。",
        "PIN_INVALID": "App PIN 错误。",
        "TOO_MANY_REQUESTS": "请求过于频繁，请稍后再试。",
    }
    if code in hints:
        return f"Trade Republic 登录失败：{hints[code]}"
    if status == 405:
        return (
            "Trade Republic 登录被 WAF 拦截（HTTP 405）。请确认已安装浏览器："
            "`python -m playwright install chromium`；或设置 FINANCE_TR_WAF_METHOD。"
        )
    return f"Trade Republic 登录发起失败：{_short(exc)}"


def complete_login(tr, code: str) -> str:
    """Step 2 (blocking): submit the 4-digit code, return cookies blob."""
    try:
        tr.complete_weblogin(code.strip())
    except Exception as exc:  # noqa: BLE001
        raise BrokerSyncError(f"验证码校验失败：{_short(exc)}") from exc
    return _dump_cookies(tr)


def cleanup_login(tr) -> None:
    """Best-effort removal of the temp cookie file behind an abandoned login."""
    try:
        Path(tr._cookies_file).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


# ─── provider (read-only sync) ───────────────────────────────────────────────


# Trade Republic's default trading venue (Lang & Schwarz). Live prices are
# fetched per ISIN as `<isin>.LSX`.
_TR_EXCHANGE = "LSX"


class TradeRepublicProvider:
    """Fetch open positions from a stored Trade Republic session.

    Two-stage over the websocket:
      1. ``compactPortfolioByType`` → holdings (isin, netSize, averageBuyIn,
         instrumentType, name), nested under ``categories[].positions[]``. It
         carries NO live price, so:
      2. one ``ticker`` subscription per ISIN → current price (EUR, LSX).
    """

    provider_id = "traderepublic"

    def __init__(self, *, cookies_blob: str, timeout: float = 15.0) -> None:
        self._cookies_blob = cookies_blob
        self._timeout = timeout

    async def fetch_positions(self) -> list[BrokerPosition]:
        fd = tempfile.NamedTemporaryFile(
            prefix="tr_resume_", suffix=".txt", delete=False
        )
        fd.write(self._cookies_blob.encode("utf-8"))
        fd.close()
        cookies_path = Path(fd.name)
        tr = _new_api(cookies_path, phone=_RESUME_PLACEHOLDER, pin=_RESUME_PLACEHOLDER)
        try:
            ok = await asyncio.to_thread(tr.resume_websession)
            if not ok:
                raise BrokerSyncError(
                    "Trade Republic 会话已失效，请在账户里重新连接（输入手机号 + PIN + 验证码）。"
                )
            try:
                portfolio = await self._sub(tr, {"type": "compactPortfolioByType"})
                raws = _flatten_positions(portfolio)
                out: list[BrokerPosition] = []
                for raw in raws:
                    isin = (raw.get("isin") or "").strip()
                    if not isin:
                        continue
                    price = await self._fetch_price(tr, isin)
                    out.append(_build_position(raw, price))
                # Uninvested cash → a cash-class "position" priced 1:1, so it
                # flows through balance / net-worth like any other holding.
                out.extend(await self._fetch_cash(tr))
                return out
            finally:
                await tr.close()
        except BrokerSyncError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BrokerSyncError(f"Trade Republic 拉取持仓失败：{_short(exc)}") from exc
        finally:
            cookies_path.unlink(missing_ok=True)

    async def _sub(self, tr, payload: dict) -> dict:
        sub_id = await tr.subscribe(payload)
        try:
            return await asyncio.wait_for(
                tr._recv_subscription(sub_id), self._timeout
            )
        finally:
            try:
                await tr.unsubscribe(sub_id)
            except Exception:  # noqa: BLE001
                pass

    async def _fetch_price(self, tr, isin: str) -> Decimal | None:
        """Best-effort live price for one ISIN — failures leave price unknown
        (the holding still shows; only its market value is missing)."""
        try:
            tk = await self._sub(tr, {"type": "ticker", "id": f"{isin}.{_TR_EXCHANGE}"})
            return _dec((tk.get("last") or {}).get("price"))
        except Exception as exc:  # noqa: BLE001
            log.info("tr_ticker_failed", isin=isin, error=_short(exc, 80))
            return None

    async def _fetch_cash(self, tr) -> list[BrokerPosition]:
        """Uninvested cash balances → cash-class positions (price 1:1).

        Best-effort: a cash-feed failure must not sink the whole sync.
        """
        try:
            rows = await self._sub(tr, {"type": "cash"})
        except Exception as exc:  # noqa: BLE001
            log.info("tr_cash_failed", error=_short(exc, 80))
            return []
        out: list[BrokerPosition] = []
        for row in rows or []:
            amount = _dec(row.get("amount"))
            ccy = (row.get("currencyId") or _TR_CURRENCY).strip().upper()
            if amount is None or amount == 0:
                continue
            out.append(
                BrokerPosition(
                    symbol=ccy,
                    conid=None,
                    asset_category="cash",
                    currency=ccy,
                    quantity=amount,
                    mark_price=Decimal("1"),
                    avg_cost=None,
                    description=f"{ccy} 现金",
                    asset_class="cash",
                )
            )
        return out


# ─── response mapping ────────────────────────────────────────────────────────


def _flatten_positions(data: dict) -> list[dict]:
    """Pull raw position dicts out of compactPortfolioByType's category tree."""
    out: list[dict] = []
    for cat in (data or {}).get("categories", []) or []:
        for p in (cat or {}).get("positions", []) or []:
            out.append(p)
    return out


def _build_position(raw: dict, price: Decimal | None) -> BrokerPosition:
    isin = (raw.get("isin") or "").strip()
    itype = raw.get("instrumentType") or ""
    return BrokerPosition(
        symbol=isin,
        conid=isin,
        asset_category=itype,
        currency=_TR_CURRENCY,
        quantity=_dec(raw.get("netSize")) or Decimal("0"),
        mark_price=price,
        avg_cost=_dec(raw.get("averageBuyIn")),
        description=raw.get("name") or None,
        asset_class=map_instrument_type_to_asset_class(itype, isin),
    )


def _dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _short(exc: Exception, n: int = 160) -> str:
    msg = str(exc) or exc.__class__.__name__
    return msg[:n]


__all__ = [
    "TradeRepublicProvider",
    "initiate_login",
    "complete_login",
    "cleanup_login",
    "normalize_phone",
    "map_instrument_type_to_asset_class",
]
