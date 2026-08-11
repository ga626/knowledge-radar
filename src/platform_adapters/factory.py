"""Factory helpers for registering current platform adapters."""

from __future__ import annotations

from typing import Callable, Dict

from kr_core.models import SearchRequest
from kr_core.registry import PlatformRegistry, registry

from .bilibili_adapter import BilibiliAdapter
from .boss_adapter import BossAdapter
from .liepin_adapter import LiepinAdapter
from .maimai_adapter import MaimaiAdapter
from .xiaohongshu_adapter import XiaohongshuAdapter
from .youtube_adapter import YouTubeAdapter
from .zhihu_adapter import ZhihuAdapter


def register_default_adapters(
    *,
    bilibili_search: Callable[[SearchRequest], Dict],
    zhihu_search: Callable[[SearchRequest], Dict],
    xiaohongshu_search: Callable[[SearchRequest], Dict],
    boss_search: Callable[[SearchRequest], Dict] | None = None,
    liepin_search: Callable[[SearchRequest], Dict] | None = None,
    maimai_search: Callable[[SearchRequest], Dict] | None = None,
    youtube_search: Callable[[SearchRequest], Dict] | None = None,
    target_registry: PlatformRegistry = registry,
) -> PlatformRegistry:
    target_registry.register(BilibiliAdapter(bilibili_search))
    target_registry.register(ZhihuAdapter(zhihu_search))
    target_registry.register(XiaohongshuAdapter(xiaohongshu_search))
    if boss_search:
        target_registry.register(BossAdapter(boss_search))
    if liepin_search:
        target_registry.register(LiepinAdapter(liepin_search))
    if maimai_search:
        target_registry.register(MaimaiAdapter(maimai_search))
    if youtube_search:
        target_registry.register(YouTubeAdapter(youtube_search))
    return target_registry
