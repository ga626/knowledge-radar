"""前台和 worker 共用的能力装配；注册不采集、不探测账号或启动服务。"""
from __future__ import annotations

import threading

from kr_core.registry import PlatformRegistry, registry

_BINDINGS_LOCK = threading.RLock()


def register_default_adapters(*, target_registry: PlatformRegistry = registry) -> PlatformRegistry:
    """仅填充缺失平台；重复装配不能覆盖已绑定的策略、钩子或调用方替身。"""
    from importlib import import_module
    from runtime.detail_composition import build_detail_strategy

    with _BINDINGS_LOCK:
        for name, module, adapter_type in (
            ("B站", "bilibili", "BilibiliAdapter"),
            ("YouTube", "youtube", "YouTubeAdapter"),
        ):
            if name not in target_registry.platforms():
                adapter_module = import_module("platform_adapters." + module + "_adapter")
                target_registry.register(
                    getattr(adapter_module, adapter_type)(),
                    detail_factory=lambda p=name: build_detail_strategy(p),
                )
        for name, module in (
            ("小红书", "xiaohongshu"), ("知乎", "zhihu"), ("BOSS直聘", "boss"),
            ("猎聘", "liepin"), ("脉脉", "maimai"), ("V2EX", "v2ex"), ("GitHub", "github"), ("微信", "wechat"),
        ):
            if name not in target_registry.platforms():
                import_module("platform_adapters." + module + "_adapter").register(target_registry)
    return target_registry
