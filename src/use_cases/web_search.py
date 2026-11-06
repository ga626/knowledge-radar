"""网页发现用例：入口只装配参数，配置快照、缓存与公共证据在此交接。"""
from runtime.search_configuration import configuration_scope
from search_providers.service import search_web


def execute(request, *, cache, record_evidence):
    from runtime.execution_context import current_execution
    with configuration_scope() as configuration:
        execution = current_execution() or {}
        options = {**request.options, 'configuration_generation':configuration.generation,
                   'authority_scope':execution.get('KR_RESEARCH_TASK_ID') or execution.get('KR_DURABLE_TASK_ID', 'interactive')}
        key = cache.key(platform='web', query=request.query, limit=request.limit,
                        provider=request.provider, freshness=request.freshness, language=request.language,
                        include_raw_content=request.include_raw_content, options=options)
        result = cache.run(key, lambda: search_web(request).to_mcp_dict())
        # Each subscriber gets its own evidence/trace association, including a cache hit.
        record_evidence('web', request.query, result)
        return result
