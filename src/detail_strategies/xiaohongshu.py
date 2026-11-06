"""小红书详情薄适配；不再装配旧CDP、Bridge或账号池。"""
from kr_core import DetailRequest, DetailResponse


class XiaohongshuDetailStrategy:
    platform = "小红书"

    def extract(self, request: DetailRequest) -> DetailResponse:
        from runtime.xhs_opencli import detail
        from understanding.evidence import build_detail_evidence
        data = detail(request)
        return DetailResponse.from_legacy(self.platform, request.url, data,
            evidence=build_detail_evidence(request.url, self.platform, data),
            metadata={"strategy": "xiaohongshu_detail"})
