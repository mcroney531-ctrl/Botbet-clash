"""Central id generation so it's trivial to swap for a deterministic
sequence in tests without touching every call site."""

from __future__ import annotations

import uuid


def new_id() -> str:
    return str(uuid.uuid4())
