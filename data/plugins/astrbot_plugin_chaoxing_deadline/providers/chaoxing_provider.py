from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta

import requests

from ..repositories.task_repository import TaskRepository
from .vendor import chaoxing_api


class ChaoxingAntiSpiderError(RuntimeError):
    """Chaoxing rejected the scripted session before course parsing."""


class ChaoxingProvider:
    """Synchronize read-only task data through the verified upstream API flow."""

    COMPLETED_MARKERS = ("已完成", "已提交", "待批阅", "已批阅")
    SOURCE = "SuperL-2233/chaoxing-api@554e01a"

    def __init__(self, config: dict, repository: TaskRepository) -> None:
        self.config = config
        self.repository = repository
        self.last_error = ""
        self.partial_errors: list[str] = []
        self._session: requests.Session | None = None
        self._lease_owner = uuid.uuid4().hex
        self._worker_task: asyncio.Task | None = None

    @staticmethod
    def _is_antispider_error(exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        response_url = str(getattr(response, "url", ""))
        return "antispiderShowMsg" in response_url or "antispiderShowMsg" in str(exc)

    @classmethod
    def _raise_if_antispider(cls, exc: Exception) -> None:
        if cls._is_antispider_error(exc):
            raise ChaoxingAntiSpiderError(
                "超星返回 403 反爬提示：我们发现您的访问有异常，请联系客服"
            ) from exc

    def _record_antispider_backoff(self, exc: Exception) -> ChaoxingAntiSpiderError:
        error = (
            exc
            if isinstance(exc, ChaoxingAntiSpiderError)
            else ChaoxingAntiSpiderError(
                "超星返回 403 反爬提示：我们发现您的访问有异常，请联系客服"
            )
        )
        self._session = None
        self.last_error = str(error)
        cooldown_hours = max(1, int(self.config.get("antispider_cooldown_hours", 12)))
        self.repository.set_sync_backoff(
            datetime.now() + timedelta(hours=cooldown_hours), self.last_error
        )
        return error

    async def _run_leased_worker(self, phone: str, password: str) -> dict:
        try:
            return await asyncio.to_thread(self._refresh_sync, phone, password)
        except ChaoxingAntiSpiderError as exc:
            raise self._record_antispider_backoff(exc)
        except requests.RequestException as exc:
            if self._is_antispider_error(exc):
                raise self._record_antispider_backoff(exc) from exc
            raise
        finally:
            self.repository.release_sync_lease(self._lease_owner)

    async def wait_for_worker(self) -> None:
        """Wait for a detached sync worker before a plugin instance is unloaded."""
        worker = self._worker_task
        if worker:
            try:
                await worker
            except (
                asyncio.CancelledError,
                requests.RequestException,
                RuntimeError,
                ValueError,
                KeyError,
            ):
                pass
        if self._worker_task is worker:
            self._worker_task = None

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
        if self._worker_task and not self._worker_task.done():
            return {
                "success": False,
                "data": None,
                "message": "当前插件实例已有超星同步正在运行。",
                "error_code": "CHAOXING_SYNC_IN_PROGRESS",
            }
        guard = self.repository.sync_guard_state()
        if guard["backoff_active"]:
            self.last_error = guard["backoff_reason"]
            return {
                "success": False,
                "data": None,
                "message": (
                    f"超星同步处于反爬冷却期，{guard['backoff_until']} 前不会访问云端。"
                    f"原因：{guard['backoff_reason']}"
                ),
                "error_code": "CHAOXING_ANTISPIDER_BACKOFF",
            }
        lease_minutes = max(5, int(self.config.get("sync_lease_minutes", 30)))
        if not self.repository.claim_sync_lease(self._lease_owner, lease_minutes):
            guard = self.repository.sync_guard_state()
            return {
                "success": False,
                "data": None,
                "message": f"已有超星同步正在运行，锁定至 {guard['lease_until']}。",
                "error_code": "CHAOXING_SYNC_IN_PROGRESS",
            }
        try:
            self._worker_task = asyncio.create_task(
                self._run_leased_worker(phone, password)
            )
            data = await asyncio.shield(self._worker_task)
        except ChaoxingAntiSpiderError as exc:
            cooldown_hours = max(
                1, int(self.config.get("antispider_cooldown_hours", 12))
            )
            return {
                "success": False,
                "data": None,
                "message": (f"超星同步已停止并进入 {cooldown_hours} 小时冷却期：{exc}"),
                "error_code": "CHAOXING_ANTISPIDER_DETECTED",
            }
        except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
            self.last_error = str(exc)
            return {
                "success": False,
                "data": None,
                "message": f"超星同步失败：{exc}",
                "error_code": "CHAOXING_REFRESH_FAILED",
            }
        finally:
            if self._worker_task and self._worker_task.done():
                self._worker_task = None
        self.last_error = ""
        self.repository.clear_sync_backoff()
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
        session = self._session
        if session is None:
            session = chaoxing_api.create_session()
            chaoxing_api.login(session, phone, password, timeout)
            self._session = session
        try:
            courses = chaoxing_api.get_courses(session, timeout)
        except RuntimeError as exc:
            if "Cookie" not in str(exc) and "登录页" not in str(exc):
                raise
            session = chaoxing_api.create_session()
            chaoxing_api.login(session, phone, password, timeout)
            self._session = session
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
                self._raise_if_antispider(exc)
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
                self._raise_if_antispider(exc)
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
                self._raise_if_antispider(exc)
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
                self._raise_if_antispider(exc)
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
            self.partial_errors = errors[-20:]
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
        sync_guard = self.repository.sync_guard_state()
        status.update(
            {
                "enabled": bool(self.config.get("enabled", False)),
                "credentials_configured": bool(
                    str(self.config.get("phone", "")).strip()
                    and str(self.config.get("password", ""))
                ),
                "last_error": self.last_error or sync_guard["backoff_reason"],
                "partial_error_count": len(self.partial_errors),
                "source": self.SOURCE,
                "sync_guard": sync_guard,
            }
        )
        return status
