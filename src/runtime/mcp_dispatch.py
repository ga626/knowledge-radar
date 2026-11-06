"""MCP 异步边界：有限线程接短I/O，保留直接Python调用合同。"""
import asyncio
from functools import wraps
import inspect
import json
import os
from contextvars import ContextVar
from typing import Dict
from runtime.application import submit_application, _PENDING as _PENDING

_RECEIPT = ContextVar('kr_mcp_reply_receipt', default=None)


class ApplicationFailure(Exception):
    def __init__(self, response):
        self.response = response


def install_error_boundary(mcp):
    if getattr(mcp, '_kr_error_boundary', False):
        return
    from mcp.types import CallToolResult, TextContent
    from runtime.agent_contracts import error_result
    original = mcp.call_tool

    async def call(name, arguments):
        _RECEIPT.set(None)
        operation = mcp._kr_operations.get(name)
        try:
            if operation is None:
                raise ValueError('unknown_tool_use_catalog')
            operation.validate({} if arguments is None else arguments)
        except Exception as exc:
            response = error_result(exc, name, not_started=True)
        else:
            try:
                result = await original(name, arguments or {})
                receipt = _RECEIPT.get()
                if not receipt:
                    return result
                if isinstance(result, CallToolResult):
                    result.meta = {**(result.meta or {}), 'kr_execution_receipt': receipt}
                    return result
                content, structured = result if isinstance(result, tuple) else (result, None)
                return CallToolResult(content=content, structuredContent=structured,
                                      _meta={'kr_execution_receipt': receipt})
            except ApplicationFailure as exc:
                response = exc.response
            except Exception as exc:
                current = exc
                while current.__cause__ is not None and not isinstance(current, ApplicationFailure):
                    current = current.__cause__
                response = current.response if isinstance(current, ApplicationFailure) else error_result(exc, name)
        return CallToolResult(isError=True, content=[TextContent(type='text', text=json.dumps(response, ensure_ascii=False))], structuredContent=response,
                              _meta={'kr_execution_receipt': _RECEIPT.get()} if _RECEIPT.get() else None)

    mcp._mcp_server.call_tool(validate_input=False)(call)
    mcp._kr_error_boundary = True


def attach_operation(mcp, operation):
    fn = operation.function
    if not hasattr(mcp, '_kr_operations'):
        mcp._kr_operations = {}
    mcp._kr_operations[fn.__name__] = operation
    @wraps(fn)
    async def dispatch(*args, **kwargs):
        if getattr(mcp, '_kr_forward_owner', False) or os.environ.get('KR_RUNTIME_ROLE') == 'client':
            from runtime.execution_owner import OwnerClient
            client = OwnerClient()
            request_id = kwargs.pop('request_id', '') or None
            value = await asyncio.to_thread(client.call, fn.__name__, kwargs, request_id=request_id)
            _RECEIPT.set(client.last_receipt)
            if fn.__name__ in {'health_check', 'get_capabilities'} and isinstance(value, dict) and value.get('status') != 'error':
                value['execution_owner'] = await asyncio.to_thread(client.status)
                if 'mcp_observability' in value:
                    from runtime.mcp_observability import snapshot
                    from runtime.application import TOOL_NAMES
                    value['mcp_observability'] = snapshot(transport=os.environ.get('KR_MCP_TRANSPORT', 'streamable-http'), tool_names=TOOL_NAMES)
        else:
            value = await asyncio.wrap_future(submit_application(fn.__name__, kwargs, operation=operation))
        if isinstance(value, dict) and value.get('status') == 'error' and 'error' in value:
            raise ApplicationFailure(value)
        return value
    signature = inspect.signature(fn)
    from runtime.agent_contracts import BusinessResult
    typed_business = inspect.isclass(signature.return_annotation) and issubclass(signature.return_annotation, BusinessResult)
    # 业务类型加严不改变既有MCP的result外壳，也不补出业务未返回的默认null。
    dispatch.__signature__ = signature.replace(return_annotation=Dict if typed_business else signature.return_annotation,
        parameters=[*signature.parameters.values(),
        inspect.Parameter('request_id', inspect.Parameter.KEYWORD_ONLY, default='', annotation=str)])
    from mcp.types import ToolAnnotations
    mcp.tool(annotations=ToolAnnotations(readOnlyHint=operation.read_only, destructiveHint=not operation.read_only))(dispatch)
    tool=mcp._tool_manager.get_tool(fn.__name__)
    # MCP是同一类型合同的投影，不再充当合同注册事实源。
    tool.parameters = operation.arguments.model_json_schema()
    if typed_business:
        schema = operation.result.json_schema()
        definitions = schema.pop('$defs', {})
        tool.fn_metadata.output_schema = {'type': 'object', 'properties': {'result': schema}, 'required': ['result'],
                                          **({'$defs': definitions} if definitions else {})}
    install_error_boundary(mcp)
    return fn


def nonblocking_mcp_tool(mcp, *, group=None, read_only=False):
    """供局部协议测试/扩展挂载；不改变公共应用注册表。"""
    def register(fn):
        from runtime.application import register as register_operation
        operation = register_operation(fn, group=group or 'research', read_only=read_only, publish=False)
        return attach_operation(mcp, operation)
    return register
