"""跨平台内容的确定性单位与步骤预算。"""
from __future__ import annotations
import re
import hashlib
import unicodedata


def duration_seconds(value):
    if isinstance(value, (float, int)):
        return max(0, float(value))
    text = str(value or "").strip()
    if text.isdecimal():
        return int(text)
    if re.fullmatch(r"\d+(?::\d{1,2}){1,2}", text):
        result = 0
        for part in text.split(":"):
            result = result * 60 + int(part)
        return result
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", text)
    if match and any(match.groups()):
        return sum(float(x or 0) * m for x, m in zip(match.groups(), [86400, 3600, 60, 1]))
    return None


def _edit_distance(left, right):
    previous = list(range(len(right) + 1))
    for i, item in enumerate(left, 1):
        current = [i]
        for j, other in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (item != other)))
        previous = current
    return previous[-1]


def natural_speech_assessment(rows, annotations):
    """词错率是测量值；自然语音验收还需同音频/转写的人工关键项复核。"""
    indexed = {row["id"]: row for row in annotations.get("samples", [])}
    output = []
    for row in rows:
        annotation = indexed.get(row["id"], {})
        hypothesis = str(row.get("hypothesis") or "")
        identity_matches = bool(row.get("audio_sha256")) and row["audio_sha256"] == annotation.get("audio_sha256")
        transcript_hash = hashlib.sha256(hypothesis.encode("utf-8")).hexdigest()
        human_gold = bool(identity_matches and annotation.get("human_review_status") == "APPROVED" and annotation.get("human_reference")
                          and annotation.get("reviewer") and annotation.get("reviewed_transcript_sha256") == transcript_hash)
        reference = str(annotation["human_reference"] if human_gold else row.get("reference") or annotation.get("corpus_reference") or "")
        normalized = [unicodedata.normalize("NFKC", text).casefold() for text in (reference, hypothesis)]
        chars = [[char for char in text if char.isalnum()] for text in normalized]
        words = [re.findall(r"\w+(?:['’]\w+)*", text) for text in normalized]
        critical = annotation.get("critical_review") or {}
        statuses = [critical.get(key, "PENDING") for key in ("numbers", "proper_names", "meaning")]
        verdict = "FAIL" if human_gold and "FAIL" in statuses else "PASS" if human_gold and all(status in {"PASS", "NOT_APPLICABLE"} for status in statuses) else "INCOMPLETE"
        output.append({"id": row["id"], "round": row.get("round"), "verdict": verdict,
                       "reference_kind": "human_gold" if human_gold else "corpus_reference",
                       "identity_matches": identity_matches, "transcript_sha256": transcript_hash,
                       "cer": _edit_distance(*chars) / len(chars[0]) if chars[0] else None,
                       "wer": _edit_distance(*words) / len(words[0]) if words[0] and row.get("language") == "en" else None,
                       "critical_review": dict(zip(("numbers", "proper_names", "meaning"), statuses)),
                       "review_status": annotation.get("human_review_status", "PENDING")})
    verdicts = [row["verdict"] for row in output]
    return {"schema": "knowledgeradar-natural-speech-quality/v1", "status": "FAIL" if "FAIL" in verdicts else "PASS" if verdicts and all(v == "PASS" for v in verdicts) else "INCOMPLETE",
            "samples": output, "boundary": "语料参考测量不能冒充真人复标；不从WER自动推断句意正确或关键字正确。"}
