from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_mcp_tool_schema import validate_tool_schemas


def test_public_mcp_tool_schemas_are_model_facing_contracts() -> None:
    result = validate_tool_schemas()

    assert result["status"] == "PASS", result["issues"]
    assert {"health_check", "get_capabilities", "search_xiaohongshu", "search_zhihu"} <= set(result["tool_names"])


def test_schema_checker_rejects_changed_input_and_output_fields():
    import asyncio
    from copy import deepcopy
    import server
    tools = asyncio.run(server.mcp.list_tools())
    changed = deepcopy(tools)
    changed[0].inputSchema['properties']['invented_parameter'] = {'type': 'string'}
    assert any('input_schema_drift' in x for x in validate_tool_schemas(changed)['issues'])
    changed = deepcopy(tools)
    target = next(t for t in changed if t.name == 'get_content_detail')
    target.outputSchema['properties']['content'] = {'type': 'integer'}
    assert any('output_schema_drift' in x for x in validate_tool_schemas(changed)['issues'])


def test_catalog_rejects_missing_ecology_tool_and_invalid_fulltext_template():
    from copy import deepcopy
    from capability_manifest.ecologies import source_ecology_manifest
    from runtime.application import OPERATIONS
    from scripts.check_mcp_tool_schema import validate_declarations
    data = source_ecology_manifest()
    data['ecologies']['github_repository_ecology']['candidate_tools'].append('missing_tool')
    assert any('unknown tool' in x for x in validate_declarations(data, OPERATIONS))
    data = source_ecology_manifest()
    flow = data['ecologies']['academic_literature_ecology']['workflows'][1]
    flow['arguments']['academic_reference'] = 'invalid-reference'
    assert any('invalid workflow' in x for x in validate_declarations(data, OPERATIONS))


def test_registry_source_coverage_rejects_removed_career_ecology():
    from capability_manifest.ecologies import source_ecology_manifest, joined_catalog
    from kr_core.registry import PlatformRegistry
    from platform_adapters.factory import register_default_adapters
    from runtime.application import OPERATIONS
    from scripts.check_mcp_tool_schema import validate_declarations
    registry = register_default_adapters(target_registry=PlatformRegistry())
    view = joined_catalog(registry)
    assert view['registered_platforms']['脉脉']['source_contract']['native_outputs'] != ['job_card']
    data = source_ecology_manifest()
    del data['ecologies']['career_recruitment_ecology']
    assert any('unmapped registered platform' in x for x in validate_declarations(data, OPERATIONS, registry))


def test_unknown_source_and_global_error_do_not_become_web_observations():
    from capabilities import capability_atlas_manifest
    atlas = capability_atlas_manifest(decision_log_summary={'by_platform': {'new_platform': 2, 'YouTube': 3},
        'failure_tags': {'by_tag': {'network': 1}}}, include_runtime_observations=True)
    assert atlas['unmapped_observation']['platforms'] == {'new_platform': 2}
    assert atlas['global_observation']['scope'] == 'global'
    assert not atlas['cards']['youtube_video_ecology']['runtime_observation']['failure_tags']
    assert not atlas['cards']['generic_web_ecology']['runtime_observation']


def test_result_contract_rejects_invalid_common_fields_but_keeps_extensions():
    import pytest
    from pydantic import TypeAdapter, ValidationError
    from runtime.agent_contracts import DetailResult
    adapter = TypeAdapter(DetailResult)
    with pytest.raises(ValidationError):
        adapter.validate_python({'accepted': 'yes', 'evidence_assets': [{'version_id': 123}]})
    value = adapter.validate_python({'content': 'text', 'platform_field': {'native': True}})
    result = adapter.dump_python(value, mode='json', exclude_unset=True)
    assert result == {'content': 'text', 'platform_field': {'native': True}}


def test_candidate_catalog_uses_own_contract_and_independent_minimum():
    from types import SimpleNamespace
    from scripts.kr_candidate_acceptance import catalog_identity
    names = ['health_check', 'get_capabilities', 'get_content_detail', 'read_knowledge', 'control_task']
    contract = {'tool_count': len(names), 'tools': [{'name': x} for x in names]}
    tools = [SimpleNamespace(name=x) for x in names]
    assert catalog_identity(tools, contract)
    assert not catalog_identity(tools[:-1] + [SimpleNamespace(name='invented')], contract)
    empty = {'tool_count': 0, 'tools': []}
    assert not catalog_identity([], empty)


def test_strategy_tool_typo_is_an_error_even_for_empty_surface():
    from strategy_trees.registry import build_strategy_tree_bundle, validate_strategy_tree_bundle
    bundle = build_strategy_tree_bundle()
    bundle['trees'][0]['tool_name'] = 'missing_tool'
    report = validate_strategy_tree_bundle(bundle, actual_tools=[])
    assert report['status'] == 'FAIL'
    assert any(x['type'] == 'tool_not_in_actual_surface' for x in report['errors'])


def test_inventory_ignores_mtime_but_tracks_semantic_changes(tmp_path, monkeypatch):
    import os
    import scripts.generate_script_lifecycle_inventory as generator
    path = tmp_path / 'scripts/a.py'
    path.parent.mkdir()
    path.write_text('pass')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text('{}')
    monkeypatch.setattr(generator, 'ROOT', tmp_path)
    monkeypatch.setattr(generator, 'MANIFEST', manifest)
    monkeypatch.setattr(generator, '_discover_files', lambda: [path])
    monkeypatch.setattr(generator, '_reference_counts', lambda paths: {})
    before = generator.build_inventory()
    os.utime(path, (1000000000, 1000000000))
    assert before == generator.build_inventory()
    monkeypatch.setattr(generator, '_reference_counts', lambda paths: {'scripts/a.py': 2})
    assert before != generator.build_inventory()


def test_current_docs_check_rejects_tamper_and_unknown_example(tmp_path):
    import shutil
    from scripts.check_current_docs import check
    for name in ['config/codex-product/tool-contracts.json', 'config/codex-product/TOOL_REFERENCE.md',
                 'src/capability_manifest/ecologies.py', 'docs/CAPABILITIES.md']:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    assert check(tmp_path)['status'] == 'PASS'
    path = tmp_path / 'docs/CAPABILITIES.md'
    path.write_text(path.read_text(encoding='utf-8') + '\nchanged', encoding='utf-8')
    assert check(tmp_path)['status'] == 'FAIL'
    (tmp_path / 'docs/EXAMPLE.md').write_text('```kr-tool-example\n{"tool":"missing_tool","arguments":{}}\n```', encoding='utf-8')
    assert any('invalid tool example' in x for x in check(tmp_path)['issues'])


def test_pure_docs_ci_returns_failure_for_broken_documentation(tmp_path):
    from scripts.kr_eval.ci import run_documentation_checks
    script = tmp_path / 'scripts/check_current_docs.py'
    script.parent.mkdir()
    script.write_text('raise SystemExit(1)', encoding='utf-8')
    result = run_documentation_checks(tmp_path)
    assert result['status'] == 'FAIL' and result['business_tests'] == 'not_run'


def test_wechat_paused_comments_never_invoke_reader_browser(monkeypatch):
    from types import SimpleNamespace
    from detail_strategies import wechat
    from collectors.platform import wechat as collector
    stored = []
    checkpoint = SimpleNamespace(read=lambda: {}, write=lambda data: stored.append(dict(data)))
    monkeypatch.setattr(wechat, '_checkpoint', lambda request: checkpoint)
    monkeypatch.setattr(collector, 'collect_article', lambda *a, **kw: {'body_verified': True, 'content': 'article'})
    monkeypatch.setattr(collector, 'collect_comments', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('browser invoked')))
    request = SimpleNamespace(url='https://mp.weixin.qq.com/s/fixture', options={'include_comments': True})
    value = wechat.collect(request)
    assert value['status'] == 'partial'
    assert not value['comments_attempted'] and not value['comments_complete']
    assert value['comment_gaps'][0]['reason'] == 'paused_by_user'
    assert value['content'] == 'article'


def test_schema_metadata_normalization_preserves_named_title_fields():
    from scripts.check_mcp_tool_schema import normalize_schema
    schema = {'title': 'generated name', 'properties': {'title': {'title': 'field label', 'type': 'string',
              'default': {'title': 'literal data'}}}}
    assert normalize_schema(schema) == {'properties': {'title': {'type': 'string', 'default': {'title': 'literal data'}}}}


def test_required_detail_paths_cannot_be_removed_with_generated_docs():
    from capability_manifest.ecologies import source_ecology_manifest
    from runtime.application import OPERATIONS
    from scripts.check_mcp_tool_schema import validate_declarations
    data = source_ecology_manifest()
    item = data['ecologies']['github_repository_ecology']
    item['candidate_tools'].remove('get_content_detail')
    item['detail_tools'].remove('get_content_detail')
    assert any('required capability path missing' in x for x in validate_declarations(data, OPERATIONS))


def test_runtime_observation_retains_identity_scope_and_log_reference(tmp_path):
    from kr_core.decision_log import DecisionLogger, DecisionLogEvent
    from capabilities import capability_atlas_manifest
    logger = DecisionLogger(str(tmp_path / 'decision.jsonl'))
    logger.record(DecisionLogEvent(event_type='detail', platform='GitHub', url='https://github.com/example/project',
        code_generation='source-sha', runtime_generation='runtime-id', strategy='readme',
        trace={'request_id': 'receipt-one', 'tool_name': 'get_content_detail'}))
    atlas = capability_atlas_manifest(decision_log_summary=logger.summarize(), include_runtime_observations=True)
    card = atlas['cards']['github_repository_ecology']
    record = card['runtime_observation']['records'][0]
    assert record['source_identity']['code_generation'] == 'source-sha'
    assert record['observed_at'] and record['receipt_reference']['request_id'] == 'receipt-one'
    assert record['scope']['strategy'] == 'readme'
    assert record['success'] and not record['verified'] and not card['availability']['verified']
    assert card['maximum_observed_boundary'] is None


def test_typed_business_results_keep_existing_mcp_wrapper_without_null_inflation():
    import server
    from runtime.application import OPERATIONS, invoke
    from runtime.mcp_dispatch import attach_operation
    from mcp.server.fastmcp import FastMCP
    from runtime.agent_contracts import TaskStatusResult
    from runtime.application import register
    def fixture_receipt() -> TaskStatusResult:
        return {'status': 'completed', 'result': {'status': 'authorized', 'grant_id': 'fixed'}}
    fixture_receipt.__annotations__['return'] = TaskStatusResult
    operation = register(fixture_receipt, group='task', read_only=True, publish=False)
    mcp = FastMCP('typed-envelope-test')
    attach_operation(mcp, operation)
    value = invoke('fixture_receipt', {}, operation=operation)
    _, structured = mcp._tool_manager.get_tool('fixture_receipt').fn_metadata.convert_result(value)
    assert structured == {'result': {'status': 'completed', 'result': {'status': 'authorized', 'grant_id': 'fixed'}}}
    assert 'task_id' not in structured['result']
    for name in ['get_content_detail', 'process_local_evidence', 'get_task_status', 'control_task', 'kr_research']:
        assert server.mcp._tool_manager.get_tool(name).fn_metadata.wrap_output
