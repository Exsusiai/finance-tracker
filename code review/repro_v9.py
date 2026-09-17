"""Isolated V9 audit probes. Assertions describe CORRECT behaviour.

Run separately from the normal suite (these expose unfixed bugs):
  PYTHONPATH=backend:mcp-server/src .venv/bin/pytest 'code review/repro_v9.py' -q

No application lifespan, real data, external services or credentials are used.
"""
import csv
import io
import json
import os
from decimal import Decimal as D

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["BASE_CURRENCY"] = "EUR"
os.environ["FINANCE_TRACKER_API_TOKEN"] = "isolated-review-v9"
os.environ["AUTH_DISABLED"] = "false"

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.core.auth import require_auth
from app.main import app, _BALANCE_VIEW_SQL, _index_migrations
from app.models import Account, Asset, AssetHolding, ChainAddress, ExchangeConnection, FxRate, MarketPrice, PdfImport, Transaction
from app.services.crypto_sync import BalanceItem
from app.services.transfer_matcher.engine import pair_transactions, replace_synthetic_with_real
from app.services.valuation.composition import compute_composition
from app.services.valuation.net_worth import compute_net_worth

DATE = "2026-09-10T12:00:00Z"


@pytest_asyncio.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    @event.listens_for(engine.sync_engine, "connect")
    def pragmas(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(_BALANCE_VIEW_SQL))
        for _name, ddl in _index_migrations:
            await conn.execute(text(ddl))
    async def db_override():
        async with sessions() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    old = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[require_auth] = lambda: "review"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://review") as client:
        yield sessions, client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(old)
    await engine.dispose()


async def account(s, name="bank", currency="EUR", kind="bank", initial="0"):
    a = Account(name=name, currency=currency, type=kind, initial_balance=D(initial))
    s.add(a)
    await s.flush()
    return a


async def tx(s, a, amount="100", kind="expense", **kw):
    row = Transaction(account_id=a.id, amount=D(amount), currency=a.currency,
                      type=kind, occurred_at=DATE, source="pdf_import", is_pending=False, **kw)
    s.add(row)
    await s.flush()
    return row


async def balance(s, aid):
    value = (await s.execute(text("SELECT balance FROM v_account_balance WHERE account_id=:id"), {"id": aid})).scalar_one()
    return D(str(value))


@pytest.mark.asyncio
async def test_split_preserves_foreign_base_amount(env):
    sessions, c = env
    async with sessions() as s:
        a = await account(s, currency="USD")
        t = await tx(s, a, base_amount=D("90"), fx_rate_to_base=D("0.9"))
        await s.commit()
    r = await c.post(f"/api/v1/transactions/{t.id}/split", json={"lines": [
        {"amount": "20", "type": "expense"}, {"amount": "80", "type": "expense"}]})
    assert r.status_code == 200, r.text
    actual = sum(D(x["base_amount"]) for x in r.json()["data"])
    assert actual == D("90"), f"split base total is {actual}, expected 90"


@pytest.mark.asyncio
async def test_statement_delete_removes_split_children(env):
    sessions, c = env
    async with sessions() as s:
        a = await account(s)
        pdf = PdfImport(filename="test.pdf", file_hash="a" * 64, file_size=8,
                        storage_path="", account_id=a.id, status="success")
        s.add(pdf)
        await s.flush()
        t = await tx(s, a, pdf_import_id=pdf.id)
        await s.commit()
    r = await c.post(f"/api/v1/transactions/{t.id}/split", json={"lines": [
        {"amount": "20", "type": "expense"}, {"amount": "80", "type": "expense"}]})
    assert r.status_code == 200, r.text
    r = await c.delete(f"/api/v1/statements/{pdf.id}")
    assert r.status_code == 200, r.text
    async with sessions() as s:
        assert await balance(s, a.id) == 0, "80 of deleted statement survives as a split child"


@pytest.mark.asyncio
async def test_split_preserves_signed_balance(env):
    sessions, c = env
    async with sessions() as s:
        a = await account(s)
        t = await tx(s, a)
        await s.commit()
    r = await c.post(f"/api/v1/transactions/{t.id}/split", json={"lines": [
        {"amount": "20", "type": "expense"}, {"amount": "80", "type": "income"}]})
    assert r.status_code in (200, 400, 422), r.text
    async with sessions() as s:
        assert await balance(s, a.id) == D("-100"), "opposite-sign split changes -100 into +60"


@pytest.mark.asyncio
async def test_foreign_transaction_does_not_pollute_account_unit(env):
    sessions, c = env
    async with sessions() as s:
        a = await account(s, initial="1000")
        s.add(FxRate(base_currency="USD", quote_currency="EUR", rate=D("0.9"), quoted_at=DATE, source="review"))
        await s.commit()
    r = await c.post("/api/v1/transactions", json={"account_id": a.id, "amount": "100", "currency": "USD", "type": "expense", "occurred_at": DATE})
    assert r.status_code in (201, 400, 422), r.text
    async with sessions() as s:
        # Either reject the currency mismatch or book the correct 90 EUR debit.
        assert await balance(s, a.id) in (D("1000"), D("910")), "100 USD is subtracted as 100 EUR"


@pytest.mark.asyncio
async def test_same_chain_addresses_are_aggregated(env, monkeypatch):
    from app.services.wallet_sync import orchestrator as o
    sessions, _ = env
    class Provider:
        async def fetch_balances(self, address):
            return [BalanceItem(symbol="ETH", contract=None, quantity=D(address), decimals=18)]
    async def skip_prices(*args):
        pass
    monkeypatch.setattr(o, "_dispatch_chain", lambda *args: Provider())
    monkeypatch.setattr(o, "_refresh_prices_for_account", skip_prices)
    async with sessions() as s:
        a = await account(s, kind="crypto_wallet", currency="USDT")
        s.add_all([ChainAddress(account_id=a.id, chain="ethereum", address=x) for x in ("1", "2")])
        await s.flush()
        result = await o.sync_account(s, a.id, alchemy_api_key=None)
        assert result.total_errors == 0
        actual = (await s.execute(select(AssetHolding.quantity).where(AssetHolding.account_id == a.id))).scalar_one()
        assert actual == D("3"), f"1 ETH + 2 ETH became {actual} ETH"


@pytest.mark.asyncio
async def test_partial_exchange_response_preserves_previous_holdings(env, monkeypatch):
    from app.services.wallet_sync import orchestrator as o
    from app.services.wallet_sync.upsert import apply_balance_snapshot
    from app.services.exchange_sync.bitget import BitgetProvider
    sessions, _ = env
    def respond(request):
        if request.url.path == "/api/v2/spot/account/assets":
            return httpx.Response(503)
        rows = [{"marginCoin": "USDT", "available": "10", "locked": "0"}] if request.url.params.get("productType") == "USDT-FUTURES" else []
        return httpx.Response(200, json={"code": "00000", "data": rows})
    async def skip_prices(*args):
        pass
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="https://mock.invalid") as http:
        monkeypatch.setattr(o, "_dispatch_exchange", lambda *args: BitgetProvider(http=http))
        monkeypatch.setattr(o, "decrypt_str", lambda value: value)
        monkeypatch.setattr(o, "_refresh_prices_for_account", skip_prices)
        async with sessions() as s:
            a = await account(s, kind="exchange", currency="USDT")
            conn = ExchangeConnection(account_id=a.id, exchange="bitget", api_key_enc="fake", api_secret_enc="fake", api_passphrase_enc="fake")
            s.add(conn)
            await apply_balance_snapshot(s, a.id, "", [BalanceItem(symbol="BTC", contract=None, quantity=D("1"), decimals=8)])
            result = await o.sync_account(s, a.id, alchemy_api_key=None)
            btc = (await s.execute(select(AssetHolding.quantity).join(Asset).where(Asset.symbol == "BTC"))).scalar_one()
            assert btc == D("1"), f"503 removed BTC; quantity={btc}, status={conn.last_sync_status}, errors={result.total_errors}"


@pytest.mark.asyncio
async def test_amount_and_date_alone_do_not_auto_erase_income_expense(env):
    from app.services.ingestion import ingest_transactions
    sessions, _ = env
    async with sessions() as s:
        a, b = await account(s, "Alpha"), await account(s, "Beta")
        expense = await tx(s, a, description="Groceries")
        income = await tx(s, b, kind="income", description="Savings interest")
        result = await ingest_transactions(s, [expense, income], auto_pair=True)
        assert expense.type == "expense" and income.type == "income", f"unrelated rows became transfers: {result.auto_paired}"


@pytest.mark.asyncio
async def test_synthetic_replacement_requires_matching_direction(env):
    sessions, _ = env
    async with sessions() as s:
        a, b = await account(s, "Alpha"), await account(s, "Beta")
        source = await tx(s, a)
        mirror = await tx(s, b, kind="income")
        await pair_transactions(s, source, mirror)
        meta = json.loads(mirror.metadata_json)
        meta["synthetic_counterleg"] = True
        mirror.metadata_json = json.dumps(meta)
        real = await tx(s, b, description="Groceries")
        await s.flush()
        before = await balance(s, b.id)
        await replace_synthetic_with_real(s, real_tx=real)
        await s.flush()
        assert await balance(s, b.id) == before, "unrelated -100 expense replaced +100 synthetic deposit"


@pytest.mark.asyncio
async def test_editing_transfer_does_not_hide_income_from_monthly(env):
    sessions, c = env
    async with sessions() as s:
        a, b = await account(s, "Alpha"), await account(s, "Beta")
        out, incoming = await tx(s, a), await tx(s, b, kind="income")
        await pair_transactions(s, out, incoming)
        await s.commit()
    r = await c.patch(f"/api/v1/transactions/{incoming.id}?apply_scope=single", json={"type": "income", "category_id": None})
    assert r.status_code == 200, r.text
    monthly = (await c.get("/api/v1/cashflow/monthly")).json()["data"]
    ts = (await c.get("/api/v1/cashflow/timeseries")).json()["data"]
    assert D(monthly[0]["income"]) == D(ts["income"][0]), f"monthly={monthly[0]['income']}, timeseries={ts['income'][0]}"


@pytest.mark.asyncio
async def test_inbox_income_to_transfer_preserves_direction(env):
    sessions, c = env
    async with sessions() as s:
        a = await account(s)
        t = await tx(s, a, kind="income")
        t.is_pending = True
        await s.commit()
    r = await c.post(f"/api/v1/transactions/inbox/{t.id}/confirm?apply_scope=single", json={"type": "transfer", "category_id": None})
    assert r.status_code == 200, r.text
    async with sessions() as s:
        assert await balance(s, a.id) == D("100"), "confirmation reverses +100 to -100"


@pytest.mark.asyncio
async def test_composition_keeps_large_negative_liabilities(env):
    sessions, _ = env
    async with sessions() as s:
        await account(s, "Debt", kind="credit_card", initial="-1000")
        broker = await account(s, "Broker", kind="brokerage")
        asset = Asset(symbol="STOCK", name="Stock", asset_class="us_stock", currency="EUR")
        s.add(asset)
        await s.flush()
        s.add_all([AssetHolding(account_id=broker.id, asset_id=asset.id, quantity=D("10")),
                   MarketPrice(asset_id=asset.id, quoted_at=DATE, price=D("1000"), currency="EUR", source="review")])
        await s.flush()
        nw = await compute_net_worth(s, "EUR")
        composition = await compute_composition(s, "EUR")
        assert composition.total == nw.net_worth, f"composition={composition.total}, net worth={nw.net_worth}, dust={composition.dust_excluded_count}"


def paypal_csv(rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=["Date", "Time", "Description", "Currency", "Gross", "Net", "Balance", "Transaction ID"])
    writer.writeheader()
    for date, desc, net, bal in rows:
        writer.writerow({"Date": date, "Time": "12:00:00", "Description": desc, "Currency": "EUR", "Gross": net, "Net": net, "Balance": bal, "Transaction ID": "REUSED-ID"})
    return stream.getvalue().encode()


@pytest.mark.asyncio
async def test_paypal_reused_id_is_stable_across_export_windows(env):
    from app.services.csv_parser.paypal import parse_paypal_csv
    from app.services.csv_import import import_csv_rows
    sessions, _ = env
    async with sessions() as s:
        a = await account(s)
        first = parse_paypal_csv(paypal_csv([("01.08.2026", "User Initiated Withdrawal", "-100", "0")]))
        second = parse_paypal_csv(paypal_csv([("01.09.2026", "Reversal of ACH Withdrawal Transaction", "100", "100")]))
        assert not first["error"] and not second["error"]
        await import_csv_rows(s, a.id, first)
        result = await import_csv_rows(s, a.id, second)
        assert result.imported == 1, f"distinct reversal skipped as duplicate: {result}"


@pytest.mark.asyncio
async def test_literal_promoted_route_is_reachable(env):
    _, c = env
    r = await c.get("/api/v1/transactions/recently-promoted-to-transfer")
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_patch_cannot_move_transaction_into_snapshot_account(env):
    sessions, c = env
    async with sessions() as s:
        a = await account(s)
        broker = await account(s, "Broker", kind="brokerage")
        t = await tx(s, a)
        await s.commit()
    r = await c.patch(f"/api/v1/transactions/{t.id}?apply_scope=single", json={"account_id": broker.id})
    assert r.status_code in (400, 422), f"snapshot guard bypassed: status {r.status_code}"


@pytest.mark.asyncio
async def test_fx_only_patch_updates_base_amount(env):
    sessions, c = env
    async with sessions() as s:
        a = await account(s, currency="USD")
        t = await tx(s, a, base_amount=D("90"), fx_rate_to_base=D("0.9"))
        await s.commit()
    r = await c.patch(f"/api/v1/transactions/{t.id}?apply_scope=single", json={"fx_rate_to_base": "0.8"})
    assert r.status_code == 200, r.text
    assert D(r.json()["data"]["base_amount"]) == D("80"), "edited FX=0.8, but higher-priority base_amount remains 90"


@pytest.mark.asyncio
async def test_uncategorized_income_and_expense_are_not_merged(env):
    sessions, c = env
    async with sessions() as s:
        a = await account(s)
        await tx(s, a, amount="100", kind="income")
        await tx(s, a, amount="20", kind="expense")
        await s.commit()
    r = await c.get("/api/v1/cashflow/by-category?period=2026-09")
    assert r.status_code == 200, r.text
    groups = {row["kind"]: D(row["total"]) for row in r.json()["data"]}
    assert groups == {"income": D("100"), "expense": D("20")}, f"uncategorized breakdown={groups}"


@pytest.mark.asyncio
async def test_base_currency_change_does_not_relabel_historical_amounts(env):
    from app.api.v1.system import settings
    sessions, c = env
    async with sessions() as s:
        a = await account(s, currency="GBP")
        await tx(s, a, kind="income", base_amount=D("90"), fx_rate_to_base=D("0.9"))
        s.add(FxRate(base_currency="EUR", quote_currency="CNY", rate=D("10"), quoted_at=DATE, source="review"))
        await s.commit()
    previous = settings.base_currency
    try:
        r = await c.patch("/api/v1/system/settings", json={"base_currency": "CNY"})
        assert r.status_code in (200, 400, 422), r.text
        if r.status_code != 200:
            return  # Forbidding a ledger-base switch is also correct.
        monthly = (await c.get("/api/v1/cashflow/monthly")).json()["data"][0]
    finally:
        settings.base_currency = previous
    assert D(monthly["income"]) == D("900"), f"90 EUR has been relabelled as {monthly['income']} {monthly['base_currency']}"


@pytest.mark.asyncio
async def test_llm_response_does_not_overwrite_later_manual_confirmation(env, monkeypatch):
    from types import SimpleNamespace
    from app.models import Category
    from app.services.llm import classifier
    from app.services.llm.provider import ClassificationResult
    sessions, _ = env
    async def runtime(_db):
        return SimpleNamespace(enabled=True, max_notes_in_prompt=0, use_grounding=False, confidence_threshold=0.8)
    async def budget(_db):
        return True, 0, 10
    monkeypatch.setattr(classifier.app_settings_svc, "get_llm_settings", runtime)
    monkeypatch.setattr(classifier, "check_budget", budget)
    async with sessions() as s:
        a = await account(s)
        food, travel = Category(name="Food", kind="expense"), Category(name="Travel", kind="expense")
        s.add_all([food, travel])
        t = await tx(s, a)
        t.is_pending = True
        await s.commit()
        class Provider:
            model = "fake-review"
            async def classify(self, *args, **kwargs):
                # Manual confirmation commits while the provider request is outstanding.
                async with sessions() as user_session:
                    user_row = await user_session.get(Transaction, t.id)
                    user_row.category_id = travel.id
                    user_row.is_pending = False
                    user_row.categorization_method = "user"
                    await user_session.commit()
                return ClassificationResult(category_path="Food", confidence=0.99, reason="fake", used_search=False, input_tokens=0, output_tokens=0, cost_usd=0)
        await classifier.classify_with_llm(s, t, provider=Provider())
        await s.commit()
    async with sessions() as s:
        actual = await s.get(Transaction, t.id)
        assert actual.category_id == travel.id and actual.categorization_method == "user", f"manual category {travel.id} overwritten by {actual.category_id}/{actual.categorization_method}"


@pytest.mark.asyncio
async def test_mcp_import_failure_rolls_back_inserted_rows(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from finance_mcp import server
    from app.services.pdf_parser import engine as parser
    path = tmp_path / "mcp-review.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        a = Account(name="Review", type="bank", currency="EUR", initial_balance=D("0"))
        s.add(a)
        s.commit()
        aid = a.id
    monkeypatch.setattr(server, "_DB_PATH", path)
    monkeypatch.setattr(server, "settings", SimpleNamespace(pdf_storage_dir=tmp_path / "pdfs", base_currency="EUR"))
    async def parsed(*args, **kwargs):
        # Fault injection: the second parsed row fails a real database constraint.
        return {"detected_bank": "test", "transactions": [
            {"occurred_at": DATE, "amount": "100", "currency": "EUR", "type": "expense"},
            {"occurred_at": DATE, "amount": "200", "currency": "EUR", "type": "invalid"},
        ]}
    monkeypatch.setattr(parser, "parse_pdf_statement", parsed)
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.7\nmocked-content")
    result = await server.parse_bank_statement(str(pdf), account_id=aid, auto_confirm=True)
    assert result["success"] is False, result
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM transactions WHERE deleted_at IS NULL")).scalar_one()
    engine.dispose()
    assert count == 0, f"failed MCP import committed {count} transaction(s)"


def test_amex_january_statement_places_december_in_previous_year():
    from app.services.pdf_parser.engine import _parse_amex_de
    rows = _parse_amex_de("Datum 08.01.26\nAbrechnungszeitraum vom 09.12.25 bis 08.01.26\n28.12 29.12 GROCERY SHOP 10,00\n")
    assert len(rows) == 1, rows
    assert rows[0]["occurred_at"].startswith("2025-12-28"), rows[0]


@pytest.mark.asyncio
async def test_refresh_matching_respects_manual_category_confirmation(env):
    from app.services.refresh_matching import RefreshContext, step_redetect_type
    sessions, _ = env
    async with sessions() as s:
        a = await account(s)
        t = await tx(s, a, description="SEPA direct debit Insurance", categorization_method="user")
        await step_redetect_type(RefreshContext(db=s))
        assert t.type == "expense", "refresh-matching overwrites a user-confirmed expense as transfer"


def test_sepa_direct_debit_is_not_automatically_an_own_account_transfer():
    from app.services.pdf_parser.engine import _parse_n26
    rows = _parse_n26("SEPA Direct Debit Insurance 10.09.2026 -100,00€\n")
    assert len(rows) == 1, rows
    assert rows[0]["type"] == "expense", rows[0]
