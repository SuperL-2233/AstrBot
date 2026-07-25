from __future__ import annotations

import json
from datetime import date

from ..repositories.settings_repo import SettingsRepository
from ..utils.time_utils import parse_date, today_local

RESIDENT_LOCATION_KEY = "resident_location"
TRIP_KEY = "trip"
TARGET_UMO_KEY = "target_umo"
PUSH_ENABLED_KEY = "push_enabled"
RESIDENT_PUSH_TIME_KEY = "resident_push_time"
PUSH_TIME_OVERRIDE_KEY = "push_time_override"


class StateService:
    """Owns plugin state: resident/trip location and resident/override push time.

    Both location and push time follow the same dual-state model: a resident
    value used by default, plus an optional date-bounded override (a "trip"
    for location) that takes precedence inside [start_date, end_date] and is
    dropped lazily once expired.
    """

    def __init__(self, config: dict, repo: SettingsRepository) -> None:
        self.config = config
        self.repo = repo
        self.tz_name = str(config["push"].get("timezone", "Asia/Shanghai"))

    def _load_json(self, key: str) -> dict | None:
        raw = self.repo.get_setting(key)
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _load_windowed(self, key: str) -> dict | None:
        """Load a dated record, dropping it lazily once its window has passed."""
        record = self._load_json(key)
        if not record:
            return None
        end = parse_date(str(record.get("end_date", "")))
        if end is None or end < today_local(self.tz_name):
            self.repo.clear_setting(key)
            return None
        return record

    @staticmethod
    def _window_covers(record: dict | None, day: date) -> bool:
        if not record:
            return False
        start = parse_date(str(record.get("start_date", "")))
        end = parse_date(str(record.get("end_date", "")))
        return bool(start and end and start <= day <= end)

    # Location state
    def get_resident_location(self) -> dict | None:
        return self._load_json(RESIDENT_LOCATION_KEY)

    def set_resident_location(self, place: dict) -> None:
        self.repo.set_setting(
            RESIDENT_LOCATION_KEY, json.dumps(place, ensure_ascii=False)
        )

    def get_trip(self) -> dict | None:
        return self._load_windowed(TRIP_KEY)

    def set_trip(self, place: dict, start: date, end: date) -> None:
        payload = {
            **place,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        self.repo.set_setting(TRIP_KEY, json.dumps(payload, ensure_ascii=False))

    def effective_location(self, day: date) -> dict | None:
        trip = self.get_trip()
        if self._window_covers(trip, day):
            return trip
        return self.get_resident_location()

    # Push time state
    def get_resident_push_time(self) -> str:
        stored = self.repo.get_setting(RESIDENT_PUSH_TIME_KEY)
        if stored:
            return stored
        return str(self.config["push"].get("default_time", "07:50"))

    def set_resident_push_time(self, hhmm: str) -> None:
        self.repo.set_setting(RESIDENT_PUSH_TIME_KEY, hhmm)

    def get_push_time_override(self) -> dict | None:
        return self._load_windowed(PUSH_TIME_OVERRIDE_KEY)

    def set_push_time_override(self, hhmm: str, start: date, end: date) -> None:
        payload = {
            "time": hhmm,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        self.repo.set_setting(
            PUSH_TIME_OVERRIDE_KEY, json.dumps(payload, ensure_ascii=False)
        )

    def effective_push_time(self, day: date) -> str:
        override = self.get_push_time_override()
        if self._window_covers(override, day):
            return str(override["time"])
        return self.get_resident_push_time()

    # Push target and switch
    def get_target_umo(self) -> str:
        configured = str(self.config["push"].get("target_umo", "")).strip()
        if configured:
            return configured
        return self.repo.get_setting(TARGET_UMO_KEY)

    def set_target_umo(self, umo: str) -> None:
        self.repo.set_setting(TARGET_UMO_KEY, umo)

    def push_enabled(self) -> bool:
        raw = self.repo.get_setting(PUSH_ENABLED_KEY)
        if raw == "":
            return bool(self.config["push"].get("enabled", True))
        return raw == "1"

    def set_push_enabled(self, enabled: bool) -> None:
        self.repo.set_setting(PUSH_ENABLED_KEY, "1" if enabled else "0")

    def status(self) -> dict:
        """Snapshot of all user-facing state for status commands."""
        today = today_local(self.tz_name)
        trip = self.get_trip()
        return {
            "resident_location": self.get_resident_location(),
            "trip": trip,
            "trip_active_today": self._window_covers(trip, today),
            "effective_location": self.effective_location(today),
            "resident_push_time": self.get_resident_push_time(),
            "push_time_override": self.get_push_time_override(),
            "effective_push_time": self.effective_push_time(today),
            "push_enabled": self.push_enabled(),
            "target_umo_set": bool(self.get_target_umo()),
        }
