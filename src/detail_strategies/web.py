"""开放文章和文档详情；不借账号、不把网页元数据冒充媒体或评论。"""
from kr_core import DetailResponse


class PublicWebDetailStrategy:
    def extract(self, request):
        from generic_web import GenericWebRequest, collect_url
        from runtime.execution_context import check_execution
        check_execution()
        response = collect_url(GenericWebRequest(url=request.url,
            timeout=min(600, max(1, float(request.options.get('timeout_s', 600)))),
            document_options=request.options.get('document') or {},
            use_jina=bool(request.options.get('use_jina', True))))
        check_execution()
        data = response.to_mcp_dict()
        data['platform'] = request.platform or 'web'
        if response.collector == 'pypdf' and not data.get('error'):
            parsed = response.metadata
            data['document_blocks'] = parsed.get('document_blocks') or []
            data['content_locators'] = parsed.get('locators') or []
            data['document'] = {key: parsed[key] for key in (
                'input_sha256', 'page_count', 'pages_read', 'requested_pages', 'page_ledger',
                'request_satisfied', 'source_pages_exhausted', 'processing_identity', 'resume',
                'original_asset', 'coverage', 'processing_method', 'missing_text_pages',
                'quality_warnings') if key in parsed}
        error = data.get('error')
        if error:
            data.update(status='failed', failure_type=error.get('type', 'web_collection_failed'))
        elif not response.content.strip():
            data.update(status='failed', failure_type='empty_detail', error='No article text acquired')
        else:
            data['status'] = 'ok'
        # 未采集的评论和媒体保留为缺口；只有平台清单才能证明不适用。
        return DetailResponse.from_legacy(data['platform'], request.url, data,
            metadata={'strategy': 'public_web', 'collector': response.collector})
