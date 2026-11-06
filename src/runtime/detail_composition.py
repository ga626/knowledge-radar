"""惰性详情装配；只有装配层认识站点实现，解析/执行层不认识server。"""

def resolve_strategy(platform):
    from kr_core.registry import registry
    strategy = registry.detail_strategy(platform)
    if strategy is None and platform == 'GitHub':
        from detail_strategies.github import GitHubDetailStrategy
        return GitHubDetailStrategy()
    if strategy is None and platform in {'', 'unknown', 'web'}:
        from detail_strategies.web import PublicWebDetailStrategy
        return PublicWebDetailStrategy()
    return strategy

def build_detail_strategy(platform):
    if platform in {'B站', 'YouTube'}:
        from detail_strategies.video import VideoDetailStrategy
        from importlib import import_module
        module = "bilibili" if platform == "B站" else "youtube"
        collector = import_module("collectors.platform." + module)
        return VideoDetailStrategy(platform, collect=collector.collect_video_detail)
    if platform == '知乎':
        from detail_strategies.zhihu import ZhihuDetailStrategy
        return ZhihuDetailStrategy()
    return None
