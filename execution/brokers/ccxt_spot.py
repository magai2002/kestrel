"""``LiveBroker`` over ccxt's unified spot API — shared by the Binance
Spot Testnet (sandbox) and Kraken (live, Phase 7-gated) adapters.

Only ccxt's *unified* methods and structures are used (``create_order``,
``fetch_order``, ``cancel_order``, ``fetch_open_orders``,
``fetch_my_trades``, ``fetch_balance``, ``fetch_ticker``,
``fetch_time``), so the venue-specific JSON is ccxt's problem and this
adapter is tested against the unified structures documented in the ccxt
Manual. What *is* venue-specific (verified in ccxt 4.5.x source, see
``docs/api-notes.md``):

- the client order id param: ccxt maps ``params={"clientOrderId": ...}``
  to Binance's ``newClientOrderId`` and Kraken's ``cl_ord_id``;
- looking an order up by client id: ccxt's binance ``fetch_order(id,
  symbol, params)`` sends ``origClientOrderId`` instead of ``orderId``
  when that param is given (``id`` may be None; ``symbol`` is required).
  Kraken has no single-call lookup by ``cl_ord_id`` in ccxt, so we scan
  open, then closed, orders — see ``_fetch_by_client_id``.

Error mapping: ccxt's ``NetworkError`` family (timeouts, 5xx,
``RequestTimeout``, ``ExchangeNotAvailable``, ``DDoSProtection``) ->
``TransientBrokerError``; ``OrderNotFound`` -> ``None``; any other
``ExchangeError`` (insufficient funds, invalid order) -> ``BrokerError``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import ccxt

from core.order import OrderSide
from execution.brokers.base import (
    BrokerError,
    BrokerFill,
    BrokerOrder,
    BrokerOrderStatus,
    TransientBrokerError,
)
from execution.brokers.client_ids import venue_client_id

_STATUS = {
    "open": BrokerOrderStatus.OPEN,
    "closed": BrokerOrderStatus.FILLED,
    "canceled": BrokerOrderStatus.CANCELLED,
    "cancelled": BrokerOrderStatus.CANCELLED,
    "expired": BrokerOrderStatus.CANCELLED,
    "rejected": BrokerOrderStatus.REJECTED,
}


def _d(x: Any) -> Decimal:
    return Decimal(str(x)) if x is not None else Decimal(0)


class CcxtSpotBroker:
    """``symbols`` maps our store symbol (``BTCUSDT``, ``BTC-USD``) to the
    ccxt unified symbol (``BTC/USDT``, ``BTC/USD``)."""

    def __init__(self, name: str, exchange: Any, symbols: dict[str, str]) -> None:
        self.name = name
        self._ex = exchange
        self._symbols = symbols
        self._reverse = {v: k for k, v in symbols.items()}
        # venue client id -> our full client id, for orders we've seen.
        self._ids: dict[str, str] = {}

    # --- helpers ---------------------------------------------------------
    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ccxt.OrderNotFound:
            return None
        except ccxt.NetworkError as exc:  # timeouts, 5xx, DDoS protection, unavailable
            raise TransientBrokerError(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        except ccxt.ExchangeError as exc:
            raise BrokerError(f"{self.name}: {type(exc).__name__}: {exc}") from exc

    def _to_order(self, raw: dict[str, Any], client_order_id: str | None = None) -> BrokerOrder:
        venue_cid = raw.get("clientOrderId") or ""
        cid = client_order_id or self._ids.get(venue_cid, venue_cid)
        symbol = self._reverse.get(raw.get("symbol", ""), raw.get("symbol", ""))
        filled = _d(raw.get("filled"))
        status = _STATUS.get(str(raw.get("status")), BrokerOrderStatus.OPEN)
        fills = tuple(
            BrokerFill(
                fill_id=str(t.get("id")),
                client_order_id=cid,
                symbol=symbol,
                side=OrderSide(str(t.get("side") or raw.get("side"))),
                quantity=_d(t.get("amount")),
                price=_d(t.get("price")),
                fee=_d((t.get("fee") or {}).get("cost")),
                timestamp=datetime.fromtimestamp((t.get("timestamp") or 0) / 1000, UTC),
            )
            for t in (raw.get("trades") or [])
        )
        return BrokerOrder(
            client_order_id=cid,
            broker_order_id=str(raw.get("id")),
            symbol=symbol,
            side=OrderSide(raw["side"]),
            quantity=_d(raw.get("amount")),
            limit_price=_d(raw["price"]) if raw.get("price") is not None else None,
            status=status,
            filled_quantity=filled,
            avg_fill_price=_d(raw["average"]) if raw.get("average") else None,
            fills=fills,
            reject_reason=None
            if status is not BrokerOrderStatus.REJECTED
            else str(raw.get("info")),
        )

    def _with_trades(self, order: BrokerOrder, symbol: str) -> BrokerOrder:
        """Many venues don't embed trades in the order response; fetch
        them by order id so fills carry real prices, fees and trade ids."""
        if order.fills or order.filled_quantity == 0:
            return order
        trades = (
            self._call(
                self._ex.fetch_my_trades,
                self._symbols[symbol],
                None,
                None,
                {"orderId": order.broker_order_id},
            )
            or []
        )
        mine = [t for t in trades if str(t.get("order")) == order.broker_order_id]
        fills = tuple(
            BrokerFill(
                fill_id=str(t.get("id")),
                client_order_id=order.client_order_id,
                symbol=symbol,
                side=order.side,
                quantity=_d(t.get("amount")),
                price=_d(t.get("price")),
                fee=_d((t.get("fee") or {}).get("cost")),
                timestamp=datetime.fromtimestamp((t.get("timestamp") or 0) / 1000, UTC),
            )
            for t in mine
        )
        return BrokerOrder(**{**order.__dict__, "fills": fills})

    # --- LiveBroker ------------------------------------------------------
    def server_time(self) -> datetime:
        ms = self._call(self._ex.fetch_time)
        return datetime.fromtimestamp(int(ms) / 1000, UTC)

    def place_limit_order(
        self,
        client_order_id: str,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        limit_price: Decimal,
    ) -> BrokerOrder:
        vcid = venue_client_id(client_order_id)
        self._ids[vcid] = client_order_id
        raw = self._call(
            self._ex.create_order,
            self._symbols[symbol],
            "limit",
            side.value,
            float(quantity),
            float(limit_price),
            {"clientOrderId": vcid},
        )
        if raw is None:
            raise BrokerError(f"{self.name}: create_order returned nothing for {client_order_id}")
        # Some venues return a sparse ack (id only); normalise from what we sent.
        raw = {
            "symbol": self._symbols[symbol],
            "side": side.value,
            "amount": float(quantity),
            "price": float(limit_price),
            "status": "open",
            "clientOrderId": vcid,
            **{k: v for k, v in raw.items() if v is not None},
        }
        return self._to_order(raw, client_order_id)

    def _fetch_by_client_id(self, client_order_id: str, symbol: str) -> dict[str, Any] | None:
        vcid = venue_client_id(client_order_id)
        self._ids[vcid] = client_order_id
        if self._ex.id == "binance":
            found = self._call(
                self._ex.fetch_order, None, self._symbols[symbol], {"origClientOrderId": vcid}
            )
            return dict(found) if found is not None else None
        # Kraken & others: scan open + closed orders for the client id.
        for fn in (self._ex.fetch_open_orders, self._ex.fetch_closed_orders):
            orders = self._call(fn, self._symbols[symbol]) or []
            for o in orders:
                if o.get("clientOrderId") == vcid:
                    return dict(o)
        return None

    def get_order(self, client_order_id: str, symbol: str) -> BrokerOrder | None:
        raw = self._fetch_by_client_id(client_order_id, symbol)
        if raw is None:
            return None
        return self._with_trades(self._to_order(raw, client_order_id), symbol)

    def cancel_order(self, client_order_id: str, symbol: str) -> BrokerOrder | None:
        current = self.get_order(client_order_id, symbol)
        if current is None or current.status.is_final:
            return current
        self._call(self._ex.cancel_order, current.broker_order_id, self._symbols[symbol])
        return self.get_order(client_order_id, symbol)

    def open_orders(self, symbols: list[str]) -> list[BrokerOrder]:
        out = []
        for s in symbols:
            for raw in self._call(self._ex.fetch_open_orders, self._symbols[s]) or []:
                out.append(self._to_order(raw))
        return out

    def positions(self, symbols: list[str]) -> dict[str, Decimal]:
        bal = self._call(self._ex.fetch_balance) or {}
        total = bal.get("total") or {}
        out = {}
        for s in symbols:
            base = self._ex.market(self._symbols[s])["base"]
            out[s] = _d(total.get(base))
        return out

    def cash(self, currency: str) -> Decimal:
        bal = self._call(self._ex.fetch_balance) or {}
        return _d((bal.get("free") or {}).get(currency))

    def last_price(self, symbol: str) -> Decimal | None:
        t = self._call(self._ex.fetch_ticker, self._symbols[symbol]) or {}
        px = t.get("last") or t.get("close")
        return _d(px) if px else None


def to_ccxt_symbol(symbol: str, base: str, quote: str) -> str:
    return f"{base}/{quote}"
