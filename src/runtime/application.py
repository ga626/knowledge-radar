"""入口中立的应用合同；类型、校验、调用和结果视图只有一个事实源。"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import contextvars
import hashlib
import inspect
import json
import threading
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, create_model


@dataclass
class Operation:
    name: str
    function: Any
    arguments: type[BaseModel]
    result: TypeAdapter
    group: str
    read_only: bool
    description: str

    def validate(self, value):
        parsed = self.arguments.model_validate(value)
        # 只展开参数层，保留嵌套的Pydantic请求类型。
        return {name: getattr(parsed, name) for name in type(parsed).model_fields}

    def card(self):
        return {"name": self.name, "purpose": self.description.split("\n")[0],
                "group": self.group, "read_only": self.read_only,
                "usage": self.description,
                "error_schema": __import__("runtime.agent_contracts", fromlist=["ErrorResponse"]).ErrorResponse.model_json_schema(),
                "input_schema": self.arguments.model_json_schema(),
                "output_schema": self.result.json_schema()}


OPERATIONS: dict[str, Operation] = {}
TOOL_NAMES: list[str] = []
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="kr-short-io")
_PENDING = threading.BoundedSemaphore(24)
_CONTROL_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kr-control")
_CONTROL_PENDING = threading.BoundedSemaphore(8)
_REQUEST = contextvars.ContextVar('kr_application_request', default='')


def current_request_id():
    return _REQUEST.get()


@contextmanager
def request_scope(request_id):
    token = _REQUEST.set(request_id)
    try:
        yield
    finally:
        _REQUEST.reset(token)


def submit_application(name, arguments, *, operation=None):
    """共用执行槽，控制请求独立；等待不会被慢采集占满。"""
    control = name in {"health_check", "get_capabilities", "control_task"} or (name == "get_task_status" and not arguments.get("wait"))
    pending = _CONTROL_PENDING if control else _PENDING
    executor = _CONTROL_EXECUTOR if control else _EXECUTOR
    if not pending.acquire(blocking=False):
        raise ValueError("runtime_busy")
    context = contextvars.copy_context()
    try:
        future = executor.submit(context.run, invoke, name, arguments, operation=operation)
    except BaseException:
        pending.release()
        raise
    future.add_done_callback(lambda _: pending.release())
    return future


def clear():
    OPERATIONS.clear()
    TOOL_NAMES.clear()


def register(function, *, group, read_only, publish=True):
    signature = inspect.signature(function, eval_str=True)
    fields = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}:
            raise ValueError("variadic_application_contract_forbidden")
        annotation = parameter.annotation if parameter.annotation is not parameter.empty else Any
        default = parameter.default if parameter.default is not parameter.empty else ...
        fields[name] = (annotation, default)
    fields['request_id'] = (str, Field(default='', max_length=128, pattern=r'^[A-Za-z0-9_-]*$',
        description='可选的调用者动作ID。写动作重试必须沿用原ID；不同意图用新ID。查询旧回执用get_task_status(receipt_request_id=...)。'))
    if 'research_task_id' in fields and function.__name__ not in {'kr_research','finalize_research_task','review_research_progress'}:
        from runtime.research_contracts import ResearchContext
        fields['research_context'] = (ResearchContext | None, Field(default=None,
            description='可选的研究意图关联：action_id与question_id/requirement_id，或预先登记的exploration_id；不填保持未关联。'))
    arguments = create_model(function.__name__ + "Arguments", __config__=ConfigDict(extra="forbid"), **fields)
    result = signature.return_annotation if signature.return_annotation is not signature.empty else Any
    operation = Operation(function.__name__, function, arguments, TypeAdapter(result),
                          group, read_only, inspect.getdoc(function) or function.__name__)
    if publish:
        OPERATIONS[operation.name] = operation
        if operation.name not in TOOL_NAMES:
            TOOL_NAMES.append(operation.name)
    return operation


def application_tool(*, group=None, read_only=False):
    """直接注册业务合同；不依赖任何传输对象。"""
    def decorate(function):
        selected = group or ('search' if function.__name__.startswith('search_') or function.__name__ == 'kr_web_search'
                             else 'evidence' if 'detail' in function.__name__ or 'extract' in function.__name__ else 'research')
        register(function, group=selected, read_only=read_only)
        return function
    return decorate


def groups():
    result = {}
    for operation in OPERATIONS.values():
        result.setdefault(operation.group, []).append(operation.name)
    return result


def research_workflow_identity():
    from pathlib import Path
    skill = Path(__file__).resolve().parents[2] / 'config/codex-product/plugin/knowledgeradar-research/skills/research/SKILL.md'
    digest = hashlib.sha256(skill.read_bytes()).hexdigest() if skill.is_file() else ''
    return {'skill_sha256': digest, 'packaged_skill': 'present' if digest else 'missing',
            'host_loaded_skill': 'unverified',
            'workflow': 'Declare project and question scope; associate actions; read fixed evidence; assess meaning and gaps; deliver with explicit limits.'}


def catalog(name="", *, full=False):
    cards = [op.card() for _, op in sorted(OPERATIONS.items())]
    digest = hashlib.sha256(json.dumps(cards, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if name:
        found = next((card for card in cards if card["name"] == name), None)
        if found is None:
            raise ValueError("unknown_tool_use_catalog")
        return {"schema": "kr-tool-contract/v1", "contract_hash": digest, "tool": found}
    return {"schema": "kr-tool-catalog/v1", "contract_hash": digest, "tool_count": len(cards),
            "tools": cards if full else [{k: v for k, v in card.items()
                                         if k not in {"input_schema", "output_schema", "error_schema", "usage"}} for card in cards],
            "expand": {"tool": "get_capabilities", "arguments": {"tool_name": "<name>"}},
            "availability": "declared_not_live_probe", "research_workflow": research_workflow_identity()}


def invoke(name, arguments, *, operation=None):
    """返回业务结果或统一错误；执行状态不由传输层猜测。"""
    from runtime.agent_contracts import compact_evidence, error_result
    operation = operation or OPERATIONS.get(name)
    if operation is None:
        return error_result(ValueError("unknown_tool_use_catalog"), name, not_started=True)
    try:
        values = operation.validate(arguments)
        values.pop('request_id', None)
    except Exception as exc:
        return error_result(exc, name, not_started=True)
    try:
        research_context = values.pop('research_context', None)
        from contextlib import nullcontext
        from runtime.research_contracts import context_scope
        scope = context_scope(values.get('research_task_id',''), research_context) if research_context else nullcontext()
        with scope:
            result = operation.function(**values)
        # 各入口都校验相同的业务输出；MCP封装仅负责协议表示。
        validated = operation.result.validate_python(result)
        return compact_evidence(operation.result.dump_python(validated, mode="json", exclude_unset=True))
    except Exception as exc:
        return error_result(exc, name)
