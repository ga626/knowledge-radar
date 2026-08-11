from runtime.recruitment_address import normalize_recruitment_address


def test_zhilian_style_location_extracts_street_precision() -> None:
    result = normalize_recruitment_address({"area": "杭州·余杭·五常"}, platform="智联招聘")

    assert result["location_raw"] == "杭州·余杭·五常"
    assert result["city"] == "杭州"
    assert result["district"] == "余杭区"
    assert result["street_or_area"] == "五常"
    assert result["address_precision"] == "street"
    assert result["address_confidence"] == "high"
    assert result["district_claim_allowed"] is True


def test_liepin_style_location_extracts_district_precision() -> None:
    result = normalize_recruitment_address({"area": "杭州-滨江区"}, platform="猎聘")

    assert result["city"] == "杭州"
    assert result["district"] == "滨江区"
    assert result["address_precision"] == "district"
    assert result["district_claim_allowed"] is True


def test_boss_city_only_location_does_not_allow_district_claim() -> None:
    result = normalize_recruitment_address({"area": "杭州"}, platform="BOSS直聘")

    assert result["city"] == "杭州"
    assert result["district"] == ""
    assert result["address_precision"] == "city"
    assert result["district_claim_allowed"] is False


def test_boss_native_district_fields_extract_street_precision() -> None:
    result = normalize_recruitment_address(
        {
            "cityName": "杭州",
            "areaDistrict": "西湖区",
            "businessDistrict": "翠苑",
        },
        platform="BOSS直聘",
    )

    assert result["location_raw"] == "杭州·西湖区·翠苑"
    assert result["city"] == "杭州"
    assert result["district"] == "西湖区"
    assert result["street_or_area"] == "翠苑"
    assert result["address_precision"] == "street"
    assert result["district_claim_allowed"] is True


def test_multi_city_location_does_not_allow_district_claim() -> None:
    result = normalize_recruitment_address({"area": "北京/上海/杭州/深圳"}, platform="猎聘", default_city="杭州")

    assert result["city"] == "杭州"
    assert result["district"] == ""
    assert result["address_precision"] == "unknown"
    assert result["district_claim_allowed"] is False


def test_remote_location_does_not_allow_district_claim() -> None:
    result = normalize_recruitment_address({"area": "远程"}, platform="智联招聘")

    assert result["address_precision"] == "unknown"
    assert result["district_claim_allowed"] is False


def test_non_structured_recruitment_platform_never_allows_district_claim() -> None:
    result = normalize_recruitment_address({"area": "杭州·余杭·五常"}, platform="V2EX")

    assert result["district"] == "余杭区"
    assert result["district_claim_allowed"] is False


def test_maimai_open_signal_never_allows_district_claim() -> None:
    result = normalize_recruitment_address({"area": "杭州·滨江·长河"}, platform="脉脉")

    assert result["district"] == "滨江区"
    assert result["district_claim_allowed"] is False


def test_full_address_text_preserves_detail_address() -> None:
    result = normalize_recruitment_address({"area": "杭州市余杭区五常街道文一西路969号"}, platform="猎聘")

    assert result["city"] == "杭州"
    assert result["district"] == "余杭区"
    assert result["address_precision"] == "full_address"
    assert result["address_text"] == "杭州市余杭区五常街道文一西路969号"
    assert result["district_claim_allowed"] is True


def test_street_level_location_is_not_full_address() -> None:
    result = normalize_recruitment_address({"area": "杭州·余杭·余杭街道"}, platform="智联招聘")

    assert result["city"] == "杭州"
    assert result["district"] == "余杭区"
    assert result["street_or_area"] == "余杭街道"
    assert result["address_precision"] == "street"
    assert result["address_text"] == ""
    assert result["district_claim_allowed"] is True
