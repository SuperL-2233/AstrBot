from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

from astrbot.api import logger

from ..core.jwgl_core import JwglError, safe_exception_name
from .poll_service import PollService, PushFunc
from .session_service import JwglSessionService

TargetFunc = Callable[[], str]


class ConsentService:
    """登录编排：发起登录、提交验证码。验证码经 LLM 工具或指令传入，不截获消息。"""

    def __init__(
        self,
        config: dict[str, Any],
        session: JwglSessionService,
        poll_service: PollService,
        push: PushFunc,
        get_target_umo: TargetFunc,
    ) -> None:
        self.config = config
        self.session = session
        self.poll_service = poll_service
        self.push = push
        self.get_target_umo = get_target_umo

    async def request_login(self, resend: bool = False) -> str:
        """发起登录：同步执行到学校短信验证码发出为止，返回真实结果或失败原因。

        Args:
            resend: 已有等待中的流程且用户明确说没收到短信时为 True，
                会等学校重发冷却结束后重新发送验证码。
        """
        if not self.session.portal_configured:
            return "尚未配置 portal_url，请先在插件配置中填写 WebVPN 门户地址。"
        if not self.get_target_umo():
            return "尚未绑定推送目标，请先在任意会话发送 /教务绑定。"
        try:
            stage = await self.session.begin_login(resend=resend)
        except JwglError as error:
            return f"教务系统登录发起失败：{error}"
        except Exception as error:
            logger.exception("教务系统登录发起异常")
            return f"教务系统登录发起异常（{safe_exception_name(error)}）。"
        if stage == "reuse":
            self.poll_service.reset_expiry_notification()
            self.poll_service.start()
            return "教务系统会话仍然有效，无需重新登录。"
        if stage == "sms_resent":
            return (
                "已重新发送短信验证码（学校限制 60 秒重发间隔，必要时已自动等待"
                "冷却结束）。请告诉用户：以最新收到的一条短信为准，把 6 位验证码"
                "回复到本对话即可。"
            )
        if stage == "sms_pending":
            if self.session.login_stage == "mportal":
                return (
                    "登录流程已在进行中：教务门户验证已通过，学校已为 mportal "
                    "结构化课表登录重新发送了 6 位短信验证码到用户手机。"
                    "请直接把手机收到的最新验证码回复到本对话（或调用 "
                    "jwgl_submit_sms_code 提交），无需重新发起登录；"
                    "若用户明确说没收到短信，请以 resend=true 再次调用本工具重发。"
                )
            return (
                "登录流程已在进行中：学校之前已把 6 位短信验证码发送到用户手机，"
                "当前正在等待验证码。请直接把手机收到的验证码回复到本对话（或调用 "
                "jwgl_submit_sms_code 提交），无需重复发起登录；"
                "若用户明确说没收到短信，请以 resend=true 再次调用本工具重发，"
                "学校限制 60 秒重发间隔，会自动等待冷却结束后发出。"
            )
        return (
            "登录流程已发起成功：学校统一认证平台已经通过短信把 6 位验证码发送到"
            "用户绑定的手机上（机器人不发送、不转发验证码）。请告诉用户：查收手机短信，"
            "把 6 位验证码回复到本对话。当用户回复验证码后，调用 jwgl_submit_sms_code "
            "工具（或让用户发送 /教务验证码 <6位数字>）提交即可完成登录。"
        )

    async def submit_sms_code(self, code: str) -> dict[str, Any]:
        """把用户回复的学校短信验证码提交给统一认证，完成登录。"""
        code = (code or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            return {"success": False, "message": "验证码必须是 6 位数字。"}
        if not self.session.login_pending:
            return {
                "success": False,
                "message": "当前没有等待验证码的登录流程，请先发起登录。",
            }
        try:
            stage = await self.session.complete_login(code)
        except JwglError as error:
            return {"success": False, "message": f"教务系统登录失败：{error}"}
        except Exception as error:
            logger.exception("教务系统登录提交异常")
            detail = str(error).strip().replace("\n", " ")[:100]
            suffix = f"：{detail}" if detail else ""
            return {
                "success": False,
                "message": (
                    f"教务系统登录异常（{safe_exception_name(error)}{suffix}）。"
                    "若再次提交仍失败，请重新发起登录。"
                ),
            }
        if stage == "mportal_sms_pending":
            return {
                "success": True,
                "message": (
                    "教务门户已登录成功。mportal 结构化课表还需要一次短信验证："
                    "学校已重新发送 6 位验证码到你的手机，请再回复一次验证码完成最后一步。"
                ),
            }
        self.poll_service.reset_expiry_notification()
        await self.push("教务系统登录成功，将立即执行一次数据检查并开始定时轮询。")
        self.poll_service.start()
        asyncio.create_task(self.poll_service.poll_now(trigger="post-login"))
        return {
            "success": True,
            "message": "教务系统登录成功，定时轮询已启动，新数据会自动推送。",
        }
