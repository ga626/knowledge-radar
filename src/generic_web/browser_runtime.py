"""同一内核内复用自有浏览器；每个请求独立匿名 context，取消不关闭共享进程。"""
import atexit
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager, contextmanager
import threading
import time

_LOCK = threading.Lock()
_LOOP = None
_THREAD = None
_PLAYWRIGHT = None
_BROWSER = None
_SLOTS = None
_LAUNCH = None


def launch_options():
    from runtime.executables import resolve_managed_chrome
    from runtime.chrome_manager import CHROMIUM_MODEL_POLICY_ARGS
    options = {'headless': True, 'args': CHROMIUM_MODEL_POLICY_ARGS}
    selection = resolve_managed_chrome()
    if selection:
        options['executable_path'] = selection.path
    return options


@contextmanager
def owned_sync_browser(playwright):
    """同步站点留在原线程；只共用启动、资源租约与关闭，不搬站点逻辑。"""
    from runtime.execution_context import check_execution
    from runtime.resource_concurrency import acquire_resource
    with acquire_resource('anonymous_browser'):
        check_execution()
        browser = playwright.chromium.launch(**launch_options())
        try:
            yield browser
        finally:
            browser.close()


def run(coroutine, *, timeout):
    global _LOOP, _THREAD
    from runtime.execution_context import check_execution
    with _LOCK:
        if _LOOP is None:
            _LOOP = asyncio.new_event_loop()
            _THREAD = threading.Thread(target=_LOOP.run_forever, daemon=True, name='kr-public-browser')
            _THREAD.start()
    future = asyncio.run_coroutine_threadsafe(coroutine,_LOOP)
    deadline = time.monotonic()+timeout
    try:
        while True:
            check_execution()
            if time.monotonic() >= deadline:
                raise TimeoutError('dynamic_acquisition_deadline_exceeded')
            try:
                return future.result(timeout=min(.1,deadline-time.monotonic()))
            except FutureTimeout:
                if future.done():
                    raise
    except BaseException:
        future.cancel()
        raise


@asynccontextmanager
async def context():
    global _PLAYWRIGHT, _BROWSER, _SLOTS, _LAUNCH
    from runtime.execution_context import check_execution
    if _SLOTS is None:
        _SLOTS = asyncio.Semaphore(2)
        _LAUNCH = asyncio.Lock()
    from runtime.resource_concurrency import acquire_resource
    async with _SLOTS:
      with acquire_resource('anonymous_browser'):
        check_execution()
        async with _LAUNCH:
            reused = bool(_BROWSER and _BROWSER.is_connected())
            if not reused:
                await _launch_browser()
        page_context = await _BROWSER.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            locale='zh-CN',service_workers='block',accept_downloads=False)
        try:
            yield page_context, reused
        finally:
            await page_context.close()


async def _launch_browser():
    global _PLAYWRIGHT, _BROWSER
    from playwright.async_api import async_playwright
    if _PLAYWRIGHT is None:
        _PLAYWRIGHT = await async_playwright().start()
    _BROWSER = await _PLAYWRIGHT.chromium.launch(**launch_options())

def close():
    global _BROWSER, _PLAYWRIGHT, _LOOP, _THREAD, _SLOTS, _LAUNCH
    if _LOOP is None or not _LOOP.is_running():
        return
    async def cleanup():
        global _BROWSER, _PLAYWRIGHT
        if _BROWSER:
            await _BROWSER.close()
            _BROWSER=None
        if _PLAYWRIGHT:
            await _PLAYWRIGHT.stop()
            _PLAYWRIGHT=None
    try:
        asyncio.run_coroutine_threadsafe(cleanup(),_LOOP).result(timeout=3)
    except Exception:
        pass
    _LOOP.call_soon_threadsafe(_LOOP.stop)
    if _THREAD is not threading.current_thread():
        _THREAD.join(timeout=3)
    _LOOP = _THREAD = _SLOTS = _LAUNCH = None


atexit.register(close)
