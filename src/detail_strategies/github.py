"""GitHub对象策略；原文、覆盖与定位交给公共详情用例。"""
from kr_core.models import DetailResponse


class GitHubDetailStrategy:
    def extract(self, request):
        from collectors.platform.github_content import collect
        data = collect(request)
        return DetailResponse.from_legacy('GitHub', request.url, data,
            metadata={'strategy': 'github_objects'})
