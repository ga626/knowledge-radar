"""NSSD / National Center for Philosophy and Social Sciences Documentation provider."""

from __future__ import annotations

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig, encoded_nssd_search_url, public_json_search, json_response, clean_api_text
from .models import AcademicSearchRequest, AcademicWork
from urllib.parse import quote


class NssdProvider(ChineseOpenAccessProvider):
    name = "nssd"
    config = OpenAccessPlatformConfig(
        name="nssd",
        display_name="国家哲学社会科学文献中心",
        homepage="https://www.ncpssd.org/",
        status="available",
        available=True,
        auto_enabled=True,
        full_text_access="direct_pdf_confirmed",
        coverage="Chinese philosophy and social sciences OA papers",
        stable=True,
        direct_pdf_samples=("https://ft.ncpssd.cn/pdf/getComm/npssd/pageStyle?url=pdf/1779066653843.pdf",),
        pdf_url_markers=(".pdf", "getcomm", "pagestyle"),
    )

    def search_urls(self, request: AcademicSearchRequest):
        query = str(request.query or "").strip()
        urls = []
        if query:
            urls.append(encoded_nssd_search_url(query))
        urls.append(self.config.homepage)
        return urls

    def search(self, request):
        query = request.query.strip().replace('\\', '\\\\').replace('"', '\\"')
        expression = '(' + ' OR '.join(f'{field}="{query}"' for field in ('IKTE','IKPYTE','IKST','IKET','IKSE')) + ')'
        def fetch(client):
            payload = json_response(client.post('https://www.ncpssd.org/searchHandler/search',
                data={'search': expression, 'pageNum': 1, 'pageSize': min(request.limit, 20), 'sort': ''}))
            return (payload.get('data') or {}).get('rows')
        def work(row):
            if not row.get('title') or not row.get('encryptedUrl'):
                return None
            url = 'https://www.ncpssd.org/Literature/secure/articleinfo?params=' + quote(row['encryptedUrl'], safe='')
            year = str(row.get('years') or '')
            return AcademicWork(title=clean_api_text(row['title']), url=url,
                authors=[clean_api_text(a) for a in str(row.get('creator') or '').split(';') if a],
                year=int(year) if year.isdigit() else None, abstract=clean_api_text(row.get('remark')),
                source=self.config.display_name, source_database=self.name, access_mode='public_search_api',
                document_type='journal-article',
                full_text_status='article_landing_unverified', verification_status='public_search_record',
                raw={'query': request.query, 'id': row.get('data_id') or row.get('id'), 'pdf_url': row.get('pdfurl')})
        return public_json_search(self, request, fetch, work)
