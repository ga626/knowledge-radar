"""核对应用合同、MCP投影及来源目录；不启动执行内核或平台业务。"""
from pathlib import Path
import asyncio
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def normalize_schema(schema, container=""):
    if isinstance(schema, list):
        return [normalize_schema(x) for x in schema]
    if isinstance(schema, dict):
        return {k: (v if k in {'default', 'const', 'enum', 'examples'} else normalize_schema(v, k))
                for k, v in schema.items()
                if k != 'title' or container in {'properties', '$defs', 'patternProperties', 'dependentSchemas'}}
    return schema


def business_output(schema):
    # FastMCP wraps non-BaseModel output in result; definitions remain on the envelope.
    if set(schema.get('properties', {})) == {'result'} and schema.get('required') == ['result']:
        return {**schema['properties']['result'], **({'$defs': schema['$defs']} if '$defs' in schema else {})}
    return schema


def validate_declarations(declaration, operations, registry=None, first_wave=()):
    from strategy_trees.registry import build_strategy_tree_bundle
    issues, aliases = [], set()
    ecologies = declaration['ecologies']
    tree_ids = {t['tree_id'] for t in build_strategy_tree_bundle()['trees']}
    # 独立业务底线，防止目录与生成物一起误删而仍通过。
    required_paths = {
        'github_repository_ecology': {'get_content_detail'},
        'wechat_public_article_ecology': {'get_content_detail'},
        'academic_literature_ecology': {'search_academic', 'get_content_detail', 'get_task_status', 'read_knowledge'},
        'career_recruitment_ecology': {'search_recruitment'},
    }
    for key, required in required_paths.items():
        if not required <= set(ecologies.get(key, {}).get('candidate_tools', [])):
            issues.append(f'{key}: required capability path missing')
    academic_flows = ecologies.get('academic_literature_ecology', {}).get('workflows', [])
    if not any(f.get('tool') == 'get_content_detail' and 'academic_reference' in f.get('arguments', {}) for f in academic_flows):
        issues.append('academic_literature_ecology: independent fulltext workflow missing')
    for key, item in ecologies.items():
        names = item['candidate_tools']
        for tool in names:
            if tool not in operations:
                issues.append(f'{key}: unknown tool {tool}')
        if len(names) != len(set(names)) or not set(item.get('detail_tools', [])) <= set(names):
            issues.append(f'{key}: invalid tool relations')
        for alias in item['platform_aliases']:
            if alias.casefold() in aliases:
                issues.append(f'{key}: duplicate platform alias {alias}')
            aliases.add(alias.casefold())
        if not item.get('strategy_trees') or not set(item['strategy_trees']) <= tree_ids:
            issues.append(f'{key}: missing strategy coverage')
        for flow in item.get('workflows', []):
            try:
                operations[flow['tool']].validate(flow['arguments'])
            except (KeyError, ValueError, TypeError) as exc:
                issues.append(f'{key}: invalid workflow {flow["stage"]}: {type(exc).__name__}')
    if set(first_wave) - set(ecologies):
        issues.append('first_wave_unknown_ecology')
    if registry is not None:
        # Validate the supplied declaration, so removal cannot pass using a global copy.
        for platform in registry.platforms():
            if platform.casefold() not in aliases:
                issues.append(f'{platform}: unmapped registered platform')
    return issues


def validate_tool_schemas(tools=None, cards=None):
    import server
    from runtime.application import OPERATIONS, catalog
    from capability_manifest.ecologies import source_ecology_manifest
    from runtime.composition import _FIRST_WAVE_SAFE_ECOLOGIES
    tools = asyncio.run(server.mcp.list_tools()) if tools is None else tools
    cards = catalog(full=True)['tools'] if cards is None else cards
    by_name = {card['name']: card for card in cards}
    issues = []
    for tool in tools:
        card = by_name.get(tool.name)
        if card is None:
            issues.append(tool.name + ': not registered')
            continue
        if normalize_schema(tool.inputSchema) != normalize_schema(card['input_schema']):
            issues.append(tool.name + ': input_schema_drift')
        if normalize_schema(business_output(tool.outputSchema or {})) != normalize_schema(card['output_schema']):
            issues.append(tool.name + ': output_schema_drift')
        if not tool.description or not card.get('error_schema'):
            issues.append(tool.name + ': missing purpose/error contract')
    if set(by_name) != {t.name for t in tools} or len(tools) != len({t.name for t in tools}):
        issues.append('registry_drift')
    from platform_adapters.factory import register_default_adapters
    from kr_core.registry import PlatformRegistry
    registry = register_default_adapters(target_registry=PlatformRegistry())
    issues.extend(validate_declarations(source_ecology_manifest(), OPERATIONS, registry, _FIRST_WAVE_SAFE_ECOLOGIES))
    return {'status': 'FAIL' if issues else 'PASS', 'issues': issues, 'tool_names': [t.name for t in tools]}


if __name__ == '__main__':
    import json
    result = validate_tool_schemas()
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(result['status'] != 'PASS')
