from __future__ import annotations

from typing import Any


def make_result(
    success: bool,
    data: Any = None,
    message: str = "",
    error_code: str | None = None,
    source_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": success,
        "data": data,
        "message": message,
        "error_code": error_code,
        "source_status": source_status or {},
    }
