"""Hong Kong Journals Online provider."""

from __future__ import annotations

from urllib.parse import quote

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig, public_json_search, json_response, clean_api_text
from .models import AcademicSearchRequest, AcademicWork


class HkjoProvider(ChineseOpenAccessProvider):
    name = "hkjo"
    config = OpenAccessPlatformConfig(
        name="hkjo",
        display_name="Hong Kong Journals Online",
        homepage="https://digitalrepository.lib.hku.hk/hkjo",
        status="available",
        available=True,
        auto_enabled=False,
        full_text_access="catalog_metadata_and_item_landing",
        coverage="Hong Kong journals, including Chinese-language publications",
        stable=True,
        degraded_reason=(
            "available but kept out of default Chinese auto order because coverage is Hong Kong journal specific"
        ),
        failure_category="",
        login_url="https://digitalrepository.lib.hku.hk/hkjo",
        manual_action="Use explicit provider when Hong Kong journal coverage is desired.",
        pdf_url_markers=(".pdf", "/article/view/", "/article/download/"),
    )

    def search_urls(self, request: AcademicSearchRequest):
        query = quote(str(request.query or "").strip())
        if query:
            return (
                f"https://digitalrepository.lib.hku.hk/catalog?q={query}&collection=5h73qj41h",
            )
        return (self.config.homepage,)

    def search(self, request):
        def fetch(client):
            payload = json_response(client.get('https://digitalrepository.lib.hku.hk/service/api/catalog',
                params={'q': request.query, 'collection': '5h73qj41h', 'rows': min(request.limit, 20)}))
            return (payload.get('response') or {}).get('docs')
        def work(row):
            titles = row.get('title_tesim') or []
            if not row.get('id') or not titles:
                return None
            return AcademicWork(title=clean_api_text(titles[0]),
                url='https://digitalrepository.lib.hku.hk/catalog/' + quote(str(row['id']), safe=''),
                authors=[clean_api_text(a) for a in row.get('creator_tesim', [])],
                abstract='\n'.join(map(clean_api_text, row.get('description_tesim', []))),
                source=self.config.display_name, source_database=self.name, access_mode='public_catalog_api',
                document_type=next(iter(row.get('resource_type_tesim') or []), 'unknown'),
                full_text_status='metadata_only', verification_status='public_search_record',
                raw={'query': request.query, 'catalog_id': row['id']})
        return public_json_search(self, request, fetch, work)
