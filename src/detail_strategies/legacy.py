"""Compatibility strategy for platform detail paths not migrated yet."""

from __future__ import annotations

from typing import Callable, Dict

from kr_core import DetailRequest, DetailResponse, EvidenceItem


class LegacyDetailStrategy:
    def __init__(
        self,
        *,
        platform: str,
        extractor: Callable[[DetailRequest], Dict],
        evidence_builder: Callable[[str, str, Dict], EvidenceItem],
    ) -> None:
        self.platform = platform
        self._extractor = extractor
        self._evidence_builder = evidence_builder

    def extract(self, request: DetailRequest) -> DetailResponse:
        result = self._extractor(request)
        platform = str(result.get("platform") or request.platform or self.platform)
        return DetailResponse.from_legacy(
            platform,
            request.url,
            result,
            evidence=self._evidence_builder(request.url, platform, result),
            metadata={"strategy": "legacy_compat", "platform": self.platform},
        )
