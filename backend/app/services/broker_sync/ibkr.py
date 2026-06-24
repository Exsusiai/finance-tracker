"""Interactive Brokers Flex Web Service provider.

Two-step download against the stable Flex endpoints, then parse with
``ibflex``. We deliberately do NOT use ibflex's bundled ``client`` (it's
synchronous ``requests`` and the maintained fork hardcodes an experimental
v1 URL) — only its battle-tested XML ``parser``.

Flex Web Service flow (v3):
  1. GET SendRequest?t=<token>&q=<queryId>&v=3
       → <FlexStatementResponse><Status>Success</Status>
            <ReferenceCode>NNN</ReferenceCode><Url>…GetStatement</Url>
       → or Status=Fail/Warn with <ErrorCode>/<ErrorMessage>
  2. GET GetStatement?t=<token>&q=<referenceCode>&v=3
       → the FlexQueryResponse XML once ready
       → or a FlexStatementResponse with code 1019/1009 ("generating,
         try again shortly") / 1018 ("throttled") → wait & retry

Docs: https://www.interactivebrokers.com/campus/ibkr-api-page/flex-web-service/
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable

import httpx
import structlog

from app.services.broker_sync import BrokerPosition, BrokerSyncError

log = structlog.get_logger(__name__)

# Stable, documented service locations (NOT the fork's experimental v1 URL).
_FLEX_BASE = "https://gdcdyn.interactivebrokers.com/Universal/servlet"
_SEND_URL = f"{_FLEX_BASE}/FlexStatementService.SendRequest"
_GET_URL = f"{_FLEX_BASE}/FlexStatementService.GetStatement"

# IBKR rejects requests without a user-agent; their own examples use "Java".
_HEADERS = {"user-agent": "finance-tracker/1.0"}

# Error codes that mean "not an error, just retry shortly".
_SERVER_BUSY = frozenset({"1009", "1019"})
_THROTTLED = frozenset({"1018"})

# Human-readable hints for the hard-failure codes worth distinguishing in the
# UI. Anything not listed surfaces with its code + IBKR's own message.
_FATAL_HINTS: dict[str, str] = {
    "1010": "Legacy Flex Queries are no longer supported — recreate as Activity Flex.",
    "1012": "Flex token has expired — generate a new one in IBKR Client Portal.",
    "1013": "IP restriction — this server's IP isn't allowed for the token.",
    "1014": "Flex Query is invalid — check the Query ID.",
    "1015": "Flex token is invalid — re-enter it.",
    "1016": "Account is invalid for this token.",
}


class IBKRFlexProvider:
    """Fetch open positions from IBKR via the Flex Web Service.

    ``client`` / ``sleep`` are injectable so tests can drive the two-step
    flow with ``httpx.MockTransport`` and zero real waiting.
    """

    provider_id = "ibkr"

    def __init__(
        self,
        *,
        token: str,
        query_id: str,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        max_tries: int = 8,
        poll_interval_sec: float = 5.0,
    ) -> None:
        self._token = token
        self._query_id = query_id
        self._client = client
        self._sleep = sleep or asyncio.sleep
        self._max_tries = max_tries
        self._poll_interval = poll_interval_sec

    async def fetch_positions(self) -> list[BrokerPosition]:
        xml_bytes = await self._download()
        return _parse_positions(xml_bytes)

    # ─── 2-step download ────────────────────────────────────────────────

    async def _download(self) -> bytes:
        if self._client is not None:
            return await self._run(self._client)
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await self._run(client)

    async def _run(self, client: httpx.AsyncClient) -> bytes:
        ref_code, get_url = await self._request_statement(client)
        for attempt in range(self._max_tries):
            resp = await client.get(
                get_url,
                params={"v": "3", "t": self._token, "q": ref_code},
                headers=_HEADERS,
            )
            resp.raise_for_status()
            body = resp.content
            if b"FlexQueryResponse" in body:
                return body
            # Otherwise it's a FlexStatementResponse status/error.
            code, message = _parse_status(body)
            if code in _SERVER_BUSY or code in _THROTTLED:
                wait = self._poll_interval * (2 if code in _THROTTLED else 1)
                log.info(
                    "ibkr_flex_statement_pending",
                    code=code, attempt=attempt + 1, wait=wait,
                )
                await self._sleep(wait)
                continue
            raise BrokerSyncError(_fatal_message(code, message))
        raise BrokerSyncError(
            "IBKR Flex statement was still generating after "
            f"{self._max_tries} attempts — try again shortly."
        )

    async def _request_statement(self, client: httpx.AsyncClient) -> tuple[str, str]:
        """Step 1 → (reference_code, get_statement_url)."""
        resp = await client.get(
            _SEND_URL,
            params={"v": "3", "t": self._token, "q": self._query_id},
            headers=_HEADERS,
        )
        resp.raise_for_status()
        root = _safe_xml(resp.content)
        if root.tag != "FlexStatementResponse":
            raise BrokerSyncError("Unexpected response from IBKR Flex SendRequest.")
        data = {child.tag: (child.text or "") for child in root}
        status = data.get("Status", "")
        if status == "Success":
            ref = data.get("ReferenceCode", "").strip()
            url = data.get("Url", "").strip() or _GET_URL
            if not ref:
                raise BrokerSyncError("IBKR Flex SendRequest returned no ReferenceCode.")
            return ref, url
        code = data.get("ErrorCode", "").strip()
        message = data.get("ErrorMessage", "").strip()
        raise BrokerSyncError(_fatal_message(code, message))


# ─── XML helpers ─────────────────────────────────────────────────────────────


def _safe_xml(content: bytes) -> ET.Element:
    try:
        return ET.fromstring(content)
    except ET.ParseError as exc:
        raise BrokerSyncError("Could not parse IBKR Flex response XML.") from exc


def _parse_status(content: bytes) -> tuple[str, str]:
    """Read a FlexStatementResponse status/error → (code, message)."""
    root = _safe_xml(content)
    data = {child.tag: (child.text or "") for child in root}
    return data.get("ErrorCode", "").strip(), data.get("ErrorMessage", "").strip()


def _fatal_message(code: str, message: str) -> str:
    hint = _FATAL_HINTS.get(code)
    if hint:
        return f"IBKR Flex error {code}: {hint}"
    base = message or "request failed"
    return f"IBKR Flex error {code or '?'}: {base}"


def _parse_positions(xml_bytes: bytes) -> list[BrokerPosition]:
    """Parse a FlexQueryResponse → list[BrokerPosition].

    We read attributes directly with ElementTree rather than ibflex's strict
    dataclass parser: IBKR adds new Flex attributes regularly and the strict
    parser blows up on any it doesn't know (and the pure-Python tolerance
    flag isn't reliably available). We only need a handful of attributes, so
    reading them by name is both simpler and immune to schema drift.
    """
    try:
        root = _safe_xml(xml_bytes)
    except BrokerSyncError:
        raise BrokerSyncError("Could not parse IBKR Flex statement.")

    out: list[BrokerPosition] = []
    for stmt in root.iter("FlexStatement"):
        for p in stmt.iter("OpenPosition"):
            a = p.attrib
            symbol = (a.get("symbol") or "").strip()
            if not symbol:
                # A position with no symbol can't be made into an Asset row.
                continue
            out.append(
                BrokerPosition(
                    symbol=symbol,
                    conid=(a.get("conid") or "").strip() or None,
                    asset_category=(a.get("assetCategory") or "").strip(),
                    currency=(a.get("currency") or "").strip().upper(),
                    quantity=_as_decimal(a.get("position")),
                    mark_price=_opt_decimal(a.get("markPrice")),
                    avg_cost=_opt_decimal(a.get("costBasisPrice")),
                    description=(a.get("description") or "").strip() or None,
                )
            )
        out.extend(_parse_cash(stmt))
    return out


def _parse_cash(stmt) -> list[BrokerPosition]:
    """Cash balances → cash-class positions (price 1:1), so they roll into
    the account total like any holding.

    Prefers ``CashReport`` (explicit per-currency ``endingCash``; the
    ``BASE_SUMMARY`` aggregate row is skipped). Falls back to
    ``EquitySummaryInBase.cash`` when only that section is present. Either
    section must be enabled in the user's Flex Query; if neither is, this
    returns [] (positions still sync).
    """
    out: list[BrokerPosition] = []
    seen: set[str] = set()

    for row in stmt.iter("CashReportCurrency"):
        ccy = (row.get("currency") or "").strip().upper()
        # Skip the aggregate row + anything that isn't a real ISO currency.
        if not ccy or ccy == "BASE_SUMMARY" or len(ccy) != 3:
            continue
        amount = _opt_decimal(row.get("endingCash"))
        if amount is None or amount == 0:
            continue
        out.append(_cash_position(ccy, amount))
        seen.add(ccy)

    if not out:
        for row in stmt.iter("EquitySummaryByReportDateInBase"):
            ccy = (row.get("currency") or "").strip().upper() or "USD"
            amount = _opt_decimal(row.get("cash"))
            if amount is None or amount == 0 or ccy in seen:
                continue
            out.append(_cash_position(ccy, amount))
            seen.add(ccy)

    return out


def _cash_position(currency: str, amount: Decimal) -> BrokerPosition:
    return BrokerPosition(
        symbol=currency,
        conid=None,
        asset_category="cash",
        currency=currency,
        quantity=amount,
        mark_price=Decimal("1"),
        avg_cost=None,
        description=f"{currency} 现金",
        asset_class="cash",
    )


def _as_decimal(value) -> Decimal:
    d = _opt_decimal(value)
    return d if d is not None else Decimal("0")


def _opt_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
