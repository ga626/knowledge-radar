"""National Taiwan University Repository provider."""

from __future__ import annotations

from urllib.parse import quote

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig, public_json_search, json_response
from .models import AcademicSearchRequest, AcademicWork


class NturProvider(ChineseOpenAccessProvider):
    name = "ntur"
    config = OpenAccessPlatformConfig(
        name="ntur",
        display_name="NTU Scholars / NTUR",
        homepage="https://scholars.lib.ntu.edu.tw/",
        status="available",
        available=True,
        auto_enabled=False,
        full_text_access="public_metadata_optional_item_files",
        coverage="National Taiwan University repository records",
        stable=False,
        degraded_reason="元数据公开；是否可读全文取决于每条记录的文件与授权。",
        failure_category="",
        pdf_url_markers=(".pdf", "bitstream", "download"),
    )

    def search_urls(self, request: AcademicSearchRequest):
        query = quote(str(request.query or "").strip())
        if query:
            return (f"https://scholars.lib.ntu.edu.tw/search?query={query}",)
        return (self.config.homepage,)

    def search(self, request):
        def fetch(client):
            data = json_response(client.get(self.config.homepage + 'server/api/discover/search/objects',
                params={'query': request.query, 'size': min(request.limit, 20), 'page': 0, 'configuration': 'researchoutputs'}))
            return ((data.get('_embedded') or {}).get('searchResult') or {}).get('_embedded', {}).get('objects')
        def work(row):
            item = (row.get('_embedded') or {}).get('indexableObject') or {}
            metadata = item.get('metadata') or {}
            def values(key):
                return [str(v['value']) for v in metadata.get(key, []) if isinstance(v, dict) and v.get('value')]
            url = ((row.get('_links') or {}).get('indexableObject') or {}).get('href', '')
            if not item.get('name') or not url:
                return None
            year = ''.join(values('dc.date.issued'))[:4]
            return AcademicWork(title=item['name'], url=url, authors=values('dc.contributor.author'),
                abstract='\n'.join(values('dc.description.abstract')), year=int(year) if year.isdigit() else None,
                source=self.config.display_name, source_database=self.name, access_mode='public_repository_api',
                document_type=next(iter(values('dc.type')), 'unknown'),
                full_text_status='metadata_only', verification_status='public_search_record', raw={'query': request.query, 'uuid': item.get('uuid')})
        return public_json_search(self, request, fetch, work)
