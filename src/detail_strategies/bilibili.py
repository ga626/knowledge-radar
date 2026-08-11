"""Bilibili detail strategy.

This module owns the Bilibili detail orchestration boundary. It reuses the
existing low-level functions from server.py through explicit dependencies so
the public MCP response stays compatible while the detail layer becomes
replaceable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from kr_core import DetailRequest, DetailResponse, EvidenceItem
from multimodal.pipeline import MultimodalPipeline
from runtime.media_cache import media_cache_subdir


@dataclass(frozen=True)
class BilibiliDetailDeps:
    extract_bvid: Callable[[str], Optional[str]]
    get_info: Callable[[str], Optional[Dict]]
    transcribe: Callable[..., str]
    get_comments: Callable[[str], List[Dict]]
    filter_comments: Callable[..., List[Dict]]
    attach_routing: Callable[[str, Dict], Dict]
    routing_recommends_l2: Callable[[Dict], bool]
    deep_analyze: Callable[..., Dict]
    direct_media_probe: Callable[[str, bool], Dict]
    evidence_builder: Callable[[str, str, Dict], EvidenceItem]
    data_dir: str


class BilibiliDetailStrategy:
    platform = "B站"

    def __init__(self, deps: BilibiliDetailDeps) -> None:
        self.deps = deps

    def extract(self, request: DetailRequest) -> DetailResponse:
        bvid = self.deps.extract_bvid(request.url)
        if not bvid:
            result = {"platform": self.platform, "url": request.url, "error": "无法从 URL 提取 BVID"}
            return self._response(request, result)

        result: Dict = {
            "platform": self.platform,
            "title": "",
            "desc": "",
            "transcript": "",
            "url": request.url,
        }
        pipeline = MultimodalPipeline(self.platform)

        info = self.deps.get_info(bvid)
        if info:
            result["title"] = info.get("title", "")
            result["desc"] = info.get("desc", "")
            result["author"] = info.get("author", "")
            result["duration"] = info.get("duration", 0)
            result["cover"] = info.get("cover", "")
            result["stats"] = info.get("stats", {})
            result["video_play_count"] = info.get("video_play_count", 0)
            result["liked_count"] = info.get("liked_count", 0)
            result["video_coin_count"] = info.get("video_coin_count", 0)
            result["video_favorite_count"] = info.get("video_favorite_count", 0)
            result["video_share_count"] = info.get("video_share_count", 0)
            result["video_danmaku"] = info.get("video_danmaku", 0)
            result["video_comment"] = info.get("video_comment", 0)

        transcribe_enabled = os.environ.get("KR_BILIBILI_TRANSCRIBE_ON_DETAIL", "1").strip().lower() not in {"0", "false", "no", "off"}
        result["transcript"] = pipeline.run(
            "video_transcript",
            trigger="always_bilibili_detail",
            enabled=transcribe_enabled,
            fn=lambda: self._call_transcribe(bvid, request),
        )
        if not transcribe_enabled:
            result["transcript"] = "[transcribe] skipped by KR_BILIBILI_TRANSCRIBE_ON_DETAIL=0"
        comments = self.deps.get_comments(bvid)
        result["comments"] = comments

        filtered_comments = pipeline.run(
            "comment_filtering",
            trigger="enable_comment_filtering",
            enabled=request.enable_comment_filtering,
            fn=lambda: self._call_filter_comments(comments, bvid, request),
        )
        if request.enable_comment_filtering:
            result["filtered_comments"] = filtered_comments or []
        else:
            result["filtered_comments"] = []
        result["comment_filtering_policy"] = {
            "blocking": False,
            "trigger": "enable_comment_filtering",
            "timeout_behavior": "returns raw comments with background cache when local filter exceeds timeout",
        }

        if request.research_session_id:
            result["research_session_id"] = request.research_session_id
        result = self.deps.attach_routing(request.url, result)

        result["direct_media"] = self._direct_media_probe(request, bvid)

        if request.enable_deep_analysis or (request.auto_multimodal and self.deps.routing_recommends_l2(result)):
            result["deep_analysis"] = pipeline.run(
                "video_deep_analysis",
                trigger="enable_deep_analysis_or_auto_multimodal_routing",
                enabled=True,
                fn=lambda: self._call_deep_analyze(bvid, result, request),
            )
            result = self.deps.attach_routing(request.url, result)
        elif request.auto_multimodal:
            result["deep_analysis"] = {
                "status": "skipped",
                "reason": "routing_did_not_recommend_l2",
                "routing_decision": (result.get("routing") or {}).get("decision", {}),
            }

        result["multimodal_pipeline"] = pipeline.to_dict()
        return self._response(request, result)

    def _call_transcribe(self, bvid: str, request: DetailRequest) -> str:
        try:
            return self.deps.transcribe(bvid, request.research_session_id, request.options)
        except TypeError:
            return self.deps.transcribe(bvid, request.research_session_id)

    def _call_filter_comments(self, comments: List[Dict], bvid: str, request: DetailRequest) -> List[Dict]:
        output_dir = str(media_cache_subdir("comments", content_id=bvid))
        try:
            return self.deps.filter_comments(comments, output_dir, request.options, bvid, request.url)
        except TypeError:
            return self.deps.filter_comments(comments, output_dir)

    def _call_deep_analyze(self, bvid: str, result: Dict, request: DetailRequest) -> Dict:
        try:
            return self.deps.deep_analyze(bvid, result, request.research_session_id, request.options)
        except TypeError:
            return self.deps.deep_analyze(bvid, result, request.research_session_id)

    def _direct_media_probe(self, request: DetailRequest, bvid: str) -> Dict:
        enabled_by_env = os.environ.get("KR_DIRECT_MEDIA_PROBE_ON_DETAIL", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        should_probe = enabled_by_env or request.auto_multimodal or request.enable_deep_analysis
        if not should_probe:
            return {
                "schema": "knowledgeradar-direct-media/v1",
                "status": "skipped",
                "reason": "direct media probe runs only for auto_multimodal/deep analysis or KR_DIRECT_MEDIA_PROBE_ON_DETAIL=1",
            }
        try:
            return self.deps.direct_media_probe(bvid, True)
        except Exception as exc:
            return {
                "schema": "knowledgeradar-direct-media/v1",
                "status": "failed",
                "reason": str(exc)[:240],
            }

    def _response(self, request: DetailRequest, result: Dict) -> DetailResponse:
        return DetailResponse.from_legacy(
            self.platform,
            request.url,
            result,
            evidence=self.deps.evidence_builder(request.url, self.platform, result),
            metadata={"strategy": "bilibili_detail"},
        )
