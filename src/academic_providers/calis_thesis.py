"""CALIS thesis provider."""

from __future__ import annotations
import json
from urllib.parse import quote

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig, public_json_search, json_response, clean_api_text
from .models import AcademicWork


class CalisThesisProvider(ChineseOpenAccessProvider):
    name = "calis_thesis"
    config = OpenAccessPlatformConfig(
        name="calis_thesis",
        display_name="CALIS高校学位论文库",
        homepage="https://etd2.calis.edu.cn/",
        status="available",
        available=True,
        auto_enabled=False,
        access_mode="public_abstract_discovery",
        full_text_access="institution_or_document_delivery",
        coverage="Chinese university theses and dissertations",
        stable=False,
        degraded_reason="公开检索接口已取得元数据与摘要；全文需高校机构授权或 CALIS 文献传递。",
        failure_category="institution_or_document_delivery_required",
        requires_login=False,
        login_url="https://etd2.calis.edu.cn/",
        manual_action="Use only when the user has institutional authorization or document-delivery rights; not suitable for unattended full-text retrieval.",
    )

    def search(self, request):
        query = request.query.strip()
        payload = {'offset': 1, 'limit': min(request.limit, 20), 'sortKey': None,
            'queryValue': json.dumps({'list': [{'type': '0', 'searchValue': query, 'label': query, 'andor': 'and'}],
                                     'other': {}, 'secondList': []}, ensure_ascii=False),
            'searchJson': json.dumps({'condition': {'0': {'type': '0', 'label': query, 'value': query,
                                     'title': '全面检索', 'andor': 'and'}}, 'secondList': [], 'searchType': 'simple'}, ensure_ascii=False)}
        def fetch(client):
            data = json_response(client.post(self.config.homepage + 'prod-api/codex/ekb/opac/sep/simple/query', json=payload))
            return (data.get('data') or {}).get('content')
        def work(row):
            if not row.get('stringTitle'):
                return None
            urls = str(row.get('stringEresourceURL') or '').split(';')
            url = next((u for u in urls if u.startswith(('https://', 'http://'))),
                       self.config.homepage + 'searchList/index?searchType=simple&queryValue=' + quote(query) + '&queryKey=0&nav=common')
            year = str(row.get('stringPublishyear') or '')
            return AcademicWork(title=clean_api_text(row['stringTitle']), url=url,
                authors=[clean_api_text(a) for a in str(row.get('stringCreator') or '').split(';') if a],
                year=int(year) if year.isdigit() else None, abstract=clean_api_text(row.get('stringAbstract')),
                source=self.config.display_name, source_database=self.name, access_mode=self.config.access_mode,
                document_type='dissertation',
                full_text_status='institution_or_document_delivery', verification_status='public_search_record',
                raw={'query': query, 'id': row.get('ukey'), 'fulltext_requires_institution_or_delivery': True})
        return public_json_search(self, request, fetch, work)
