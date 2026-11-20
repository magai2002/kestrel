"""``LiveBroker`` for Interactive Brokers via ``ib_async`` (ADR 0004).

Used for equities ``sandbox`` (IBKR paper account, Gateway port 4002)
and — only once the Phase 7 gates pass — ``live`` (port 4001). The
engine talks to an **IB Gateway** process (Docker, IBC auto-login);
the IBKR username/password live in that container, never here.

Verified against ``ib_async`` 2.1 source (``docs/api-notes.md``):

- ``IB.placeOrder(contract, LimitOrder(action, qty, lmtPrice, orderRef=...))``
  returns a live-updating ``Trade``; ``orderRef`` is free text, so our
  full deterministic client id goes there unchanged (design rule 6).
- Order lookup after a restart: ``IB.trades()`` only knows this
  session, so we also ask ``reqAllOpenOrders()`` and
  ``reqCompletedOrders(apiOnly=True)``, matching on ``order.orderRef``;
  fills come from ``reqExecutions()`` (``Fill.execution.orderRef``,
  ``execId`` for de-duplication, ``commissionReport.commission``).
- ``OrderStatus`` strings: ``Filled``; ``Cancelled``/``ApiCancelled``;
  ``Inactive`` (rejected, or held by IBKR's own risk checks); everything
  else (``PendingSubmit``, ``PreSubmitted``, ``Submitted``...) is open.
- Positions: ``IB.positions(account)`` -> ``Position(account, contract,
  position, avgCost)``. Cash: ``accountValues()`` rows with
  ``tag == "CashBalance"`` and the sleeve's currency.

**Not verifiable here** (no gateway on this box yet): the exact
contract IBKR resolves for each UCITS twin. ``kestrel ibkr contracts``
qualifies each one against a running gateway and stores conId and
minTick; until then the adapter qualifies on first use and refuses an
ambiguous match.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from core.order import OrderSide
from execution.brokers.base import (
    BrokerError,
    BrokerFill,
    BrokerOrder,
    BrokerOrderStatus,
    TransientBrokerError,
)

PAPER_PORT = 4002
LIVE_PORT = 4001


def _status(ib_status: str) -> BrokerOrderStatus:
    if ib_status == "Filled":
        return BrokerOrderStatus.FILLED
    if ib_status in ("Cancelled", "ApiCancelled"):
        return BrokerOrderStatus.CANCELLED
    if ib_status == "Inactive":
        return BrokerOrderStatus.REJECTED
    return BrokerOrderStatus.OPEN


def _d(x: Any) -> Decimal:
    return Decimal(str(x)) if x is not None else Decimal(0)


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


class IbkrBroker:
    def __init__(
        self,
        ib: Any,
        contracts: dict[str, Any],
        account: str = "",
        currency: str = "USD",
        name: str = "ibkr",
    ) -> None:
        """``contracts``: our symbol (``VUSD``) -> qualified ``ib_async.Contract``."""
        self.name = name
        self._ib = ib
        self._contracts = contracts
        self._account = account
        self._currency = currency

    # --- helpers ---------------------------------------------------------
    def _ensure_connected(self) -> None:
        if not self._ib.isConnected():
            raise TransientBrokerError(f"{self.name}: not connected to IB Gateway")

    def _symbol_of(self, contract: Any) -> str:
        for sym, c in self._contracts.items():
            if getattr(c, "conId", None) and c.conId == getattr(contract, "conId", None):
                return sym
        return str(getattr(contract, "symbol", ""))

    def _find_trade(self, client_order_id: str, refresh: bool) -> Any | None:
        for t in self._ib.trades():
            if t.order.orderRef == client_order_id:
                return t
        if not refresh:
            return None
        for t in list(self._ib.reqAllOpenOrders()) + list(self._ib.reqCompletedOrders(True)):
            if t.order.orderRef == client_order_id:
                return t
        return None

    def _fills_for(self, client_order_id: str) -> list[Any]:
        seen: dict[str, Any] = {}
        for f in list(self._ib.fills()) + list(self._ib.reqExecutions()):
            if f.execution.orderRef == client_order_id:
                seen[f.execution.execId] = f
        return list(seen.values())

    def _to_order(self, trade: Any, client_order_id: str, fills: list[Any]) -> BrokerOrder:
        symbol = self._symbol_of(trade.contract)
        side = OrderSide.BUY if trade.order.action.upper() == "BUY" else OrderSide.SELL
        broker_fills = tuple(
            BrokerFill(
                fill_id=f.execution.execId,
                client_order_id=client_order_id,
                symbol=symbol,
                side=side,
                quantity=_d(f.execution.shares),
                price=_d(f.execution.price),
                fee=_d(f.commissionReport.commission if f.commissionReport else 0),
                timestamp=_utc(f.execution.time),
            )
            for f in fills
        )
        filled = sum((bf.quantity for bf in broker_fills), Decimal(0)) or _d(
            trade.orderStatus.filled
        )
        status = _status(trade.orderStatus.status)
        reject = None
        if status is BrokerOrderStatus.REJECTED:
            reject = (
                "; ".join(e.message for e in trade.log if getattr(e, "message", "")) or "Inactive"
            )
        avg = _d(trade.orderStatus.avgFillPrice) if trade.orderStatus.avgFillPrice else None
        return BrokerOrder(
            client_order_id=client_order_id,
            broker_order_id=str(trade.order.permId or trade.order.orderId),
            symbol=symbol,
            side=side,
            quantity=_d(trade.order.totalQuantity),
            limit_price=_d(trade.order.lmtPrice) if trade.order.lmtPrice else None,
            status=status,
            filled_quantity=filled,
            avg_fill_price=avg,
            fills=broker_fills,
            reject_reason=reject,
        )

    # --- LiveBroker ------------------------------------------------------
    def server_time(self) -> datetime:
        self._ensure_connected()
        return _utc(self._ib.reqCurrentTime())

    def place_limit_order(
        self,
        client_order_id: str,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        limit_price: Decimal,
    ) -> BrokerOrder:
        from ib_async import LimitOrder

        self._ensure_connected()
        if quantity != quantity.to_integral_value():
            raise BrokerError(f"{self.name}: {symbol} trades in whole shares, got {quantity}")
        order = LimitOrder(
            "BUY" if side is OrderSide.BUY else "SELL",
            float(quantity),
            float(limit_price),
            orderRef=client_order_id,
            tif="DAY",
            account=self._account,
        )
        try:
            trade = self._ib.placeOrder(self._contracts[symbol], order)
            self._ib.sleep(1.0)  # let the gateway ack (or reject) before we report state
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise TransientBrokerError(f"{self.name}: {exc}") from exc
        result = self._to_order(trade, client_order_id, self._fills_for(client_order_id))
        if result.status is BrokerOrderStatus.REJECTED:
            raise BrokerError(
                f"{self.name}: order {client_order_id} rejected: {result.reject_reason}"
            )
        return result

    def get_order(self, client_order_id: str, symbol: str) -> BrokerOrder | None:
        self._ensure_connected()
        trade = self._find_trade(client_order_id, refresh=True)
        if trade is None:
            return None
        return self._to_order(trade, client_order_id, self._fills_for(client_order_id))

    def cancel_order(self, client_order_id: str, symbol: str) -> BrokerOrder | None:
        self._ensure_connected()
        trade = self._find_trade(client_order_id, refresh=True)
        if trade is None:
            return None
        if _status(trade.orderStatus.status).is_final:
            return self._to_order(trade, client_order_id, self._fills_for(client_order_id))
        self._ib.cancelOrder(trade.order)
        self._ib.sleep(1.0)
        return self._to_order(trade, client_order_id, self._fills_for(client_order_id))

    def open_orders(self, symbols: list[str]) -> list[BrokerOrder]:
        self._ensure_connected()
        out = []
        for t in self._ib.reqAllOpenOrders():
            sym = self._symbol_of(t.contract)
            if sym in symbols and t.order.orderRef:
                out.append(self._to_order(t, t.order.orderRef, self._fills_for(t.order.orderRef)))
        return out

    def positions(self, symbols: list[str]) -> dict[str, Decimal]:
        self._ensure_connected()
        out = {s: Decimal(0) for s in symbols}
        for p in self._ib.positions(self._account):
            sym = self._symbol_of(p.contract)
            if sym in out:
                out[sym] = _d(p.position)
        return out

    def cash(self, currency: str) -> Decimal:
        self._ensure_connected()
        for v in self._ib.accountValues(self._account):
            if v.tag == "CashBalance" and v.currency == currency:
                return _d(v.value)
        return Decimal(0)

    def last_price(self, symbol: str) -> Decimal | None:
        self._ensure_connected()
        try:
            [ticker] = self._ib.reqTickers(self._contracts[symbol])
        except Exception:  # no market-data subscription, or no quote right now
            return None
        px = ticker.marketPrice()
        return _d(px) if px == px and px > 0 else None  # NaN check


def qualify_contracts(ib: Any, listings: dict[str, tuple[str, str]]) -> dict[str, Any]:
    """``listings``: symbol -> (exchange, currency). Qualifies each
    ``Stock`` against the gateway; refuses ambiguous or missing matches
    rather than guessing (an ETF symbol can exist on several exchanges
    and in several currencies)."""
    from ib_async import Stock

    out: dict[str, Any] = {}
    for symbol, (exchange, currency) in listings.items():
        contract = Stock(symbol, "SMART", currency, primaryExchange=exchange)
        details = ib.reqContractDetails(contract)
        if len(details) != 1:
            raise BrokerError(
                f"IBKR resolved {len(details)} contracts for {symbol} on {exchange} in {currency}; "
                "expected exactly one"
            )
        out[symbol] = details[0].contract
    return out


def connect(host: str, port: int, client_id: int, account: str = "", timeout: float = 10) -> Any:
    from ib_async import IB

    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=timeout, account=account)
    except (ConnectionError, TimeoutError, OSError) as exc:
        raise TransientBrokerError(f"could not reach IB Gateway at {host}:{port}: {exc}") from exc
    return ib
