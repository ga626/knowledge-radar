# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：  
# 1. 不得用于任何商业用途。  
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。  
# 3. 不得进行大规模爬取或对平台造成运营干扰。  
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。   
# 5. 不得用于任何非法或不当的用途。
#   
# 详细许可条款请参阅项目根目录下的LICENSE文件。  
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。  


# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2023/12/2 18:44
# @Desc    : bilibli登录实现类

import asyncio
import functools
import sys
from typing import Optional

from playwright.async_api import BrowserContext, Page
from tenacity import (RetryError, retry, retry_if_result, stop_after_attempt,
                      wait_fixed)

import config
from base.base_crawler import AbstractLogin
from tools import utils


class BilibiliLogin(AbstractLogin):
    def __init__(self,
                 login_type: str,
                 browser_context: BrowserContext,
                 context_page: Page,
                 login_phone: Optional[str] = "",
                 cookie_str: str = ""
                 ):
        config.LOGIN_TYPE = login_type
        self.browser_context = browser_context
        self.context_page = context_page
        self.login_phone = login_phone
        self.cookie_str = cookie_str

    async def begin(self):
        """Start login bilibili"""
        utils.logger.info("[BilibiliLogin.begin] Begin login Bilibili ...")
        if config.LOGIN_TYPE == "qrcode":
            await self.login_by_qrcode()
        elif config.LOGIN_TYPE == "phone":
            await self.login_by_mobile()
        elif config.LOGIN_TYPE == "cookie":
            await self.login_by_cookies()
        else:
            raise ValueError(
                "[BilibiliLogin.begin] Invalid Login Type Currently only supported qrcode or phone or cookie ...")

    @retry(stop=stop_after_attempt(600), wait=wait_fixed(1), retry=retry_if_result(lambda value: value is False))
    async def check_login_state(self) -> bool:
        """
            Check if the current login status is successful and return True otherwise return False
            retry decorator will retry 20 times if the return value is False, and the retry interval is 1 second
            if max retry times reached, raise RetryError
        """
        current_cookie = await self.browser_context.cookies()
        _, cookie_dict = utils.convert_cookies(current_cookie)
        if cookie_dict.get("SESSDATA", "") or cookie_dict.get("DedeUserID"):
            return True
        return False

    async def _try_click_login_button_bili(self) -> bool:
        """Try multiple strategies to find and click the Bilibili login button."""
        strategies = [
            ("xpath=//div[contains(@class,'go-login-btn')]", "class go-login-btn"),
            ("text=登录", "text=登录"),
            ("button:has-text('登录')", "button has-text"),
            ("xpath=//*[contains(@class,'login')]//div[contains(text(),'登录')]", "login div text"),
            ("xpath=//div[@class='right-entry__outside go-login-btn']//div", "original xpath"),
        ]
        for selector, name in strategies:
            try:
                btn = self.context_page.locator(selector).first
                if await btn.count() > 0:
                    await btn.click(timeout=5000)
                    utils.logger.info(f"[BilibiliLogin] Clicked login button via: {name}")
                    return True
            except Exception:
                continue
        utils.logger.warning("[BilibiliLogin] All login button strategies failed")
        return False

    async def _try_find_qrcode_bili(self) -> str:
        """Try multiple selectors to find bilibili QR code. Short timeouts."""
        import base64, httpx
        from tools.crawler_util import get_user_agent
        qr_selectors = [
            "//div[contains(@class,'login-scan')]//img",
            "//div[@class='login-scan-box']//img",
            "xpath=//img[contains(@class,'qrcode')]",
        ]
        for selector in qr_selectors:
            try:
                elements = await self.context_page.wait_for_selector(selector, timeout=3000)
                if elements:
                    login_qrcode_img = str(await elements.get_property("src"))
                    if "http://" in login_qrcode_img or "https://" in login_qrcode_img:
                        async with httpx.AsyncClient(follow_redirects=True) as client:
                            resp = await client.get(login_qrcode_img, headers={"User-Agent": get_user_agent()})
                            if resp.status_code == 200:
                                b64 = base64.b64encode(resp.content).decode('utf-8')
                                if b64:
                                    return b64
                    elif login_qrcode_img and login_qrcode_img.startswith("data:image"):
                        return login_qrcode_img
            except Exception:
                continue
        return ""

    async def login_by_qrcode(self):
        """login bilibili website and keep webdriver login state"""
        utils.logger.info("[BilibiliLogin.login_by_qrcode] Begin login bilibili by qrcode ...")

        # try to find qrcode first (in case login dialog already popped up)
        base64_qrcode_img = await self._try_find_qrcode_bili()
        if not base64_qrcode_img:
            # click login button with multiple strategies
            await self._try_click_login_button_bili()
            await asyncio.sleep(1)
            base64_qrcode_img = await self._try_find_qrcode_bili()

        if not base64_qrcode_img:
            utils.logger.info("[BilibiliLogin.login_by_qrcode] login failed , have not found qrcode please check ....")
            sys.exit()

        # show login qrcode
        partial_show_qrcode = functools.partial(utils.show_qrcode, base64_qrcode_img)
        asyncio.get_running_loop().run_in_executor(executor=None, func=partial_show_qrcode)

        utils.logger.info(f"[BilibiliLogin.login_by_qrcode] Waiting for scan code login, remaining time is 20s")
        try:
            await self.check_login_state()
        except RetryError:
            utils.logger.info("[BilibiliLogin.login_by_qrcode] Login bilibili failed by qrcode login method ...")
            sys.exit()

        wait_redirect_seconds = 5
        utils.logger.info(
            f"[BilibiliLogin.login_by_qrcode] Login successful then wait for {wait_redirect_seconds} seconds redirect ...")
        await asyncio.sleep(wait_redirect_seconds)

    async def login_by_mobile(self):
        pass

    async def login_by_cookies(self):
        utils.logger.info("[BilibiliLogin.login_by_qrcode] Begin login bilibili by cookie ...")
        for key, value in utils.convert_str_cookie_to_dict(self.cookie_str).items():
            await self.browser_context.add_cookies([{
                'name': key,
                'value': value,
                'domain': ".bilibili.com",
                'path': "/"
            }])
