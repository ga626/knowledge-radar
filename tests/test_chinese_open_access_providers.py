import pytest

from academic_providers.models import AcademicSearchRequest
from academic_providers.nssd import NssdProvider
from academic_providers.pubscholar import (
    PubScholarProvider,
    build_pubscholar_article_payload,
    extract_pdf_text,
    pubscholar_file_url_from_viewer_url,
)
from academic_providers.sciengine import SciEngineProvider
from academic_providers.sciengine import extract_pdf_text as extract_sciengine_pdf_text
from academic_providers.sciengine import normalize_sciengine_article_url
from academic_providers.service import _provider_order, academic_provider_status
from academic_providers.socolar import SocolarProvider
from academic_providers.vip_oa import VipOpenAccessProvider
from academic_providers.vip_oa import extract_pdf_text as extract_vip_pdf_text
from academic_providers.vip_oa import vip_ascii_url, vip_file_url_from_viewer_url, vip_reading_url_from_detail_url
from runtime.status_schema import classify_runtime_payload


def test_chinese_open_fulltext_providers_are_registered() -> None:
    status = academic_provider_status()

    for name in ["nssd", "chinaxiv", "hanspub", "oajrc", "sciopen", "ivy_publisher"]:
        assert name in status
        assert status[name]["status"] == "available"
        assert status[name]["full_text_access"] == "direct_pdf_confirmed"

    assert status["hkjo"]["status"] == "available"
    assert status["hkjo"]["auto_enabled"] is False
    assert status["hkjo"]["full_text_access"] == "direct_pdf_confirmed"

    assert status["oalib"]["status"] == "degraded"
    assert status["oalib"]["auto_enabled"] is False
    assert status["oalib"]["failure_category"] == "pdf_landing_not_direct_pdf"

    assert status["pubscholar"]["status"] == "available"
    assert status["pubscholar"]["auto_enabled"] is True
    assert status["pubscholar"]["full_text_access"] == "anonymous_open_access_pdf_text_confirmed"
    assert status["pubscholar"]["text_extraction"]["method"] == "pypdf_from_anonymous_pdf_file"
    assert status["pubscholar"]["resource_types"]["article"]["open_access_filter"] == {"open_access": "openAccess"}

    assert status["sciengine"]["status"] == "available"
    assert status["sciengine"]["auto_enabled"] is True
    assert status["sciengine"]["full_text_access"] == "anonymous_open_or_free_pdf_text_confirmed"
    assert status["sciengine"]["text_extraction"]["method"] == "playwright_public_article_page_to_pdf_endpoint_then_pypdf"

    assert status["socolar"]["status"] == "available"
    assert status["socolar"]["auto_enabled"] is False
    assert status["socolar"]["access_mode"] == "logged_abstract_discovery"
    assert status["socolar"]["full_text_access"] == "structured_abstract_with_external_fulltext_link"
    assert status["socolar"]["text_extraction"]["method"] == "socolar_detail_api_abstract"
    assert status["socolar"]["fulltext_limitation"]["status"] == "EXPECTED_DEGRADED"

    assert status["vip_oa"]["status"] == "available"
    assert status["vip_oa"]["auto_enabled"] is True
    assert status["vip_oa"]["access_mode"] == "logged_online_reading_pdf_viewer"
    assert status["vip_oa"]["full_text_access"] == "logged_online_reading_pdf_text_confirmed"
    assert status["vip_oa"]["text_extraction"]["method"] == "cqvip_detail_resourceid_to_online_reading_pdfjs_file_then_pypdf"
    assert status["vip_oa"]["text_extraction"]["download_button_used"] is False
    assert status["vip_oa"]["quota_boundary"]["download_button_used"] is False

    for name in ["coaj", "ucdrs", "calis_thesis", "nstrs", "toaj", "oalib"]:
        assert name in status
        assert status[name]["status"] in {"degraded", "unavailable"}
        assert status[name]["degraded_reason"]

    for name in ["ucdrs", "calis_thesis", "nstrs"]:
        assert status[name]["failure_category"]
        assert status[name]["requires_login"] is True
        assert status[name]["login_url"].startswith(("http://", "https://"))
        assert status[name]["manual_action"]
    assert status["vip_oa"]["requires_login"] is True
    assert status["vip_oa"]["login_url"].startswith(("http://", "https://"))
    assert status["vip_oa"]["manual_action"]

    assert status["coaj"]["failure_category"] == "metadata_available_fulltext_endpoint_unconfirmed"


def test_candidate_manual_guidance_is_expected_degraded_not_interaction() -> None:
    classified = classify_runtime_payload(
        {
            "status": "degraded",
            "configured": True,
            "auto_enabled": False,
            "requires_login": True,
            "manual_action": "Open an authorized browser only after a failed auth probe.",
            "degraded_reason": "login-gated candidate provider",
        },
        required=False,
        main_chain=False,
        optional=True,
    )

    assert classified["status_class"] == "EXPECTED_DEGRADED"
    assert classified["blocks_overall_pass"] is False


def test_chinese_candidate_provider_reprobe_boundaries_are_declared() -> None:
    status = academic_provider_status()

    expected_degraded = {
        "coaj": "metadata_available_fulltext_endpoint_unconfirmed",
        "nssd_cn": "paused_legacy_domain_confirmed_canonical_elsewhere",
        "paper_edu": "site_maintenance_fulltext_path_unavailable",
        "gooa": "metadata_oai_endpoint_unavailable_fulltext_unconfirmed",
        "oalib": "pdf_landing_not_direct_pdf",
        "toaj": "current_environment_tcp443_route_timeout",
        "ntur": "current_environment_tcp443_route_timeout",
    }
    for name, failure_category in expected_degraded.items():
        assert status[name]["validation_status"] == "EXPECTED_DEGRADED"
        assert status[name]["blocks_overall_pass"] is False
        assert status[name]["failure_category"] == failure_category
        assert status[name]["degraded_reason"]

    assert status["pubscholar"]["validation_status"] == "PASS"
    assert status["pubscholar"]["blocks_overall_pass"] is False
    assert status["pubscholar"]["failure_category"] == ""
    assert status["pubscholar"]["degraded_reason"] == ""

    assert status["sciengine"]["validation_status"] == "PASS"
    assert status["sciengine"]["blocks_overall_pass"] is False
    assert status["sciengine"]["failure_category"] == ""
    assert status["sciengine"]["degraded_reason"] == ""

    assert status["vip_oa"]["validation_status"] == "PASS"
    assert status["vip_oa"]["blocks_overall_pass"] is False
    assert status["vip_oa"]["failure_category"] == ""
    assert status["vip_oa"]["degraded_reason"] == ""

    assert status["socolar"]["validation_status"] == "PASS"
    assert status["socolar"]["blocks_overall_pass"] is False
    assert status["socolar"]["failure_category"] == ""
    assert status["socolar"]["fulltext_limitation"]["status"] == "EXPECTED_DEGRADED"


def test_auto_order_prefers_confirmed_chinese_open_fulltext(monkeypatch) -> None:
    monkeypatch.delenv("BAIDU_QIANFAN_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)

    order = _provider_order(AcademicSearchRequest(query="人工智能 教育", provider="auto"), "auto")

    assert order[:8] == ["nssd", "chinaxiv", "hanspub", "oajrc", "sciopen", "pubscholar", "sciengine", "vip_oa"]
    assert "openalex" in order
    assert "semanticscholar" in order


def test_nssd_search_url_uses_encoded_query() -> None:
    urls = NssdProvider().search_urls(AcademicSearchRequest(query="人工智能 教育"))

    assert urls[0].startswith("https://www.ncpssd.org/Literature/articlelist?")
    assert "search=" in urls[0]
    assert urls[-1] == "https://www.ncpssd.org/"


def test_pubscholar_article_payload_defaults_to_open_access_filter() -> None:
    payload = build_pubscholar_article_payload("ai", limit=50)

    assert payload["query"] == "ai"
    assert payload["size"] == 20
    assert payload["open_access"] == "openAccess"
    assert payload["user_id"]


def test_socolar_maps_detail_api_result_to_abstract_discovery_work() -> None:
    provider = SocolarProvider()
    work = provider._work_from_detail(
        {
            "id": "2014193426341217315",
            "title": "Multidisciplinary ML Techniques on Gesture Recognition",
            "abstracts": "Gesture recognition has a crucial role in Human-Computer Interaction.",
            "authors": [{"name": "Christos Panagiotou"}, {"name": "Evanthia Faliagka"}],
            "articleDoi": "10.3390/ai6010017",
            "publisher": "MDPI AG",
            "publishDate": "2025-01-17",
            "url": "https://www.mdpi.com/2673-2688/6/1/17",
            "isOa": 1,
        },
        AcademicSearchRequest(query="gesture recognition", provider="socolar"),
    )

    assert work.title == "Multidisciplinary ML Techniques on Gesture Recognition"
    assert work.url == "https://www.socolar.com/articleDetails?articleId=2014193426341217315"
    assert work.authors == ["Christos Panagiotou", "Evanthia Faliagka"]
    assert work.year == 2025
    assert work.doi == "10.3390/ai6010017"
    assert work.source == "MDPI AG"
    assert work.oa_status == "open"
    assert work.source_database == "socolar"
    assert work.access_mode == "logged_abstract_discovery"
    assert work.full_text_status == "abstract_with_external_landing_url"
    assert work.verification_status == "socolar_detail_abstract_confirmed"
    assert work.raw["external_url"] == "https://www.mdpi.com/2673-2688/6/1/17"


def test_pubscholar_file_url_from_pdf_viewer_url() -> None:
    viewer_url = (
        "https://pubscholar.cn/child/pdf-view/web/viewer.html?"
        "file=https://pubscholar.cn/files?fastdfspath=group1/M02/B2/68/sample&cache=false"
    )

    assert pubscholar_file_url_from_viewer_url(viewer_url) == "https://pubscholar.cn/files?fastdfspath=group1/M02/B2/68/sample"


def test_vip_file_url_from_pdfjs_viewer_url_and_ascii_conversion() -> None:
    viewer_url = (
        "https://www.cqvip.com/pdfjs-legacy/web/viewer.html?"
        "file=https%3A%2F%2Fimgv3.cqvip.com%2Fwebsite%2Fdown%2Fliterature%2F2064611431879999489%2F"
        "1%25E3%2580%2581GoldMiner-AI%25E5%25A4%25A7%25E6%2595%25B0.pdf%3FExpires%3D1781111701"
        "%26OSSAccessKeyId%3Dkey%26Signature%3Dsig%253D"
    )

    file_url = vip_file_url_from_viewer_url(viewer_url)

    assert file_url.startswith("https://imgv3.cqvip.com/website/down/literature/")
    assert "1%E3%80%81GoldMiner-AI%E5%A4%A7%E6%95%B0.pdf" in file_url
    assert "Signature=sig%3D" in file_url
    ascii_url = vip_ascii_url(file_url)
    assert "1%E3%80%81GoldMiner-AI%E5%A4%A7%E6%95%B0.pdf" in ascii_url
    assert "Signature=sig%3D" in ascii_url


def test_vip_reading_url_from_detail_url() -> None:
    detail_url = (
        "https://www.cqvip.com/doc/journal/7203295942?"
        "sign=f91a7cf4a93debc40bf46d6f667c9b775f8ac02edd2c506b31d891f0d56483cf"
        "&expireTime=1796664140538&resourceId=7203295942&type=1"
    )

    assert vip_reading_url_from_detail_url(detail_url) == "https://www.cqvip.com/onlinereading?type=1&lid=7203295942"


def test_vip_maps_search_result_to_online_reading_candidate() -> None:
    provider = VipOpenAccessProvider()
    work = provider._work_from_search_result(
        {
            "title": "GoldMiner-AI:大数据与人工智能找矿系统的设计与实现",
            "detail_url": "https://www.cqvip.com/doc/journal/7203295942?resourceId=7203295942&type=1",
            "snippet": "2026年第4期 DOI: 10.13745/j.esf.sf.2026.3.4",
            "authors": "周永章 朱彪彪",
            "source": "地学前缘",
            "availability_label": "有全文",
        },
        AcademicSearchRequest(query="GoldMiner-AI", provider="vip_oa"),
    )

    assert work.source_database == "vip_oa"
    assert work.full_text_status == "open_access_article_detail"
    assert work.verification_status == "vip_detail_to_online_reading_candidate"
    assert work.raw["reading_url"] == "https://www.cqvip.com/onlinereading?type=1&lid=7203295942"
    assert work.raw["download_button_used"] is False


def test_vip_extract_pdf_text_from_preview_bytes() -> None:
    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n"
        b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        b"5 0 obj << /Length 48 >> stream\n"
        b"BT /F1 18 Tf 72 200 Td (VIP preview text) Tj ET\n"
        b"endstream endobj\n"
        b"xref\n0 6\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n"
        b"0000000115 00000 n \n0000000234 00000 n \n0000000304 00000 n \n"
        b"trailer << /Root 1 0 R /Size 6 >>\nstartxref\n402\n%%EOF\n"
    )

    assert "VIP preview text" in extract_vip_pdf_text(pdf)


def test_pubscholar_maps_article_api_result_to_open_detail_work() -> None:
    provider = PubScholarProvider()
    work = provider._work_from_article(
        {
            "id": "03fae06090de117876cbdde622d3b438",
            "title": "Artificial <span class=\"Highlight\">intelligence</span>",
            "abstracts": "A <b>clean</b> abstract",
            "author": [{"name": "Alice"}, {"name": "Bob"}],
            "date": "2026",
            "journal": "Science Bulletin",
            "type": "article",
            "cn_type": "论文",
        },
        AcademicSearchRequest(query="ai", provider="pubscholar"),
    )

    assert work.title == "Artificial intelligence"
    assert work.url == "https://pubscholar.cn/articles/03fae06090de117876cbdde622d3b438"
    assert work.authors == ["Alice", "Bob"]
    assert work.year == 2026
    assert work.oa_status == "open"
    assert work.access_mode == "anonymous_open_access_pdf_viewer"
    assert work.full_text_status == "open_access_article_detail"
    assert work.raw["open_access_filter"] is True


def test_pubscholar_maps_direct_file_result_to_text_extractable_status() -> None:
    provider = PubScholarProvider()
    work = provider._work_from_article(
        {
            "id": "direct-pdf-id",
            "title": "Direct PDF",
            "local_links": [{"fastdfspath": "group1/M00/sample.pdf"}],
        },
        AcademicSearchRequest(query="ai", provider="pubscholar"),
    )

    assert work.url == "https://pubscholar.cn/files?fastdfspath=group1/M00/sample.pdf"
    assert work.full_text_status == "pdf_text_extractable"


@pytest.mark.parametrize(
    ("article", "query", "expected_url"),
    [
        (
            {
                "id": "attachment-id",
                "title": "Attachment PDF",
                "attachments": [{"file_link": "group1/M02/B2/68/CgMLD2oCxGKAedHHAH9p9jdsnlA0171241"}],
            },
            "ai",
            "https://pubscholar.cn/files?fastdfspath=group1/M02/B2/68/CgMLD2oCxGKAedHHAH9p9jdsnlA0171241",
        ),
        (
            {
                "id": "local-link-id",
                "title": "Local preview PDF",
                "local_links": ["https://file.scholarin.cn/preview2?file=journal_upload_c27d959b770e2e2843c46d6ed3914bf0.pdf"],
            },
            "人工智能 教育",
            "https://file.scholarin.cn/preview2?file=journal_upload_c27d959b770e2e2843c46d6ed3914bf0.pdf",
        ),
    ],
)
def test_pubscholar_maps_pdf_links_to_direct_file_url(article: dict, query: str, expected_url: str) -> None:
    provider = PubScholarProvider()
    work = provider._work_from_article(article, AcademicSearchRequest(query=query, provider="pubscholar"))

    assert work.url == expected_url
    assert work.raw["pdf_url"] == work.url
    assert work.full_text_status == "pdf_text_extractable"


def test_pubscholar_extract_pdf_text_reads_selectable_text(monkeypatch) -> None:
    class FakePage:
        def extract_text(self) -> str:
            return "Artificial intelligence ocean forecasting text"

    class FakeReader:
        def __init__(self, _stream):
            self.pages = [FakePage()]

    monkeypatch.setattr("pypdf.PdfReader", FakeReader)

    result = extract_pdf_text(b"%PDF-1.7")

    assert result["page_count"] == 1
    assert "Artificial intelligence" in result["text"]


def test_sciengine_normalizes_article_urls() -> None:
    assert (
        normalize_sciengine_article_url("https://www.sciengine.com/doi/articleIndex/10.16507/j.issn.1006-6055.2025.11.008")
        == "https://www.sciengine.com/doi/articleIndex/10.16507/j.issn.1006-6055.2025.11.008"
    )
    assert (
        normalize_sciengine_article_url("https://www.sciengine.com/globesci/articleIndex?doi=10.16507/j.issn.1006-6055.2025.11.008&scroll=")
        == "https://www.sciengine.com/doi/articleIndex/10.16507/j.issn.1006-6055.2025.11.008"
    )
    assert (
        normalize_sciengine_article_url("https://www.sciengine.com/doi/10.3724/j.issn.1000-3045.20250226003")
        == "https://www.sciengine.com/doi/articleIndex/10.3724/j.issn.1000-3045.20250226003"
    )


def test_sciengine_search_url_uses_real_search_endpoint() -> None:
    urls = SciEngineProvider().search_urls(AcademicSearchRequest(query="人工智能", provider="sciengine"))

    assert urls[0].startswith("https://www.sciengine.com/search/search?")
    assert "queryField_a=" in urls[0]


def test_sciengine_extract_pdf_text_reads_selectable_text(monkeypatch) -> None:
    class FakePage:
        def extract_text(self) -> str:
            return "技术领导力视角下美国人工智能竞争战略研究"

    class FakeReader:
        def __init__(self, _stream):
            self.pages = [FakePage()]

    monkeypatch.setattr("pypdf.PdfReader", FakeReader)

    result = extract_sciengine_pdf_text(b"%PDF-1.7")

    assert result["page_count"] == 1
    assert "人工智能竞争战略" in result["text"]
