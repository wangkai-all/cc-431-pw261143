"""Validation-error responses that stay JSON-safe.

FastAPI's default handler echoes the offending input back. If that input was a
non-finite float (``Infinity``/``NaN``) the echo itself is not valid JSON and the
request becomes a 500 instead of a 422. This handler sanitises the echo.
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": _json_safe(exc.errors())})


def install(app) -> None:
    app.add_exception_handler(RequestValidationError, validation_error_handler)
