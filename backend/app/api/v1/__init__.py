"""API v1 router — aggregates all sub-routers."""

from fastapi import APIRouter

from app.api.v1 import (
    accounts,
    categories,
    transactions,
    statements,
    assets,
    holdings,
    market,
    cashflow,
    rules,
    system,
)

api_router = APIRouter()

api_router.include_router(accounts.router, prefix="/accounts", tags=["Accounts"])
api_router.include_router(categories.router, prefix="/categories", tags=["Categories"])
api_router.include_router(transactions.router, prefix="/transactions", tags=["Transactions"])
api_router.include_router(statements.router, prefix="/statements", tags=["Statements"])
api_router.include_router(assets.router, prefix="/assets", tags=["Assets"])
api_router.include_router(holdings.router, prefix="/holdings", tags=["Holdings"])
api_router.include_router(market.router, prefix="/market", tags=["Market Data"])
api_router.include_router(cashflow.router, prefix="/cashflow", tags=["Cash Flow"])
api_router.include_router(rules.router, prefix="/rules", tags=["Categorization Rules"])
api_router.include_router(system.router, prefix="/system", tags=["System"])
