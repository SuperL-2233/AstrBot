from __future__ import annotations

from datetime import datetime

from ..importers.import_timetable import TimetableImporter
from ..repositories.course_repo import CourseRepository
from ..utils.result import make_result
from ..utils.time_utils import today_local, tomorrow_local


class CourseService:
    def __init__(self, repo: CourseRepository, importer: TimetableImporter) -> None:
        self.repo = repo
        self.importer = importer

    def refresh_timetable(self) -> dict:
        parsed = self.importer.import_courses()
        if not parsed["success"]:
            return parsed
        count = self.repo.replace_all(parsed["data"])
        return make_result(
            True,
            data={"count": count},
            message=f"课程表导入完成，共 {count} 条课程。",
            source_status=self.get_status()["data"],
        )

    def get_today_courses(self) -> dict:
        return self.get_courses_by_date(today_local().isoformat(), "今天")

    def get_tomorrow_courses(self) -> dict:
        return self.get_courses_by_date(tomorrow_local().isoformat(), "明天")

    def get_courses_by_date(self, target_date: str, label: str | None = None) -> dict:
        rows = self.repo.get_courses_by_date(target_date)
        if not rows:
            prefix = f"{label}暂无课程数据。" if label else "暂无课程数据。"
            if self.repo.count_courses() == 0:
                prefix = "还没有导入课程表，请先执行 /课程表刷新。"
            return make_result(
                False, data=[], message=prefix, error_code="COURSE_NOT_FOUND"
            )
        return make_result(
            True, data=rows, message=f"已返回 {label or target_date} 的课程。"
        )

    def get_next_course(self) -> dict:
        row = self.repo.get_next_course(datetime.now())
        if not row:
            message = "今天已经没有后续课程了。"
            if self.repo.count_courses() == 0:
                message = "还没有导入课程表，请先执行 /课程表刷新。"
            return make_result(
                False, data=None, message=message, error_code="NEXT_COURSE_NOT_FOUND"
            )
        return make_result(True, data=row, message="已返回下一节课。")

    def get_current_course(self) -> dict:
        row = self.repo.get_current_course(datetime.now())
        if not row:
            message = "当前没有正在进行的课程。"
            if self.repo.count_courses() == 0:
                message = "还没有导入课程表，请先执行 /课程表刷新。"
            return make_result(
                False, data=None, message=message, error_code="CURRENT_COURSE_NOT_FOUND"
            )
        return make_result(True, data=row, message="已返回当前课程。")

    def get_course_weeks(self, course_name: str) -> dict:
        rows = self.repo.get_course_dates(course_name)
        if not rows:
            return make_result(
                False,
                data=[],
                message="没有找到匹配的课程。",
                error_code="COURSE_NOT_FOUND",
            )
        return make_result(True, data=rows, message="已返回课程周次信息。")

    def search_courses(self, keyword: str) -> dict:
        rows = self.repo.search_courses(keyword)
        if not rows:
            return make_result(
                False,
                data=[],
                message="没有找到匹配的课程。",
                error_code="COURSE_NOT_FOUND",
            )
        return make_result(True, data=rows, message="已返回课程搜索结果。")

    def get_status(self) -> dict:
        count = self.repo.count_courses()
        return make_result(
            True,
            data={
                "enabled": True,
                "imported": count > 0,
                "count": count,
            },
            message="已返回课程表状态。",
        )
