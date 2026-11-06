"""Detail extraction strategies for platform-specific content."""

from .bilibili import BilibiliDetailStrategy
from .legacy import LegacyDetailStrategy
from .recruitment import RecruitmentDetailStrategy
from .xiaohongshu import XiaohongshuDetailStrategy
from .youtube import YouTubeDetailStrategy
from .zhihu import ZhihuDetailStrategy

__all__ = [
    "BilibiliDetailStrategy",
    "LegacyDetailStrategy",
    "RecruitmentDetailStrategy",
    "XiaohongshuDetailStrategy",
    "YouTubeDetailStrategy",
    "ZhihuDetailStrategy",
]
