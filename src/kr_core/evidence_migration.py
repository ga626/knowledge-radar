"""旧摘要库显式计划/应用；保留旧原件，绝不把摘要升级为完整证据。"""
from pathlib import Path
import hashlib
import json


def migration_plan(source, *, max_records=10000):
    path = Path(source)
    if path.is_symlink() or path.is_junction():
        raise ValueError('links_not_allowed')
    rows, digest, lines = [], hashlib.sha256(), 0
    with path.open('rb') as handle:
        for line in handle:
            digest.update(line)
            lines += 1
            if lines > max_records:
                raise ValueError('migration_plan_record_limit_exceeded')
            if len(line) > 32 * 1024 * 1024:
                raise ValueError('legacy_record_too_large')
            try:
                record = json.loads(line.decode('utf-8-sig'))
            except (ValueError, UnicodeError):
                rows.append({'line': lines, 'status': 'blocked_invalid_record'})
                continue
            if record.get('record_type') != 'detail':
                continue
            payload = record.get('payload') or {}
            for part in ('content', 'transcript'):
                text = payload.get(part)
                if isinstance(text, str) and text.strip():
                    refs = [r['version_id'] for r in payload.get('evidence_assets', []) if isinstance(r, dict) and r.get('kind') == part and r.get('version_id')]
                    rows.append({'line': lines, 'legacy_id': record.get('id', ''), 'part': part, 'chars': len(text), 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'fixed_versions_to_verify': refs, 'status': 'verify_existing_asset' if refs else 'partial_legacy_excerpt', 'completeness_proven': False, 'action': 'verify_fixed_version' if refs else 'retain_original_then_reacquire_with_permission'})
    return {'schema': 'knowledgeradar-evidence-migration-plan/v1', 'source_sha256': digest.hexdigest(), 'source_lines': lines, 'rows': rows, 'read_only': True, 'automatic_apply': False, 'rollback': 'source unchanged; no asset or profile modifications', 'rights': 'unknown_until_explicitly_reviewed', 'capacity_basis': 'content-byte admission; FTS/SQLite physical usage separately reported'}


def apply_migration(source, assets, *, expected_source_sha256, acknowledged=False):
    if acknowledged is not True:
        raise ValueError('legacy_migration_permission_required')
    path = Path(source)
    plan = migration_plan(path)
    if plan['source_sha256'] != expected_source_sha256:
        raise ValueError('legacy_source_changed_replan_required')
    # 一次读取绑定同一文件身份，不在校验后重新遍历可能增长的账本。
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_source_sha256:
        raise ValueError('legacy_source_changed_replan_required')
    rows = raw.decode('utf-8-sig').splitlines()
    migrated, verified, blocked = [], [], []
    for item in plan['rows']:
        if item['status'] == 'blocked_invalid_record':
            blocked.append(item)
            continue
        if item['fixed_versions_to_verify']:
            for version in item['fixed_versions_to_verify']:
                try:
                    assets.fetch(version, max_chars=1)
                    verified.append(version)
                except (ValueError, OSError) as error:
                    blocked.append({'line':item['line'], 'version_id':version, 'reason':str(error)})
            continue
        record = json.loads(rows[item['line'] - 1])
        value = record['payload'][item['part']]
        ref = assets.put(record.get('url') or '', value, kind=item['part'], title=str(record['payload'].get('title') or ''), metadata={'extractor':'legacy-excerpt/v1', 'coverage':'partial', 'completeness_proven':False, 'legacy_source_sha256':expected_source_sha256})
        assets.reference(ref['version_id'], 'legacy:' + expected_source_sha256)
        migrated.append({'line':item['line'], 'part':item['part'], 'version_id':ref['version_id'], 'coverage':'partial'})
    return {'schema':'knowledgeradar-evidence-migration-receipt/v1', 'source_sha256':expected_source_sha256, 'source_unchanged':True, 'migrated':migrated, 'verified':verified, 'blocked':blocked, 'automatic_apply':False, 'rollback':'source retained; migrated assets remain partial and are not substituted for originals'}
