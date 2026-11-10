# MCP Setup

## 内核与宿主连接的边界

正式安装使用唯一用户级计划任务管理持久内核。关闭本地控制台页面、MCP
目录发现结束、stdio连接退出，都不会结束已接受的任务。内核管理不依赖网页
保持打开；关闭控制台自启动时，同一个计划任务以 `--runtime-only` 管理内核。

MCP 的 `initialize` 和 `tools/list` 只提供工具合同，不负责启动内核。
内核不可用时，业务调用返回 `execution_owner_unavailable` / `not_started`，
这不是业务成功。`runtime status` 只读，输出内核身份及监督状态；显式
`runtime start` 才会唤醒已有用户级任务，不从 Codex 的受限 Job 中拉起内核。

监督记录位于用户所选数据根的 `state/execution_owner/`：
`supervision.json` 记录管理状态，`lifecycle.jsonl` 保留真实内核PID、代际与
退出码。Windows venv 转发器与实际内核PID分别记录。意外退出最多自动恢复
三次，预算耗尽后保持 `circuit_open`；显式 `runtime start` 才重新开放预算。
恢复进程不重发业务请求，未知执行结果仍须按原 `request_id` 查回执。

升级/回滚先暂停同一用户任务并安全排空；忙任务阻止切换，不强杀内核。
释放维护锁后，任务从新 `active.json` 启动。安装回执分别记录激活、
`runtime_readiness` 和 `native_host_binding`；`APPLIED_RUNTIME_NOT_READY`
表示文件已切换但内核未就绪，不能称安装验证通过。宿主绑定必须在受支持的
刷新后，由当前原生 health/capabilities 双调用另行确认。

以下为源码使用方式；源码协议测试不等于正式安装或原生宿主验证。

Start the HTTP server first:

```bat
start.cmd
```

Then register:

```text
http://127.0.0.1:18765/mcp
```

Generate current-path snippets:

```bat
python scripts\generate-mcp-config.py --agent openclaw
python scripts\generate-mcp-config.py --agent workbuddy
python scripts\generate-mcp-config.py --agent claude
python scripts\generate-mcp-config.py --agent cursor
```

Before editing an existing Agent config, create a timestamped backup. The helper `scripts\setup_agent.bat` prints snippets but does not silently overwrite external files.

For stdio clients, the generated snippet launches:

```bat
python -X utf8 src\server.py
```

with `KR_MCP_TRANSPORT=stdio`.
