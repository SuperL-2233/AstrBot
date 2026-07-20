from __future__ import annotations


async def run(reminder_service) -> dict:
    return {
        "success": False,
        "message": reminder_service.get_status()["message"],
    }
