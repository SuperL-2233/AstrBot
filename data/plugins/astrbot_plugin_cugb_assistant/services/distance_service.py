from __future__ import annotations


BUILDING_PRIORITY = {
    "教三": 100,
    "教一": 90,
    "教二": 85,
}


class DistanceService:
    def get_building_priority(self, building: str) -> int:
        return BUILDING_PRIORITY.get(building, 50)

    def recommend(self, classrooms: list[dict], next_course_location: str = "") -> list[dict]:
        scored = []
        next_building = next_course_location[:2] if next_course_location else ""
        for room in classrooms:
            score = self.get_building_priority(room["building"])
            if next_building and room["building"] == next_building:
                score += 30
                reason = f"与下节课地点 {next_course_location} 同楼，移动成本低。"
            else:
                reason = f"{room['building']} 优先级较高，适合作为自习候选。"
            enriched = dict(room)
            enriched["priority"] = score
            enriched["reason"] = reason
            scored.append(enriched)
        return sorted(scored, key=lambda item: (-item["priority"], item["building"], item["room"]))
