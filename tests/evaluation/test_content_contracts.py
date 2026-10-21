import pytest
from kr_core.content_contracts import duration_seconds
from collectors.platform.zhilian_parser import parse_job_html, normalize_jobs
from runtime.xhs_candidates import normalize_xhs_image_assets


@pytest.mark.parametrize("raw,expected", [(123,123),("04:03",243),("1:02:03",3723),("PT1H2M3S",3723),("P1DT2S",86402),("unknown",None)])
def test_duration_in_seconds(raw,expected):
    assert duration_seconds(raw)==expected


def test_job_links_not_homepage():
    html = '<a href="https://www.zhaopin.com/">智联招聘</a><article class="job-card"><a href="https://jobs.zhaopin.com/CC123456789J00123456789.htm">Python开发工程师</a><span>20K-30K</span></article>'
    jobs=parse_job_html(html)
    assert len(jobs)==1 and jobs[0]["content_type"]=="job_posting"
    assert not normalize_jobs([{"title":"首页","url":"https://jobs.zhaopin.com/"}])


def test_images_require_content_ownership():
    assets, images, quality=normalize_xhs_image_assets({"note_id":"current","image_assets":[
        {"url":"https://example.org/body.png","role":"content","note_id":"current"},
        {"url":"https://example.org/recommendation.png","role":"content","note_id":"other"},
        {"url":"https://example.org/avatar.png","role":"unknown"}]})
    assert images==["https://example.org/body.png"]
    assert quality["noise_count"]==1


def test_transcript_cache_policy_and_integrity(tmp_path,monkeypatch):
    from collectors.platform.bilibili import _write_transcript_cache,_read_transcript_cache
    path=str(tmp_path/"BVfixture_transcript.txt")
    _write_transcript_cache(path,"固定中文字幕 / English sample")
    assert _read_transcript_cache(path)=="固定中文字幕 / English sample"
    monkeypatch.setenv("KR_ASR_LANGUAGE","en")
    assert _read_transcript_cache(path)==""


def test_asr_cache_rejects_changed_audio_and_model(tmp_path, monkeypatch):
    from collectors.platform.bilibili import _write_transcript_cache,_read_transcript_cache
    from runtime import asset_identity
    model={'sha256':'first-model'}
    monkeypatch.setattr(asset_identity,'model_identity',lambda _:dict(model))
    audio=tmp_path/'fixture.wav';audio.write_bytes(b'original audio')
    path=str(tmp_path/'fixture_transcript.txt')
    _write_transcript_cache(path,'known words',kind='asr',audio_path=str(audio))
    assert _read_transcript_cache(path)=='known words'
    model['sha256']='changed-model'
    assert _read_transcript_cache(path)==''
    model['sha256']='first-model'
    audio.write_bytes(b'replaced audio')
    assert _read_transcript_cache(path)==''


def test_natural_speech_requires_bound_gold_and_critical_review():
    import hashlib
    from kr_core.content_contracts import natural_speech_assessment
    row = {"id": "en-fixture", "language": "en", "audio_sha256": "audio", "reference": "the word Sie", "hypothesis": "the world say"}
    annotation = {"id": row["id"], "audio_sha256": "audio", "corpus_reference": row["reference"]}
    result = natural_speech_assessment([row], {"samples": [annotation]})
    assert result["status"] == "INCOMPLETE" and result["samples"][0]["reference_kind"] == "corpus_reference"
    annotation.update(human_review_status="APPROVED", human_reference=row["reference"], reviewer="fixture-reviewer",
                      reviewed_transcript_sha256=hashlib.sha256(row["hypothesis"].encode()).hexdigest(),
                      critical_review={"numbers": "NOT_APPLICABLE", "proper_names": "FAIL", "meaning": "FAIL"})
    assert natural_speech_assessment([row], {"samples": [annotation]})["status"] == "FAIL"
    row["hypothesis"] = "the word Sie"
    assert natural_speech_assessment([row], {"samples": [annotation]})["status"] == "INCOMPLETE"


def test_empty_speech_evaluation_cannot_pass():
    from kr_core.content_contracts import natural_speech_assessment
    assert natural_speech_assessment([], {"samples": []})["status"] == "INCOMPLETE"
