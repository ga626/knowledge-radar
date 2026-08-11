"""Platform adapter implementations for KnowledgeRadar."""

from .bilibili_adapter import BilibiliAdapter
from .boss_adapter import BossAdapter
from .liepin_adapter import LiepinAdapter
from .maimai_adapter import MaimaiAdapter
from .factory import register_default_adapters
from .xiaohongshu_adapter import XiaohongshuAdapter
from .youtube_adapter import YouTubeAdapter
from .zhihu_adapter import ZhihuAdapter

__all__ = ["BilibiliAdapter", "BossAdapter", "LiepinAdapter", "MaimaiAdapter", "XiaohongshuAdapter", "YouTubeAdapter", "ZhihuAdapter", "register_default_adapters"]
