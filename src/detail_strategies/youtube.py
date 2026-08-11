"""YouTube detail strategy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional

from kr_core import DetailRequest, DetailResponse, EvidenceItem


@dataclass(frozen=True)
class YouTubeDetailDeps:
    extract_video_id: Callable[[str], Optional[str]]
    get_detail: Callable[[str], Dict]
    attach_routing: Callable[[str, Dict], Dict]
    routing_recommends_l2: Callable[[Dict], bool]
    deep_analyze: Callable[..., Dict]
    direct_media_probe: Callable[[str, bool], Dict]
    evidence_builder: Callable[[str, str, Dict], EvidenceItem]


class YouTubeDetailStrategy:
    platform = "YouTube"

    def __init__(self, deps: YouTubeDetailDeps) -> None:
        self.deps = deps

    def extract(self, request: DetailRequest) -> DetailResponse:
        video_id = self.deps.extract_video_id(request.url)
        if not video_id:
            result = {"platform": self.platform, "url": request.url, "error": "无法从 URL 提取 YouTube video_id"}
            return self._response(request, result)

        result = self.deps.get_detail(video_id)
        result.setdefault("platform", self.platform)
        result.setdefault("url", request.url)
        if result.get("error"):
            return self._response(request, result)

        result = self.deps.attach_routing(request.url, result)
        result["direct_media"] = self._direct_media_probe(request, video_id)
        if request.enable_deep_analysis or (request.auto_multimodal and self.deps.routing_recommends_l2(result)):
            result["deep_analysis"] = self.deps.deep_analyze(video_id, result, request.research_session_id, request.options)
            result = self.deps.attach_routing(request.url, result)
        elif request.auto_multimodal:
            result["deep_analysis"] = {
                "status": "skipped",
                "reason": "routing_did_not_recommend_l2",
                "routing_decision": (result.get("routing") or {}).get("decision", {}),
            }
        return self._response(request, result)

    def _response(self, request: DetailRequest, result: Dict) -> DetailResponse:
        return DetailResponse.from_legacy(
            self.platform,
            request.url,
            result,
            evidence=self.deps.evidence_builder(request.url, self.platform, result),
            metadata={"strategy": "youtube_detail"},
        )

    def _direct_media_probe(self, request: DetailRequest, video_id: str) -> Dict:
        # YouTube Data API does not return direct playable media URLs. This
        # still exposes the productized status so routing can avoid blindly
        # passing watch URLs to native media models.
        enabled = request.auto_multimodal or request.enable_deep_analysis
        return self.deps.direct_media_probe(video_id, enabled)
