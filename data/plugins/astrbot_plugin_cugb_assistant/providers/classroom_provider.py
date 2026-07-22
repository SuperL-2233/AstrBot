from __future__ import annotations

from typing import Protocol


class ClassroomProvider(Protocol):
    async def get_current_empty_classrooms(self, building: str = "") -> dict: ...

    async def get_empty_classrooms_by_time(
        self,
        building: str = "",
        start_time: str = "",
        end_time: str = "",
    ) -> dict: ...

    async def refresh(self) -> dict: ...

    async def get_status(self) -> dict: ...
