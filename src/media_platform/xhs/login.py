# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：  
# 1. 不得用于任何商业用途。  
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。  
# 3. 不得进行大规模爬取或对平台造成运营干扰。  
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。   
# 5. 不得用于任何非法或不当的用途。
#   
# 详细许可条款请参阅项目根目录下的LICENSE文件。  
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。  


import asyncio
import functools
import sys
from typing import Optional

from playwright.async_api import BrowserContext, Page
from tenacity import (RetryError, retry, retry_if_result, stop_after_attempt,
                      wait_fixed)

import config
from base.base_crawler import AbstractLogin
from cache.cache_factory import CacheFactory
from tools import utils


class XiaoHongShuLogin(AbstractLogin):

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

    @retry(stop=stop_after_attempt(600), wait=wait_fixed(1), retry=retry_if_result(lambda value: value is False))
    async def check_login_state(self, no_logged_in_session: str) -> bool:
        """
            Check if the current login status is successful and return True otherwise return False
            retry decorator will retry 20 times if the return value is False, and the retry interval is 1 second
            if max retry times reached, raise RetryError
        """

        if "请通过验证" in await self.context_page.content():
            utils.logger.info("[XiaoHongShuLogin.check_login_state] 登录过程中出现验证码，请手动验证")

        current_cookie = await self.browser_context.cookies()
        _, cookie_dict = utils.convert_cookies(current_cookie)
        current_web_session = cookie_dict.get("web_session")
        if current_web_session != no_logged_in_session:
            return True
        return False

    async def begin(self):
        """Start login xiaohongshu"""
        utils.logger.info("[XiaoHongShuLogin.begin] Begin login xiaohongshu ...")
        if config.LOGIN_TYPE == "qrcode":
            await self.login_by_qrcode()
        elif config.LOGIN_TYPE == "phone":
            await self.login_by_mobile()
        elif config.LOGIN_TYPE == "cookie":
            await self.login_by_cookies()
        else:
            raise ValueError("[XiaoHongShuLogin.begin]I nvalid Login Type Currently only supported qrcode or phone or cookies ...")

    async def login_by_mobile(self):
        """Login xiaohongshu by mobile"""
        utils.logger.info("[XiaoHongShuLogin.login_by_mobile] Begin login xiaohongshu by mobile ...")
        await asyncio.sleep(1)
        try:
            # 小红书进入首页后，有可能不会自动弹出登录框，需要手动点击登录按钮
            await self._try_click_login_button()
            # 弹窗的登录对话框也有两种形态，一种是直接可以看到手机号和验证码的
            # 另一种是需要点击切换到手机登录的
            element = await self.context_page.wait_for_selector(
                selector='xpath=//div[@class="login-container"]//div[@class="other-method"]/div[1]',
                timeout=5000
            )
            await element.click()
        except Exception as e:
            utils.logger.info("[XiaoHongShuLogin.login_by_mobile] have not found mobile button icon and keep going ...")

        await asyncio.sleep(1)
        login_container_ele = await self.context_page.wait_for_selector("div.login-container")
        input_ele = await login_container_ele.query_selector("label.phone > input")
        await input_ele.fill(self.login_phone)
        await asyncio.sleep(0.5)

        send_btn_ele = await login_container_ele.query_selector("label.auth-code > span")
        await send_btn_ele.click()  # 点击发送验证码
        sms_code_input_ele = await login_container_ele.query_selector("label.auth-code > input")
        submit_btn_ele = await login_container_ele.query_selector("div.input-container > button")
        cache_client = CacheFactory.create_cache(config.CACHE_TYPE_MEMORY)
        max_get_sms_code_time = 60 * 2  # 最长获取验证码的时间为2分钟
        no_logged_in_session = ""
        while max_get_sms_code_time > 0:
            utils.logger.info(f"[XiaoHongShuLogin.login_by_mobile] get sms code from redis remaining time {max_get_sms_code_time}s ...")
            await asyncio.sleep(1)
            sms_code_key = f"xhs_{self.login_phone}"
            sms_code_value = cache_client.get(sms_code_key)
            if not sms_code_value:
                max_get_sms_code_time -= 1
                continue

            current_cookie = await self.browser_context.cookies()
            _, cookie_dict = utils.convert_cookies(current_cookie)
            no_logged_in_session = cookie_dict.get("web_session")

            await sms_code_input_ele.fill(value=sms_code_value.decode())  # 输入短信验证码
            await asyncio.sleep(0.5)
            agree_privacy_ele = self.context_page.locator("xpath=//div[@class='agreements']//*[local-name()='svg']")
            await agree_privacy_ele.click()  # 点击同意隐私协议
            await asyncio.sleep(0.5)

            await submit_btn_ele.click()  # 点击登录

            # todo ... 应该还需要检查验证码的正确性有可能输入的验证码不正确
            break

        try:
            await self.check_login_state(no_logged_in_session)
        except RetryError:
            utils.logger.info("[XiaoHongShuLogin.login_by_mobile] Login xiaohongshu failed by mobile login method ...")
            sys.exit()

        wait_redirect_seconds = 5
        utils.logger.info(f"[XiaoHongShuLogin.login_by_mobile] Login successful then wait for {wait_redirect_seconds} seconds redirect ...")
        await asyncio.sleep(wait_redirect_seconds)

    async def _try_click_login_button(self) -> bool:
        """Try multiple strategies to find and click the login button on Xiaohongshu.
        Based on real page analysis (2026-05-11):
        - Header login button: button.reds-button-new.login-btn (visible, text='登录')
        - There is also button.submit in the phone-login form (NOT what we want)
        Returns True if a button was successfully clicked, False otherwise."""
        strategies = [
            # Strategy 1 (best match from real page): login-btn class in header
            ("xpath=//button[contains(@class, 'login-btn')]", "login-btn class"),
            # Strategy 2: text-based
            ("text=登录", "text=登录"),
            ("button:has-text('登录')", "button:has-text"),
            ("xpath=//button[contains(text(),'登录')]", "xpath contains text"),
            # Strategy 5: broad login class match
            ("xpath=//*[contains(@class,'login')]//button", "class contains login"),
        ]
        for selector, name in strategies:
            try:
                btn = self.context_page.locator(selector).first
                if await btn.count() > 0:
                    await btn.click(timeout=5000)
                    utils.logger.info(f"[XiaoHongShuLogin] Clicked login button via: {name}")
                    return True
            except Exception:
                continue
        utils.logger.warning("[XiaoHongShuLogin] All login button strategies failed")
        return False

    async def _try_find_qrcode(self) -> str:
        """Try multiple selectors to find the QR code image, returns base64 string or empty.
        Based on real page analysis (2026-05-11):
        - QR code image has class='qrcode-img', data:image/png base64 src, visible at (696,480)
        Uses short timeouts to avoid 30s blocking per selector."""
        import base64, httpx
        from tools.crawler_util import get_user_agent
        qr_selectors = [
            "xpath=//img[@class='qrcode-img']",
            "xpath=//img[contains(@class,'qrcode')]",
            "xpath=//div[contains(@class,'qrcode')]//img",
            "xpath=//*[contains(@class,'qrcode')]//img",
        ]
        for selector in qr_selectors:
            try:
                elements = await self.context_page.wait_for_selector(selector, timeout=5000)
                if elements:
                    login_qrcode_img = str(await elements.get_property("src"))
                    # data:image URIs are returned directly (already base64)
                    if login_qrcode_img and login_qrcode_img.startswith("data:image"):
                        utils.logger.info(f"[XiaoHongShuLogin] Found QR code via: {selector}")
                        return login_qrcode_img
                    # External URLs need to be downloaded and base64-encoded
                    if "http://" in login_qrcode_img or "https://" in login_qrcode_img:
                        async with httpx.AsyncClient(follow_redirects=True) as client:
                            resp = await client.get(login_qrcode_img, headers={"User-Agent": get_user_agent()})
                            if resp.status_code == 200:
                                b64 = base64.b64encode(resp.content).decode('utf-8')
                                if b64:
                                    utils.logger.info(f"[XiaoHongShuLogin] Found QR image URL via: {selector}")
                                    return b64
            except Exception:
                continue
        return ""

    async def login_by_qrcode(self):
        """login xiaohongshu website and keep webdriver login state"""
        utils.logger.info("[XiaoHongShuLogin.login_by_qrcode] Begin login xiaohongshu by qrcode ...")

        # 快速检测：如果页面已登录（没有登录弹窗、有频道列表），跳过扫码
        login_modal_visible = await self.context_page.query_selector(".login-modal")
        if not login_modal_visible:
            cookies = await self.browser_context.cookies()
            _, cookie_dict = utils.convert_cookies(cookies)
            if "web_session" in cookie_dict and "id_token" in cookie_dict:
                utils.logger.info("[XiaoHongShuLogin] Page shows no login modal, cookies exist - already logged in")
                return

        # Step 1: wait for page to load and try to find QR code (with retries)
        base64_qrcode_img = ""
        for attempt in range(3):
            await asyncio.sleep(3)
            base64_qrcode_img = await self._try_find_qrcode()
            if base64_qrcode_img:
                utils.logger.info(f"[XiaoHongShuLogin] Found QR code on attempt {attempt + 1}")
                break

            utils.logger.info(f"[XiaoHongShuLogin.login_by_qrcode] Attempt {attempt + 1}: QR not visible, clicking login button...")
            clicked = await self._try_click_login_button()
            if clicked:
                await asyncio.sleep(3)
                base64_qrcode_img = await self._try_find_qrcode()
                if base64_qrcode_img:
                    break

        # Step 2: if still no QR after retries, start manual polling
        if not base64_qrcode_img:
            utils.logger.warning("[XiaoHongShuLogin.login_by_qrcode] ⚠️ 请在浏览器页面中手动点击【登录】按钮打开登录弹窗")
            print("\n⚠️ 未检测到登录弹窗，请在浏览器页面上手动点击【登录】按钮")
            print("   等待最长60秒...")
            for i in range(60):
                await asyncio.sleep(1)
                base64_qrcode_img = await self._try_find_qrcode()
                if base64_qrcode_img:
                    utils.logger.info(f"[XiaoHongShuLogin] Manual - QR code found after {i+1}s")
                    break
            if not base64_qrcode_img:
                utils.logger.error("[XiaoHongShuLogin.login_by_qrcode] 手动等待超时，仍未找到二维码。")
                print("❌ 超时：60秒内未检测到登录弹窗，请确认页面已加载完成")
                sys.exit()

        # get not logged session
        current_cookie = await self.browser_context.cookies()
        _, cookie_dict = utils.convert_cookies(current_cookie)
        no_logged_in_session = cookie_dict.get("web_session")

        # show login qrcode
        # fix issue #12
        # we need to use partial function to call show_qrcode function and run in executor
        # then current asyncio event loop will not be blocked
        partial_show_qrcode = functools.partial(utils.show_qrcode, base64_qrcode_img)
        asyncio.get_running_loop().run_in_executor(executor=None, func=partial_show_qrcode)

        utils.logger.info(f"[XiaoHongShuLogin.login_by_qrcode] waiting for scan code login, remaining time is 120s")
        try:
            await self.check_login_state(no_logged_in_session)
        except RetryError:
            utils.logger.info("[XiaoHongShuLogin.login_by_qrcode] Login xiaohongshu failed by qrcode login method ...")
            sys.exit()

        wait_redirect_seconds = 5
        utils.logger.info(f"[XiaoHongShuLogin.login_by_qrcode] Login successful then wait for {wait_redirect_seconds} seconds redirect ...")
        await asyncio.sleep(wait_redirect_seconds)

    async def login_by_cookies(self):
        """login xiaohongshu website by cookies"""
        utils.logger.info("[XiaoHongShuLogin.login_by_cookies] Begin login xiaohongshu by cookie ...")
        for key, value in utils.convert_str_cookie_to_dict(self.cookie_str).items():
            if key != "web_session":  # only set web_session cookie attr
                continue
            await self.browser_context.add_cookies([{
                'name': key,
                'value': value,
                'domain': ".xiaohongshu.com",
                'path': "/"
            }])
