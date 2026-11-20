"""Phase 7 go-live gates (spec Section 13).

Phase 4 state: ``live`` mode is **locked**. The Kraken and IBKR-live
adapters exist and pass the contract suite against mocked responses,
but no code path can construct them: ``build_runtime`` calls
``require_live_unlocked`` first, and it always raises until Phase 7
replaces it with the real checklist.
"""

from __future__ import annotations


class LiveModeLocked(RuntimeError):
    pass


def require_live_unlocked(*_: object, **__: object) -> None:
    raise LiveModeLocked(
        "live mode is disabled: Phase 7 (spec Section 13 go-live gates) has not been unlocked"
    )
