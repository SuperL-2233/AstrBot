from __future__ import annotations


async def run(classroom_service) -> dict:
    return await classroom_service.refresh()
