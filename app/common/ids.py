"""Identifiers, timestamps and canonical JSON.

Every persisted byte that feeds a statistic is produced through
``dumps``/``loads`` so that revision hashes and analysis input hashes are
reproducible across processes.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def utcnow() -> str:
    """ISO-8601 UTC with a trailing Z, second resolution is enough for logs."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def dumps(value: Any) -> str:
    """Canonical JSON: sorted keys, no insignificant whitespace, UTF-8 safe."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def loads(raw: str | bytes) -> Any:
    return json.loads(raw)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest(value: Any) -> str:
    return sha256_text(dumps(value))
