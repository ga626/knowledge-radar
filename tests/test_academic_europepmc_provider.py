from academic_providers.europepmc import EuropePmcProvider
from academic_providers.models import AcademicSearchRequest
from academic_providers.service import _provider_order


def test_europepmc_maps_open_access_pdf_and_html_urls(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "resultList": {
                    "result": [
                        {
                            "title": "Machine learning for cancer prediction",
                            "authorString": "Chen W, Liu X.",
                            "pubYear": "2026",
                            "doi": "10.1186/example",
                            "abstractText": "<h4>Background</h4>Machine learning predicts cancer outcomes.",
                            "journalTitle": "Cancer Cell International",
                            "isOpenAccess": "Y",
                            "inEPMC": "Y",
                            "inPMC": "Y",
                            "pmid": "42210259",
                            "pmcid": "PMC13220599",
                            "fullTextUrlList": {
                                "fullTextUrl": [
                                    {
                                        "availabilityCode": "OA",
                                        "documentStyle": "html",
                                        "site": "Europe_PMC",
                                        "url": "https://europepmc.org/articles/PMC13220599",
                                    },
                                    {
                                        "availabilityCode": "OA",
                                        "documentStyle": "pdf",
                                        "site": "Europe_PMC",
                                        "url": "https://europepmc.org/articles/PMC13220599?pdf=render",
                                    },
                                ]
                            },
                        }
                    ]
                }
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, endpoint, params):
            captured["endpoint"] = endpoint
            captured["params"] = params
            return FakeResponse()

    monkeypatch.setattr("academic_providers.europepmc.httpx.Client", FakeClient)

    results = EuropePmcProvider(endpoint="https://example.org/search").search(AcademicSearchRequest(query="machine learning cancer", limit=1))

    assert captured["params"]["resultType"] == "core"
    assert captured["params"]["format"] == "json"
    assert results[0].source_database == "europepmc"
    assert results[0].url.endswith("?pdf=render")
    assert results[0].full_text_status == "pdf_link_found"
    assert results[0].abstract == "Background. Machine learning predicts cancer outcomes."
    assert results[0].raw["pmcid"] == "PMC13220599"


def test_europepmc_is_profiled_for_biomed_auto_route() -> None:
    order = _provider_order(AcademicSearchRequest(query="clinical machine learning cancer", provider="auto"), "auto")

    assert "europepmc" in order
    assert order.index("europepmc") > order.index("openalex")
    if "semanticscholar" in order:
        assert order.index("europepmc") < order.index("semanticscholar")


def test_open_access_pmc_exposes_official_xml_before_landing_pages():
    from academic_providers.fulltext import extract_academic_fulltext
    import httpx
    from unittest.mock import patch

    work = EuropePmcProvider()._work_from_europepmc({
        "title": "Test article", "pmcid": "PMC7092803", "isOpenAccess": "Y",
        "fullTextUrlList": {"fullTextUrl": [{"availabilityCode": "OA", "documentStyle": "html",
            "url": "https://europepmc.org/articles/PMC7092803"}]},
    })
    endpoint = "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC7092803/fullTextXML"
    seen = []
    def fetch(url, **kwargs):
        seen.append(url)
        return httpx.Response(200, headers={"content-type": "application/xml"},
            text="<article><body><sec><title>Results</title><p>" + "Actual body. " * 70 +
                 "</p></sec></body></article>", request=httpx.Request("GET", url))
    with patch("generic_web.url_policy.guarded_fetch", fetch):
        result = extract_academic_fulltext("europepmc", work.url, work=work)
    assert seen == [endpoint]
    assert result.status == "PASS" and result.mode == "public_xml_text"
    assert result.locators and result.locators[-1]["section"] == "Results"


def test_pmc_identifier_does_not_imply_open_access_or_allow_path_injection():
    for pmcid, access in [("PMC7092803", "N"), ("PMC7092803/other", "Y"), ("", "Y")]:
        work = EuropePmcProvider()._work_from_europepmc({"pmcid": pmcid, "isOpenAccess": access})
        assert not any("fullTextXML" in row.get("url", "") for row in work.raw["full_text_urls"])
