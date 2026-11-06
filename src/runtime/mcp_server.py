"""MCP传输投影；业务装配和执行所有者不导入本模块。"""
from contextlib import asynccontextmanager
from functools import partial
import asyncio
import os


def mount(app):
    from mcp.server.fastmcp import FastMCP
    from runtime.application import OPERATIONS
    from runtime.mcp_dispatch import attach_operation

    @asynccontextmanager
    async def lifespan(_server):
        from runtime.execution_owner import OwnerClient
        owner = await asyncio.to_thread(OwnerClient().start)
        if owner.get('status') != 'ready':
            raise RuntimeError('execution_owner_not_ready_' + owner.get('status', 'unknown'))
        app.mcp._kr_forward_owner = True
        registered = False
        try:
            app.record_server_started(transport=app.MCP_TRANSPORT, tool_names=app.ACTUAL_MCP_TOOLS,
                source_fingerprint=app.source_fingerprint(app.REPO_ROOT),
                invocation_kind='continuity_fallback' if os.environ.get('KR_CONTINUITY_FALLBACK') == '1' else 'native_server',
                invocation_id=os.environ.get('KR_CONTINUITY_INVOCATION_ID', ''))
            registered = True
            yield {}
        finally:
            app.mcp._kr_forward_owner = False
            if registered:
                if os.environ.get('KR_CONTINUITY_FALLBACK') == '1':
                    app.record_fallback_server_stopped(invocation_id=os.environ.get('KR_CONTINUITY_INVOCATION_ID', ''))
                else:
                    app.record_server_stopped(transport=app.MCP_TRANSPORT)

    app._mcp_lifespan = lifespan
    app.mcp = FastMCP('全网知识搜索', log_level='INFO', lifespan=lifespan,
        host=app.MCP_HOST, port=app.MCP_PORT, streamable_http_path=app.MCP_STREAMABLE_HTTP_PATH,
        sse_path=app.MCP_SSE_PATH, message_path=app.MCP_MESSAGE_PATH)
    for operation in list(OPERATIONS.values()):
        attach_operation(app.mcp, operation)
    app._install_mcp_observability = partial(install_observability, app)
    app.main = partial(main, app)
    app._install_mcp_observability()
    return app.mcp


def install_observability(app):
    low_level = app.mcp._mcp_server
    if getattr(low_level, '_kr_observability_installed', False):
        return
    from mcp.server.lowlevel.server import NotificationOptions
    from mcp.types import ListToolsRequest
    create_options = low_level.create_initialization_options

    def options(notification_options=None, experimental_capabilities=None):
        value = notification_options or NotificationOptions()
        value.tools_changed = True
        return create_options(value, experimental_capabilities)

    low_level.create_initialization_options = options
    original = low_level.request_handlers.get(ListToolsRequest)
    if original:
        async def listed(request):
            result = await original(request)
            names = [str(item.name) for item in getattr(getattr(result, 'root', None), 'tools', []) if item.name]
            try:
                session_id = id(low_level.request_context.session)
            except LookupError:
                session_id = ''
            app.record_tool_list(session_id=session_id, tool_names=names, transport=app.MCP_TRANSPORT,
                invocation_kind='continuity_fallback' if os.environ.get('KR_CONTINUITY_FALLBACK') == '1' else 'native_server',
                invocation_id=os.environ.get('KR_CONTINUITY_INVOCATION_ID', ''))
            return result
        low_level.request_handlers[ListToolsRequest] = listed
    low_level._kr_observability_installed = True


def main(app):
    os.environ['KR_RUNTIME_ROLE'] = 'client'
    app._install_mcp_observability()
    app.log.info('KnowledgeRadar MCP transport=%s, tools=%s', app.MCP_TRANSPORT, len(app.ACTUAL_MCP_TOOLS))
    try:
        app.mcp.run(transport=app.MCP_TRANSPORT)
    finally:
        if os.environ.get('KR_CONTINUITY_FALLBACK') == '1':
            app.record_fallback_server_stopped(invocation_id=os.environ.get('KR_CONTINUITY_INVOCATION_ID', ''))
