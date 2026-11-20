"""Venue-safe client order ids.

Our ids are deterministic and readable (design rule 6):
``crypto-portfolio-20261120T120000Z-BTCUSDT-0`` — about 50 characters.
Venues are stricter:

- Binance ``newClientOrderId``: ``^[.A-Z:/a-z0-9_-]{1,36}$`` (36 chars max).
- Kraken ``cl_ord_id``: a UUID (with or without dashes) or up to 18
  characters of free text.
- IBKR ``orderRef``: free text, so the full id is used as-is.

A **UUIDv5 of the full id** satisfies Binance and Kraken at once and is
still deterministic: the same logical order always maps to the same
venue id, across restarts and machines, which is the whole point —
after a crash we can ask the venue "do you have order X?" without
having persisted anything.
"""

from __future__ import annotations

import uuid

# Fixed namespace for this project. Changing it would orphan every
# in-flight order on a restart, so it never changes.
KESTREL_NAMESPACE = uuid.UUID("6b1d7e3c-5f0a-4c8e-9a51-2f3b8c4d7e90")


def venue_client_id(client_order_id: str) -> str:
    return str(uuid.uuid5(KESTREL_NAMESPACE, client_order_id))


def attempt_client_id(base_client_order_id: str, attempt: int) -> str:
    """Re-quotes of the same decision get a new id, ``...-{seq}`` with
    seq = attempt number (spec: ``{sleeve}-{strategy}-{ts}-{symbol}-{seq}``).
    The base id produced by the order manager ends in ``-0``."""
    stem, _, _ = base_client_order_id.rpartition("-")
    return f"{stem}-{attempt}"
