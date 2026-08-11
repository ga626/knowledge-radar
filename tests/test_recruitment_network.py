from runtime.recruitment_network import extract_recruitment_items_from_payloads


def test_extracts_boss_items_from_nested_network_payload() -> None:
    payload = {
        "zpData": {
            "jobList": [
                {
                    "jobName": "AI产品经理",
                    "brandName": "测试科技",
                    "salaryDesc": "20-35K",
                    "jobArea": "杭州",
                    "encryptJobId": "abc123",
                }
            ]
        }
    }

    items, diagnostics = extract_recruitment_items_from_payloads(
        [{"url": "https://www.zhipin.com/wapi/zpgeek/search/joblist.json", "body": payload}],
        platform="boss",
        keyword="AI产品经理",
        city="杭州",
        limit=3,
    )

    assert diagnostics["payload_count"] == 1
    assert diagnostics["item_count"] == 1
    assert items[0]["title"] == "AI产品经理"
    assert items[0]["company"] == "测试科技"
    assert items[0]["url"] == "https://www.zhipin.com/job_detail/abc123.html"
    assert items[0]["source"] == "boss_network_search"
    assert items[0]["evidence_strength"] == "strong_platform_network"
    assert items[0]["market_claim_allowed"] is True
    assert items[0]["salary_claim_allowed"] is False
    assert items[0]["location_raw"] == "杭州"
    assert items[0]["city"] == "杭州"
    assert items[0]["address_precision"] == "city"
    assert items[0]["district_claim_allowed"] is False


def test_extracts_boss_native_district_and_gps_from_network_payload() -> None:
    payload = {
        "zpData": {
            "jobList": [
                {
                    "jobName": "AI应用产品经理",
                    "brandName": "杭州智能科技",
                    "salaryDesc": "25-40K",
                    "city": "101210100",
                    "cityName": "杭州",
                    "areaDistrict": "西湖区",
                    "businessDistrict": "翠苑",
                    "gps": {"longitude": "120.123456", "latitude": "30.123456"},
                    "encryptJobId": "boss-gps-1",
                }
            ]
        }
    }

    items, diagnostics = extract_recruitment_items_from_payloads(
        [{"url": "https://www.zhipin.com/wapi/zpgeek/search/joblist.json", "body": payload}],
        platform="boss",
        keyword="AI产品经理",
        city="杭州",
        limit=3,
    )

    assert diagnostics["item_count"] == 1
    assert items[0]["area"] == "杭州·西湖区·翠苑"
    assert items[0]["location_raw"] == "杭州·西湖区·翠苑"
    assert items[0]["city"] == "杭州"
    assert items[0]["district"] == "西湖区"
    assert items[0]["street_or_area"] == "翠苑"
    assert items[0]["address_source"] == "network_json"
    assert items[0]["address_precision"] == "street"
    assert items[0]["district_claim_allowed"] is True
    assert items[0]["geo_lng"] == "120.123456"
    assert items[0]["geo_lat"] == "30.123456"


def test_extracts_liepin_items_from_nested_network_payload() -> None:
    payload = {
        "data": {
            "data": {
                "jobCardList": [
                    {
                        "title": "AI平台产品经理",
                        "compName": "杭州样例公司",
                        "salary": "薪资面议",
                        "dq": "杭州",
                        "jobId": "987654321",
                    }
                ]
            }
        }
    }

    items, diagnostics = extract_recruitment_items_from_payloads(
        [{"url": "https://api-c.liepin.com/api/com.liepin.searchfront4c.pc-search-job", "body": payload}],
        platform="liepin",
        keyword="AI",
        city="杭州",
        limit=3,
    )

    assert diagnostics["candidate_count"] == 1
    assert items[0]["title"] == "AI平台产品经理"
    assert items[0]["company"] == "杭州样例公司"
    assert items[0]["url"] == "https://www.liepin.com/job/987654321.shtml"
    assert items[0]["source"] == "liepin_network_search"
    assert items[0]["address_precision"] == "city"
    assert items[0]["district_claim_allowed"] is False


def test_extracts_liepin_items_from_real_job_card_shape() -> None:
    payload = {
        "flag": 1,
        "data": {
            "data": {
                "jobCardList": [
                    {
                        "dataInfo": "%7B%22jobId%22%3A%221983844057%22%7D",
                        "job": {
                            "title": "中后台AI产品经理",
                            "salary": "55-65k·16薪",
                            "dq": "杭州-余杭区",
                            "jobId": "1983844057",
                        },
                        "comp": {
                            "compName": "阿里巴巴集团",
                            "compIndustry": "互联网",
                        },
                        "recruiter": {"recruiterName": "郝先生"},
                    }
                ]
            }
        },
    }

    items, diagnostics = extract_recruitment_items_from_payloads(
        [{"url": "https://api-c.liepin.com/api/com.liepin.searchfront4c.pc-search-job", "body": payload}],
        platform="liepin",
        keyword="AI产品经理",
        city="杭州",
        limit=3,
    )

    assert diagnostics["candidate_count"] == 1
    assert diagnostics["item_count"] == 1
    assert items[0]["title"] == "中后台AI产品经理"
    assert items[0]["company"] == "阿里巴巴集团"
    assert items[0]["salary"] == "55-65k·16薪"
    assert items[0]["area"] == "杭州-余杭区"
    assert items[0]["location_raw"] == "杭州-余杭区"
    assert items[0]["city"] == "杭州"
    assert items[0]["district"] == "余杭区"
    assert items[0]["address_source"] == "network_json"
    assert items[0]["address_precision"] == "district"
    assert items[0]["district_claim_allowed"] is True
    assert items[0]["url"] == "https://www.liepin.com/job/1983844057.shtml"


def test_network_payload_must_match_keyword() -> None:
    payload = {
        "data": {
            "jobCardList": [
                {
                    "title": "3d角色美术师",
                    "compName": "杭州样例公司",
                    "salary": "薪资面议",
                    "dq": "杭州",
                    "jobId": "987654321",
                }
            ]
        }
    }

    items, diagnostics = extract_recruitment_items_from_payloads(
        [{"url": "https://api-c.liepin.com/api/com.liepin.searchfront4c.pc-search-job", "body": payload}],
        platform="liepin",
        keyword="AI产品经理",
        city="杭州",
        limit=3,
    )

    assert items == []
    assert diagnostics["item_count"] == 0


def test_network_payload_requires_distinctive_keyword_tokens() -> None:
    payload = {
        "data": {
            "data": {
                "jobCardList": [
                    {
                        "job": {
                            "title": "公安反诈大销售经理",
                            "salary": "15-25k",
                            "dq": "杭州-上城区",
                            "jobId": "1983904397",
                        },
                        "comp": {"compName": "360"},
                    },
                    {
                        "job": {
                            "title": "AI产品经理（大模型场景应用方向）",
                            "salary": "20-40k",
                            "dq": "杭州",
                            "jobId": "1983111243",
                        },
                        "comp": {"compName": "浙江省北大信息技术高等研究院"},
                    },
                ]
            }
        }
    }

    items, diagnostics = extract_recruitment_items_from_payloads(
        [{"url": "https://api-c.liepin.com/api/com.liepin.searchfront4c.pc-search-job", "body": payload}],
        platform="liepin",
        keyword="大模型产品经理",
        city="杭州",
        limit=3,
    )

    assert diagnostics["item_count"] == 1
    assert items[0]["title"] == "AI产品经理（大模型场景应用方向）"
