"""授权本地材料的任务交接；解析、费用和组件校验仍由既有运行层执行。"""
import re


def request_local_evidence(
    *, input_path="", approved_input_root="", input_sha256="", component="auto",
    language="auto", research_task_id="", task_id="", document=None, timeout_s=600, task_limits=None,
):
    from runtime.task_service import TaskService

    service = TaskService()
    if task_id:
        value = service.result(task_id)
        result = value.get("result") or {}
        return {**value, "evidence_assets": result.get("evidence_assets", [])}
    if not input_path or not approved_input_root or not re.fullmatch(r"[a-f0-9]{64}", input_sha256):
        raise ValueError("explicit_input_root_and_sha256_required")
    from runtime.read_options import DocumentReadOptions, TaskLimitOptions, normalized_options
    options=normalized_options(DocumentReadOptions,document)
    payload = {"document":options,"timeout_s":timeout_s,"input_path": input_path, "approved_input_root": approved_input_root,
               "input_sha256": input_sha256, "component": component, "language": language}
    from runtime.optional_media import select_component

    payload["component"] = select_component(payload)
    task = service.submit("optional_media", payload, scope={"research_task_id": research_task_id}, timeout_s=timeout_s, **normalized_options(TaskLimitOptions, task_limits))
    return {"status": task["status"], "task": task,
            "recommended_next_action": "调用同一工具并传入 task_id 回读；未完成不作为已取得证据。"}
