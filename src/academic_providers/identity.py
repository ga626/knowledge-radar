"""作品、版本和固定书目卡；复用公共资产库，不建立第二份学术数据库。"""
from dataclasses import asdict, fields
import hashlib
import json
import re
from .models import AcademicWork, normalize_doi, normalize_title


def arxiv_identifier(value):
    match = re.search(r'(?<![\w.])((?:\d{4}\.\d{4,5}|[a-zA-Z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?)(?!\w)', str(value or ''))
    return match.group(1) if match else ''


def work_identity(work):
    arxiv = arxiv_identifier(work.url) if (work.source_database in {'arxiv', 'ar5iv'} or 'arxiv.org/' in work.url) else ''
    doi = normalize_doi(work.doi)
    key = ('arxiv:' + re.sub(r'v\d+$', '', arxiv) if arxiv else
           'doi:' + doi if doi else 'metadata:' + json.dumps([normalize_title(work.title), work.year, work.authors[:1]], ensure_ascii=False))
    edition = str(work.version or 'unknown')
    if arxiv and re.search(r'v\d+$', arxiv):
        actual = re.search(r'v(\d+)$', arxiv).group(1)
        if edition not in {'unknown', actual, 'v'+actual}:
            raise ValueError('academic_version_url_conflict')
        edition = actual
    discriminator = work.url if edition == 'unknown' else ''
    def digest(value):
        return hashlib.sha256(value.encode('utf-8')).hexdigest()
    return {'work_id': digest(key), 'edition_id': digest(json.dumps([key, edition, work.document_type, discriminator])),
            'declared_version': edition, 'identity_verification': work.verification_status}


def _stable(value):
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in {
            'retrieved_at', 'fulltext_task', 'fulltext_ref', 'query', 'relevance_score', 'score'}}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def save_work(work):
    from kr_core.evidence_assets import EvidenceAssets
    from kr_core.source_urls import public_evidence_payload
    assets = EvidenceAssets()
    data = _stable(asdict(work))
    inline = data['raw'].pop('fulltext_text', None)
    if isinstance(inline, str) and inline.strip() and not any(
            data['raw'].get(key) is False for key in ('read_allowed','download_allowed','evidence_storage_allowed')):
        inline_ref = assets.put(work.url, inline, kind='academic_inline', title=work.title,
            metadata={'extractor': work.source_database, 'coverage': 'provider_supplied_unknown'})
        data['raw']['fulltext_inline_ref'] = inline_ref['version_id']
    data = public_evidence_payload(data)
    text = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if len(text) > 128000:
        raise ValueError('academic_card_too_large')
    ref = assets.put(work.url, text, kind='academic_card', title=work.title,
        metadata={'extractor': 'academic-card-v1', 'coverage': 'metadata_only'})
    return {'version_id': ref['version_id'], **work_identity(work)}


def load_work(reference):
    from kr_core.evidence_assets import EvidenceAssets
    version = reference.get('version_id') if isinstance(reference, dict) else reference
    if not isinstance(version, str) or not re.fullmatch('[a-f0-9]{64}', version):
        raise ValueError('invalid_academic_reference')
    assets = EvidenceAssets()
    first = assets.fetch(version, max_chars=24000)
    if first['asset']['kind'] != 'academic_card' or first['asset']['chars'] > 128000:
        raise ValueError('not_an_academic_card')
    text = first['text']
    while first['next_start'] is not None:
        first = assets.fetch(version, start=first['next_start'], max_chars=24000)
        text += first['text']
    row = json.loads(text)
    return AcademicWork(**{k:v for k,v in row.items() if k in {f.name for f in fields(AcademicWork)}})
