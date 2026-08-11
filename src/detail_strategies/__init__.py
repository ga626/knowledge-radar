"""Detail extraction strategies for platform-specific content."""

from .bilibili import BilibiliDetailDeps, BilibiliDetailStrategy
from .legacy import LegacyDetailStrategy
from .recruitment import RecruitmentDetailStrategy
from .xiaohongshu import XiaohongshuDetailDeps, XiaohongshuDetailStrategy
from .youtube import YouTubeDetailDeps, YouTubeDetailStrategy
from .zhihu import ZhihuDetailDeps, ZhihuDetailStrategy

__all__ = [
    "BilibiliDetailDeps",
    "BilibiliDetailStrategy",
    "LegacyDetailStrategy",
    "RecruitmentDetailStrategy",
    "XiaohongshuDetailDeps",
    "XiaohongshuDetailStrategy",
    "YouTubeDetailDeps",
    "YouTubeDetailStrategy",
    "ZhihuDetailDeps",
    "ZhihuDetailStrategy",
]
