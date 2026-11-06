"""Taiwan Open Access Journals provider."""

from __future__ import annotations

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig, public_json_search, json_response, clean_api_text
from .models import AcademicWork
from urllib.parse import quote


class ToajProvider(ChineseOpenAccessProvider):
    name = "toaj"
    config = OpenAccessPlatformConfig(
        name="toaj",
        display_name="臺灣學術期刊開放取用平台",
        homepage="https://toaj.stpi.niar.org.tw/",
        status="available",
        available=True,
        auto_enabled=False,
        full_text_access="public_search_optional_article_fulltext",
        coverage="Taiwan open-access Chinese journals",
        stable=False,
        degraded_reason="全文权限需逐篇核验，不把检索命中当作已取得全文。",
        failure_category="",
        pdf_url_markers=(".pdf", "download"),
    )

    def search(self, request):
        def fetch(client):
            payload = json_response(client.post(self.config.homepage + 'search', data={
                'nowPage': 1, 'rowsPerPage': min(request.limit, 20), 'jurId': '', 'volId': '',
                'author': '', 'text': request.query, 'year': '', 'sortType': 0}))
            return payload.get('obj')
        def work(row):
            if not row.get('vitId') or not (row.get('title1') or row.get('titleCn')):
                return None
            return AcademicWork(title=clean_api_text(row.get('title1') or row['titleCn']),
                url=self.config.homepage + 'index/journal/volume/article/' + quote(row['vitId'], safe=''),
                authors=row.get('authorNameList') or [], abstract=clean_api_text(row.get('summaryCn') or row.get('summaryEn')),
                source=row.get('journalName') or self.config.display_name, source_database=self.name,
                document_type='journal-article',
                access_mode='public_search_api', full_text_status='article_landing_unverified',
                verification_status='public_search_record', raw={'query': request.query, 'id': row['vitId'],
                    'fulltext_permission': row.get('fullTextPublicPermission'), 'fulltext_link': row.get('fullTextLink')})
        return public_json_search(self, request, fetch, work)
