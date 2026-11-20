"""Venue fakes for the adapter contract suite.

``FakeCcxtExchange`` speaks ccxt's *unified* API and returns the unified
order / trade / balance structures documented in the ccxt Manual
(https://docs.ccxt.com/#/README?id=order-structure) — which is the
contract ``CcxtSpotBroker`` actually depends on. It can impersonate
Binance (lookup by ``origClientOrderId``) or Kraken (scan open/closed
orders for ``clientOrderId``).

``FakeIB`` uses ``ib_async``'s real ``Trade``/``Order``/``OrderStatus``/
``Fill``/``Execution``/``CommissionReport`` classes, so ``IbkrBroker``
is exercised against the genuine object shapes.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Any

import ccxt
from ib_async import CommissionReport, Contract, Execution, Fill, OrderStatus, Trade
from ib_async.objects import AccountValue, Position

NOW_MS = 1_767_268_800_000  # 2026-01-01T12:00:00Z


class FakeCcxtExchange:
    def __init__(
        self, venue_id: str, prices: dict[str, float], fill_immediately: bool = True
    ) -> None:
        self.id = venue_id
        self.prices = prices
        self.fill_immediately = fill_immediately
        self.orders: dict[str, dict[str, Any]] = {}
        self.trades: list[dict[str, Any]] = []
        self.balance: dict[str, float] = {"USDT": 10_000.0, "USD": 10_000.0, "BTC": 0.5, "ETH": 0.0}
        self.raise_next: Exception | None = None
        self._ids = itertools.count(1)

    def _maybe_raise(self) -> None:
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc

    def market(self, symbol: str) -> dict[str, Any]:
        base, quote = symbol.split("/")
        return {"symbol": symbol, "base": base, "quote": quote}

    def fetch_time(self) -> int:
        self._maybe_raise()
        return NOW_MS

    def create_order(
        self, symbol: str, type: str, side: str, amount: float, price: float, params: dict[str, Any]
    ) -> dict[str, Any]:
        self._maybe_raise()
        cid = params["clientOrderId"]
        if any(o["clientOrderId"] == cid and o["status"] == "open" for o in self.orders.values()):
            raise ccxt.InvalidOrder("Duplicate order sent.")
        oid = str(next(self._ids))
        order = {
            "id": oid,
            "clientOrderId": cid,
            "symbol": symbol,
            "type": type,
            "side": side,
            "amount": amount,
            "price": price,
            "filled": 0.0,
            "remaining": amount,
            "average": None,
            "status": "open",
            "timestamp": NOW_MS,
            "trades": None,
            "info": {},
        }
        self.orders[oid] = order
        if self.fill_immediately and (
            (side == "buy" and price >= self.prices[symbol])
            or (side == "sell" and price <= self.prices[symbol])
        ):
            self._fill(order, amount)
        # Binance returns the full order; many venues return a sparse ack. Return sparse.
        return {"id": oid, "clientOrderId": cid, "info": {}}

    def _fill(self, order: dict[str, Any], qty: float) -> None:
        px = self.prices[order["symbol"]]
        tid = f"t{next(self._ids)}"
        self.trades.append(
            {
                "id": tid,
                "order": order["id"],
                "symbol": order["symbol"],
                "side": order["side"],
                "amount": qty,
                "price": px,
                "cost": qty * px,
                "timestamp": NOW_MS,
                "fee": {"cost": qty * px * 0.001, "currency": order["symbol"].split("/")[1]},
            }
        )
        order["filled"] += qty
        order["remaining"] -= qty
        order["average"] = px
        if order["remaining"] <= 1e-12:
            order["status"] = "closed"
        base, quote = order["symbol"].split("/")
        sign = 1 if order["side"] == "buy" else -1
        self.balance[base] = self.balance.get(base, 0.0) + sign * qty
        self.balance[quote] = self.balance.get(quote, 0.0) - sign * qty * px

    def fetch_order(
        self, id: str | None, symbol: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._maybe_raise()
        params = params or {}
        if self.id != "binance":
            raise ccxt.NotSupported("use open/closed scans")
        cid = params.get("origClientOrderId")
        for o in self.orders.values():
            if (cid and o["clientOrderId"] == cid) or (id and o["id"] == id):
                return dict(o)
        raise ccxt.OrderNotFound("Order does not exist.")

    def fetch_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        self._maybe_raise()
        return [
            dict(o) for o in self.orders.values() if o["symbol"] == symbol and o["status"] == "open"
        ]

    def fetch_closed_orders(self, symbol: str) -> list[dict[str, Any]]:
        return [
            dict(o) for o in self.orders.values() if o["symbol"] == symbol and o["status"] != "open"
        ]

    def cancel_order(self, id: str, symbol: str) -> dict[str, Any]:
        self._maybe_raise()
        o = self.orders[id]
        if o["status"] == "open":
            o["status"] = "canceled"
        return dict(o)

    def fetch_my_trades(
        self,
        symbol: str,
        since: Any = None,
        limit: Any = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return [dict(t) for t in self.trades if t["symbol"] == symbol]

    def fetch_balance(self) -> dict[str, Any]:
        self._maybe_raise()
        return {"total": dict(self.balance), "free": dict(self.balance), "used": {}}

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol, "last": self.prices[symbol], "close": self.prices[symbol]}


class FakeIB:
    """Just the ``ib_async.IB`` surface ``IbkrBroker`` uses."""

    def __init__(self, contracts: dict[str, Contract], prices: dict[str, float]) -> None:
        self.contracts = contracts
        self.prices = prices
        self._trades: list[Trade] = []
        self._fills: list[Fill] = []
        self._positions: dict[int, float] = {c.conId: 0.0 for c in contracts.values()}
        self.cash_usd = 10_000.0
        self.connected = True
        self._ids = itertools.count(1)
        self.fill_immediately = True

    def isConnected(self) -> bool:
        return self.connected

    def sleep(self, secs: float = 0.02) -> bool:
        return True

    def reqCurrentTime(self) -> datetime:
        return datetime(2026, 1, 1, 12, tzinfo=UTC)

    def placeOrder(self, contract: Contract, order: Any) -> Trade:
        order.orderId = next(self._ids)
        order.permId = 1000 + order.orderId
        trade = Trade(
            contract,
            order,
            OrderStatus(orderId=order.orderId, status="Submitted", remaining=order.totalQuantity),
            [],
            [],
        )
        self._trades.append(trade)
        px = self.prices[contract.symbol]
        crosses = (order.action == "BUY" and order.lmtPrice >= px) or (
            order.action == "SELL" and order.lmtPrice <= px
        )
        if self.fill_immediately and crosses:
            ex = Execution(
                execId=f"e{order.orderId}",
                time=datetime(2026, 1, 1, 12, tzinfo=UTC),
                side="BOT" if order.action == "BUY" else "SLD",
                shares=order.totalQuantity,
                price=px,
                permId=order.permId,
                orderId=order.orderId,
                orderRef=order.orderRef,
            )
            fill = Fill(
                contract,
                ex,
                CommissionReport(execId=ex.execId, commission=1.70, currency="USD"),
                ex.time,
            )
            trade.fills.append(fill)
            self._fills.append(fill)
            trade.orderStatus.status = "Filled"
            trade.orderStatus.filled = order.totalQuantity
            trade.orderStatus.remaining = 0
            trade.orderStatus.avgFillPrice = px
            sign = 1 if order.action == "BUY" else -1
            self._positions[contract.conId] += sign * order.totalQuantity
            self.cash_usd -= sign * order.totalQuantity * px + 1.70
        return trade

    def cancelOrder(self, order: Any) -> Trade | None:
        for t in self._trades:
            if (
                t.order.orderId == order.orderId
                and t.orderStatus.status not in OrderStatus.DoneStates
            ):
                t.orderStatus.status = "Cancelled"
                return t
        return None

    def trades(self) -> list[Trade]:
        return list(self._trades)

    def reqAllOpenOrders(self) -> list[Trade]:
        return [t for t in self._trades if t.orderStatus.status not in OrderStatus.DoneStates]

    def reqCompletedOrders(self, apiOnly: bool) -> list[Trade]:
        return [t for t in self._trades if t.orderStatus.status in OrderStatus.DoneStates]

    def fills(self) -> list[Fill]:
        return list(self._fills)

    def reqExecutions(self, execFilter: Any = None) -> list[Fill]:
        return list(self._fills)

    def positions(self, account: str = "") -> list[Position]:
        by_id = {c.conId: c for c in self.contracts.values()}
        return [Position("DU123", by_id[cid], qty, 0.0) for cid, qty in self._positions.items()]

    def accountValues(self, account: str = "") -> list[AccountValue]:
        return [
            AccountValue("DU123", "CashBalance", str(self.cash_usd), "USD", ""),
            AccountValue("DU123", "CashBalance", "500000", "EUR", ""),
        ]

    def reqTickers(self, *contracts: Contract) -> list[Any]:
        class _T:
            def __init__(self, px: float) -> None:
                self._px = px

            def marketPrice(self) -> float:
                return self._px

        return [_T(self.prices[c.symbol]) for c in contracts]
