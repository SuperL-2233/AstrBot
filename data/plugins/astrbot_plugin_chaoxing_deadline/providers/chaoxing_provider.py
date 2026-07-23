from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime

import requests

from ..repositories.task_repository import TaskRepository
from .vendor import chaoxing_api


class ChaoxingProvider:
    """Synchronize read-only task data through the verified upstream API flow."""

    COMPLETED_MARKERS = ("已完成", "已提交", "待批阅", "已批阅", "已结束", "已截止")
    SOURCE = "SuperL-2233/chaoxing-api@554e01a"

    def __init__(self, config: dict, repository: TaskRepository) -> None:
        self.config = config
        self.repository = repository
        self.last_error = ""
        self.partial_errors: list[str] = []

    async def refresh(self) -> dict:
        phone = str(self.config.get("phone", "")).strip()
        password = str(self.config.get("password", ""))
        if not phone or not password:
            return {
                "success": False,
                "data": None,
                "message": "请先在插件运行时配置中填写学习通手机号和密码。",
                "error_code": "CHAOXING_CREDENTIALS_MISSING",
            }
        try:
            data = await asyncio.to_thread(self._refresh_sync, phone, password)
        except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
            self.last_error = str(exc)
            return {
                "success": False,
                "data": None,
                "message": f"超星同步失败：{exc}",
                "error_code": "CHAOXING_REFRESH_FAILED",
            }
        self.last_error = ""
        return {
            "success": True,
            "data": data,
            "message": (
                f"超星同步完成：{data['course_count']} 门课程、{data['task_count']} 个任务，"
                f"{data['partial_error_count']} 项局部读取提示。"
            ),
            "error_code": None,
        }

    def _refresh_sync(self, phone: str, password: str) -> dict:
        """Run the synchronous upstream requests outside AstrBot's event loop.

        Args:
            phone: Chaoxing login phone number.
            password: Chaoxing password from runtime configuration.

        Returns:
            Synchronization counts suitable for user-facing status.

        Raises:
            RuntimeError: If no course can be processed without a context error.
            requests.RequestException: If login or course-list requests fail.
        """
        timeout = max(5.0, float(self.config.get("timeout_seconds", 20)))
        session = chaoxing_api.create_session()
        chaoxing_api.login(session, phone, password, timeout)
        courses = chaoxing_api.get_courses(session, timeout)
        refreshed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tasks: list[dict] = []
        errors: list[str] = []
        processed_courses = 0

        for course in courses:
            try:
                context, course_page = chaoxing_api.get_course_context(
                    session, course, timeout
                )
            except (
                requests.RequestException,
                RuntimeError,
                ValueError,
                KeyError,
            ) as exc:
                errors.append(f"{course['name']}：课程主页读取失败（{exc}）")
                continue
            processed_courses += 1
            course_items: list[dict] = []
            chapter_page = ""
            chapter_payload: dict = {}

            try:
                try:
                    videos, chapter_payload = chaoxing_api.extract_video_rows(
                        session, context, timeout
                    )
                except (requests.RequestException, ValueError, KeyError):
                    videos, chapter_page = (
                        chaoxing_api.extract_video_rows_from_chapter_page(
                            session, context, timeout
                        )
                    )
                schedule = chaoxing_api.extract_course_schedule(
                    session, context, chapter_payload, timeout
                )
                if not schedule.get("deadline"):
                    _, _, raw_deadline, deadline = (
                        chaoxing_api.extract_start_and_deadline(
                            course_page + " " + chapter_page
                        )
                    )
                    schedule["deadline_raw"] = raw_deadline
                    schedule["deadline"] = deadline
                for item in videos:
                    if not item.get("deadline") and schedule.get("deadline"):
                        item["deadline"] = schedule["deadline"]
                        item["deadline_raw"] = schedule.get("deadline_raw", "")
                        item["note"] = "章节未设置单独截止时间；使用课程截止时间"
                course_items.extend(videos)
            except (
                requests.RequestException,
                RuntimeError,
                ValueError,
                KeyError,
            ) as exc:
                errors.append(f"{course['name']}：视频章节读取失败（{exc}）")

            try:
                homeworks, note = chaoxing_api.extract_work_rows(
                    session, context, course_page, timeout, detail_fallback=True
                )
                course_items.extend(homeworks)
                if note:
                    errors.append(f"{course['name']}：{note}")
            except (
                requests.RequestException,
                RuntimeError,
                ValueError,
                KeyError,
            ) as exc:
                errors.append(f"{course['name']}：作业读取失败（{exc}）")

            try:
                discussions, note = chaoxing_api.extract_discussion_rows(
                    session, context, timeout
                )
                course_items.extend(discussions)
                if note:
                    errors.append(f"{course['name']}：{note}")
            except (
                requests.RequestException,
                RuntimeError,
                ValueError,
                KeyError,
            ) as exc:
                errors.append(f"{course['name']}：讨论读取失败（{exc}）")

            for item in course_items:
                raw_key = "|".join(
                    (
                        course["courseId"],
                        course["classId"],
                        str(item.get("type", "")),
                        str(item.get("item_id") or ""),
                        str(item.get("name") or ""),
                        str(item.get("url") or ""),
                    )
                )
                status_label = str(item.get("status", ""))
                tasks.append(
                    {
                        "task_key": hashlib.sha256(raw_key.encode()).hexdigest(),
                        "course_name": course["name"],
                        "course_id": course["courseId"],
                        "class_id": course["classId"],
                        "task_type": str(item.get("type", "任务")),
                        "title": str(item.get("name", "未命名任务")),
                        "status": "completed"
                        if any(
                            marker in status_label for marker in self.COMPLETED_MARKERS
                        )
                        else "pending",
                        "start_time": item.get("start") or "",
                        "deadline": item.get("deadline") or "",
                        "source_update_time": refreshed_at,
                        "url": item.get("url") or "",
                        "note": item.get("note") or "",
                        "source": self.SOURCE,
                    }
                )

        if not processed_courses:
            raise RuntimeError("课程列表存在，但所有课程主页均读取失败；保留原缓存")
        self.repository.replace_tasks(tasks)
        self.partial_errors = errors[-20:]
        return {
            "course_count": len(courses),
            "task_count": len(tasks),
            "partial_error_count": len(errors),
        }

    def status(self) -> dict:
        status = self.repository.status()
        status.update(
            {
                "enabled": bool(self.config.get("enabled", False)),
                "credentials_configured": bool(
                    str(self.config.get("phone", "")).strip()
                    and str(self.config.get("password", ""))
                ),
                "last_error": self.last_error,
                "partial_error_count": len(self.partial_errors),
                "source": self.SOURCE,
            }
        )
        return status
