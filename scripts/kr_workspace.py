"""只读查看Codex研究账本、固定证据与显式导出；不启动第二条研究流程。"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', type=Path, help='已有MCP运行态根；只读，不更改全局配置')
    parser.add_argument('--evidence-root', type=Path, help='已有固定证据根；只读')
    parser.add_argument('--payload-file', type=Path, help='UTF-8 JSON查询参数或导出授权')
    parser.add_argument('--task-id', default='')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--approved-root', type=Path)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--legacy-store', type=Path)
    parser.add_argument('action', choices=['list','inspect','search','fetch','export','materialize','migration-plan','migration-apply'])
    args = parser.parse_args(argv)
    payload = json.loads(args.payload_file.read_text(encoding='utf-8-sig')) if args.payload_file else {}
    if args.action in {'migration-plan', 'migration-apply'}:
        if not args.legacy_store: parser.error('migration-plan requires --legacy-store')
        from kr_core.evidence_migration import migration_plan
        if args.action == 'migration-plan':
            result = migration_plan(args.legacy_store)
        else:
            from kr_core.evidence_migration import apply_migration
            from kr_core.evidence_assets import EvidenceAssets
            if not args.evidence_root: parser.error('migration-apply requires --evidence-root and approved payload')
            result = apply_migration(args.legacy_store, EvidenceAssets(args.evidence_root), expected_source_sha256=payload.get('source_sha256'), acknowledged=payload.get('migration_acknowledged'))
    elif args.action == 'materialize':
        from runtime.evidence_export import materialize_bundle
        if not args.output or not args.bundle or not args.approved_root: parser.error('materialize requires --output --bundle --approved-root')
        result = materialize_bundle(args.bundle.read_bytes(), args.output, approved_root=args.approved_root)
    else:
        from runtime.research_workspace import ResearchRecords
        from kr_core.evidence_assets import EvidenceAssets
        from onboarding.research_api import workspace_api
        assets = EvidenceAssets(args.evidence_root or (args.state_root / 'evidence_assets' if args.state_root else None), read_only=True)
        records = ResearchRecords(args.state_root, assets=assets)
        action = {'search':'evidence_search','fetch':'evidence_fetch'}.get(args.action, args.action)
        result = workspace_api(records, {**payload,'action':action,'task_id':args.task_id})
        if args.action == 'export':
            import base64
            from runtime.evidence_export import write_bundle
            if not args.output: parser.error('export requires --output')
            result = write_bundle(base64.b64decode(result['data_base64']), args.output)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(json.dumps({'status':'rejected','error':str(error)},ensure_ascii=False))
        raise SystemExit(2)
