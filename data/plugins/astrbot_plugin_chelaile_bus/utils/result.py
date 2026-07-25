from __future__ import annotations

from typing import Any


def make_result(
    success: bool,
    data: Any = None,
    message: str = "",
    error_code: str | None = None,
) -> dict[str, Any]:
    """Build the stable result contract shared by services, commands and LLM tools.

    Args:
        success: Whether the operation succeeded.
        data: Payload on success, or auxiliary data (e.g. candidates) on failure.
        message: Human-readable status or error message.
        error_code: Machine-readable error code on failure.

    Returns:
        A dict with keys success, data, message and error_code.
    """
    return {
        "success": success,
        "data": data,
        "message": message,
        "error_code": error_code,
    }
