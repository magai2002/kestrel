"""Public API returns only shareable numbers; admin API is a separate app."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from api.app import create_admin_app, create_public_app
from api.ratelimit import TokenBucket, client_key
from live.state import FillRow, SleeveState, SqlStateStore

T0 = datetime(2026, 11, 20, 12, tzinfo=UTC)


def _seed(engine: Any) -> sessionmaker[Session]:
    factory = sessionmaker(engine)
    with factory() as s:
        store = SqlStateStore(s)
        st = SleeveState.fresh(Decimal(10_000), ["BTC", "ETH"], T0)
        st.cash = Decimal("1234.56")  # must never appear in public output
        st.positions = {"BTC": Decimal("0.0712345"), "ETH": Decimal(0)}
        st.last_bar_close = T0.isoformat()
        store.save("sim", "crypto", st, T0)
        for i in range(3):
            store.snapshot(
                "sim",
                "crypto",
                T0 + timedelta(hours=4 * i),
                Decimal(10_000 + i * 100),
                Decimal("1234.56"),
                100.0 + i,
                -0.01 * i,
                0.5,
                {
                    "BTC": {"quantity": "0.0712345", "price": "61000", "weight": 0.43},
                    "ETH": {"quantity": "0", "price": "3000", "weight": 0.0},
                },
            )
        store.record_fill(
            "sim",
            "crypto",
            FillRow(
                "crypto-x-0",
                "sim-1",
                "BTC",
                "buy",
                Decimal("0.0712345"),
                Decimal("61061"),
                Decimal("4.35"),
                T0,
                Decimal("61000"),
            ),
        )
        store.trip("sim", "crypto", "stale_data", "feed down", T0)
    return factory


def test_public_endpoints_and_no_absolute_balances(fresh_db: Any) -> None:
    factory = _seed(fresh_db)
    client = TestClient(create_public_app(factory, clock=lambda: T0 + timedelta(hours=1)))
    status = client.get("/api/status").json()
    [row] = status["sleeves"]
    assert row["kill_switches"]["stale_data"]["tripped"] is True
    assert row["data_age_seconds"] == 3600
    assert "Not financial advice" in status["disclaimer"]

    eq = client.get("/api/equity", params={"sleeve": "crypto", "mode": "sim"}).json()
    assert [v for _, v in eq["equity_index"]] == [100.0, 101.0, 102.0]

    [pos] = client.get("/api/positions").json()
    assert pos["weights_pct"] == {"BTC": 43.0, "ETH": 0.0}

    [trade] = client.get("/api/trades").json()
    assert round(trade["slippage_bps"], 4) == 10.0  # (61061 - 61000) / 61000
    assert trade["fee_bps"] > 0

    everything = json.dumps(
        [status, eq, pos, trade, client.get("/api/tracking", params={"sleeve": "crypto"}).json()]
    )
    for secret in ("1234.56", "0.0712345", "10000", "4.35"):
        assert secret not in everything, f"absolute value {secret} leaked into public API"


def test_unknown_sleeve_or_mode_is_404(fresh_db: Any) -> None:
    client = TestClient(create_public_app(_seed(fresh_db)))
    assert client.get("/api/equity", params={"sleeve": "forex"}).status_code == 404
    assert client.get("/api/positions", params={"mode": "prod"}).status_code == 404


def test_public_app_has_no_admin_routes(fresh_db: Any) -> None:
    client = TestClient(create_public_app(_seed(fresh_db)))
    for path in ("/admin/status", "/api/admin/status"):
        assert client.get(path).status_code in (404, 405)
    assert client.post("/admin/sim/crypto/pause").status_code in (404, 405)


def test_admin_app_resets_and_requires_token(fresh_db: Any) -> None:
    factory = _seed(fresh_db)
    client = TestClient(create_admin_app(factory, token="s3cret"))
    assert client.post("/admin/sim/crypto/kill-switches/stale_data/reset").status_code == 401
    ok = client.post(
        "/admin/sim/crypto/kill-switches/stale_data/reset",
        headers={"Authorization": "Bearer s3cret"},
    )
    assert ok.json() == {"reset": True}
    with factory() as s:
        assert not SqlStateStore(s).kill_switches("sim", "crypto")["stale_data"].tripped


def test_token_bucket_and_forwarded_for_only_from_trusted_proxy() -> None:
    t = [0.0]
    b = TokenBucket(rate_per_second=1, burst=2, clock=lambda: t[0])
    assert b.allow("a") and b.allow("a") and not b.allow("a")
    t[0] = 1.0
    assert b.allow("a")

    class Req:
        def __init__(self, peer: str, xff: str) -> None:
            self.client = type("C", (), {"host": peer})()
            self.headers = {"x-forwarded-for": xff}

    assert client_key(Req("127.0.0.1", "1.2.3.4, 5.6.7.8")) == "5.6.7.8"  # type: ignore[arg-type]
    assert client_key(Req("9.9.9.9", "1.2.3.4")) == "9.9.9.9"  # type: ignore[arg-type]


def test_rate_limit_returns_429(fresh_db: Any) -> None:
    client = TestClient(create_public_app(_seed(fresh_db), rate_per_second=0.001, burst=3))
    codes = [client.get("/api/healthz").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[-1] == 429


def test_public_app_hides_its_docs_and_404s_unknown_api_paths(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><div id=root></div>")
    client = TestClient(create_public_app(lambda: None, web_dist=tmp_path))  # type: ignore[arg-type,return-value]
    for path in ("/api/docs", "/api/openapi.json", "/api/nope", "/api"):
        resp = client.get(path)
        assert resp.status_code == 404, path
        assert "<!doctype" not in resp.text  # an API 404, not the dashboard shell
    assert client.get("/decisions").text.startswith("<!doctype")  # client-side routes still work
    with_docs = TestClient(create_public_app(lambda: None, docs=True))  # type: ignore[arg-type,return-value]
    assert with_docs.get("/api/openapi.json").status_code == 200


def test_head_answers_like_get_without_a_body(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "index.html").write_text("<!doctype html><div id=root></div>")
    client = TestClient(create_public_app(lambda: None, web_dist=tmp_path))  # type: ignore[arg-type,return-value]
    for path in ("/", "/how", "/assets/app.js", "/api/healthz"):
        get, head = client.get(path), client.head(path)
        assert head.status_code == get.status_code == 200, path
        assert head.content == b"", path
        assert head.headers["content-length"] == get.headers["content-length"], path
        assert head.headers["content-type"] == get.headers["content-type"], path
    assert client.head("/api/nope").status_code == 404
    assert client.head("/api/stream").status_code == 405  # would hold a connection open for nothing
    assert client.post("/").status_code == 405  # still read-only


def test_html_shell_is_revalidated_and_hashed_assets_are_immutable(tmp_path: Path) -> None:
    # A stale cached shell names a bundle the last deploy deleted -> blank page.
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1)")
    (tmp_path / "index.html").write_text("<!doctype html><div id=root></div>")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    client = TestClient(create_public_app(lambda: None, web_dist=tmp_path))  # type: ignore[arg-type,return-value]
    for path in ("/", "/strategies", "/trades", "/index.html"):
        assert client.get(path).headers["cache-control"] == "no-cache", path
    assert "immutable" in client.get("/assets/index-abc123.js").headers["cache-control"]
    assert client.get("/favicon.svg").headers["cache-control"] == "public, max-age=3600"
    gone = client.get("/assets/index-gone.js")  # a 404 must not be cached either
    assert gone.status_code == 404
    assert "cache-control" not in gone.headers


def test_live_stream_connections_are_capped() -> None:
    client = TestClient(create_public_app(lambda: None, max_streams=0))  # type: ignore[arg-type,return-value]
    assert client.get("/api/stream").status_code == 503


def test_status_says_when_the_runner_next_acts() -> None:
    from api.app import _next_decision

    # crypto: 4h UTC grid + 30 s publication delay; before the delay has passed
    # the current bar's action is still ahead.
    at = datetime(2026, 11, 21, 20, 0, 10, tzinfo=UTC)
    assert _next_decision("crypto", "sim", at) == "2026-11-21T20:00:30+00:00"
    assert _next_decision("crypto", "sim", at + timedelta(minutes=1)) == "2026-11-22T00:00:30+00:00"
    # equities: Saturday -> Monday's LSE close (16:30 UTC) + 15 min
    assert _next_decision("equities", "sim", at) == "2026-11-23T16:45:00+00:00"
    assert _next_decision("nope", "sim", at) is None  # display only: never raises
