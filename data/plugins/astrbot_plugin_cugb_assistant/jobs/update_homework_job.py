from __future__ import annotations


async def run(chaoxing_service) -> dict:
    return await chaoxing_service.refresh()
