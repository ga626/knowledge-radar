"""GitHub注册与资源就绪钩子；不在公共调度器写平台判断。"""
from dataclasses import replace
import uuid
from kr_core.models import PlatformCapability, SearchResponse


class GitHubAdapter:
    platform = 'GitHub'
    capabilities = PlatformCapability(platform=platform, search=True, detail=True, comments=True,
        login_required=False, strategies=['readonly_api', 'optional_gh_transport', 'commit_pinned_sources'],
        notes='公开仓库；源码按提交与行号；Issue/PR评论按显式分页范围。')

    @staticmethod
    def matches_url(url):
        from collectors.platform.github_content import matches_url
        return matches_url(url)

    @staticmethod
    def resource_ready(resource):
        from collectors.platform.github_api import resource_ready
        return resource_ready(resource)

    @staticmethod
    def runtime_identity():
        from collectors.platform.github_api import CONTRACT
        import os
        return {'contract': CONTRACT, 'auth_epoch': os.environ.get('KR_GITHUB_AUTH_EPOCH', '')}

    @staticmethod
    def prepare_detail(request):
        return replace(request, options={'github_checkpoint_id': uuid.uuid4().hex, **request.options})

    def search(self, request):
        from collectors.platform.gh_cli_sidecar import search_repositories
        result = search_repositories(request.keyword, limit=request.limit, page=request.page)
        return SearchResponse.from_legacy_items(self.platform, result['items'])


def register(registry):
    from detail_strategies.github import GitHubDetailStrategy
    registry.register(GitHubAdapter(), detail_factory=GitHubDetailStrategy)
