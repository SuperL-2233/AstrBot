from __future__ import annotations

import asyncio
import re
from typing import Any

from astrbot.api import logger

from .jwgl_core import (
    JwglError,
    JwglSettings,
    _is_visible,
    _text,
    is_login_page,
)


class CugbLoginAutomator:
    """CAS 登录步骤拆分：账号密码、请求短信验证码、提交验证码。"""

    def __init__(self, settings: JwglSettings) -> None:
        self.settings = settings

    def _require_credentials(self) -> tuple[str, str]:
        if not self.settings.username or not self.settings.password:
            raise JwglError("需要登录，但插件配置中的学号和密码未填写")
        return self.settings.username, self.settings.password

    async def submit_account_password(self, page: Any) -> None:
        username, password = self._require_credentials()
        logger.info("Submitting the account/password step")
        await page.locator("#double_username").fill(username)
        await page.locator("#double_password").fill(password)
        await page.locator("#enterBtn").click()

        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            if await _is_visible(page, "#J_mobile_Captcha"):
                return
            message = await _text(page, "#double_account_msg")
            if message:
                raise JwglError(f"账号密码登录被拒绝：{message}")
            await asyncio.sleep(0.25)
        raise JwglError("等待短信验证步骤超时")

    async def request_sms_code(self, page: Any) -> None:
        """点击"获取验证码"按钮发送短信；冷却期内先等倒计时结束再点。

        学校限制短信重发间隔（约 60 秒）：冷却中按钮会显示倒计时
        （如 "59秒后重新获取"）或被置灰。盲目点击会被学校拒绝
        （"发送过于频繁"），甚至把登录页重置，导致后续验证码无法提交。
        """
        send_button = page.locator("#doubletelphoneSendCaptcha")
        if await send_button.count() == 0 or not await send_button.is_visible():
            raise JwglError("验证页面上没有可用的短信发送按钮")

        deadline = asyncio.get_running_loop().time() + 75
        while asyncio.get_running_loop().time() < deadline:
            button_value = ((await send_button.get_attribute("value")) or "").strip()
            cooling = re.search(r"\d+", button_value) is not None
            if not cooling and await send_button.is_enabled():
                break
            await asyncio.sleep(1)
        else:
            raise JwglError("短信发送按钮一直处在冷却状态，请稍后重新发起登录")

        logger.info("Requesting an SMS verification code")
        await send_button.click()
        await asyncio.sleep(0.75)
        message = await _text(page, "#J_mobileMsg")
        if message:
            raise JwglError(f"短信验证码请求失败：{message}")

    async def submit_sms_code(self, page: Any, code: str) -> None:
        code = code.strip()
        if not re.fullmatch(r"\d{6}", code):
            raise JwglError("短信验证码必须是 6 位数字")

        logger.info("Submitting the SMS verification step")
        await page.locator("#J_mobile_Captcha").fill(code)
        verify_button = page.locator("#loginButton")
        if await verify_button.count() == 0 or not await verify_button.is_visible():
            raise JwglError(
                "常规的短信验证按钮不可用；该账号可能需要手机确认、绑定或代理登录处理"
            )
        await verify_button.click()

        deadline = asyncio.get_running_loop().time() + 60
        settled_checks = 0
        while asyncio.get_running_loop().time() < deadline:
            if not await is_login_page(page):
                # CAS 跳转链的中间页可能瞬时不像登录页，连续确认两次才算真正离开
                settled_checks += 1
                if settled_checks >= 2:
                    try:
                        await page.wait_for_load_state(
                            "domcontentloaded", timeout=5_000
                        )
                    except Exception:
                        pass
                    return
                await asyncio.sleep(0.5)
                continue
            settled_checks = 0
            message = await _text(page, "#J_mobileMsg")
            if message:
                raise JwglError(f"短信验证被拒绝：{message}")
            await asyncio.sleep(0.25)
        raise JwglError("短信验证后等待 CAS 跳转超时")

    async def needs_account_step(self, page: Any) -> bool:
        return await _is_visible(page, "#double_username")

    async def needs_sms_step(self, page: Any) -> bool:
        return await _is_visible(page, "#J_mobile_Captcha")
