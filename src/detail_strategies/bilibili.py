"""B站详情入口，委托同一分阶段视频用例。"""
from .video import VideoDetailStrategy

class BilibiliDetailStrategy(VideoDetailStrategy):
    def __init__(self):
        super().__init__('B站')
