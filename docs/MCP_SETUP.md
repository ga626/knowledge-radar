# MCP Setup

## 内核与宿主连接的边界

正式安装使用唯一用户级计划任务管理持久内核。关闭本地控制台页面、MCP
目录发现结束、stdio连接退出，都不会结束已接受的任务。内核管理不依赖网页
保持打开；关闭控制台自启动时，同一个计划任务以 `--runtime-only` 管理内核。

MCP 的 `initialize` 和 `tools/list` 只提供工具合同，不负责启动内核。
内核不可用时，业务调用返回 `execution_owner_unavailable` / `not_started`，
这不是业务成功。`health_check(mode="runtime")` 与 `runtime status` 在内核外只读，
输出 ready/recovering/circuit_open/stopped/blocked/maintenance/unresponsive/unknown、
监督者状态、最近故障事实与下一步；查询不启动内核。`get_capabilities(summary=true)`
提供同源静态合同并附运行摘要，工具在目录中不等于业务可以执行。

监督记录位于用户所选数据根的 `state/execution_owner/`：
`supervision.json` 记录管理状态，`lifecycle.jsonl` 保留真实内核PID、代际与
退出码。Windows venv 转发器与实际内核PID分别记录。意外退出最多自动恢复
三次额外重启，预算耗尽后保持 `circuit_open`。刷新、重连与管理器重建不清预算。
明确缺文件、缺必要依赖、访问拒绝、数据打开/配置格式错误或存储失败，直接 blocked。
未知崩溃可以有界重试；活进程启动超时则 unresponsive，不强杀、不并发再起。
恢复进程不重发业务请求，未知执行结果仍须按原 `request_id` 查回执。

在每次相关任务开始前读取 runtime；recovering 可用 `wait_seconds=15` 做一次有界观察。
熔断、blocked 或状态未知时先告知用户，不盲目调用、不自动展开调查。
运行错误保留原请求号、execution_state，并附 runtime；unknown 必须先查业务回执，
内核重新 ready 不意味着旧请求已成功或可以换号重发。

## 单次恢复与维护操作

本地控制台的“系统运行”卡显示运行状态、剩余额度、最近故障与业务影响。
“恢复一次”只接受一次启动，不重开自动预算；页面关闭后已接受的尝试继续，
刷新沿原 recovery_request_id 对账。恢复被受理不是成功，真实 ready 才是就绪。
开发预览只读；若控制台和管理器同时不可达，页面只能说明未知，不能代替 OS 入口。

同源安装根的 `knowledgeradar.cmd` 支持以下命令（源码可用 Python `src/server.py`）：

```text
knowledgeradar.cmd runtime status --wait 15
knowledgeradar.cmd runtime start --request-id <新恢复意图ID> --expected-source <状态中的source_identity> --expected-incident <状态中的incident_id>
knowledgeradar.cmd runtime recovery-receipt <原恢复意图ID>
knowledgeradar.cmd runtime receipt <原业务request_id>
knowledgeradar.cmd runtime rearm --request-id <新的维护意图ID> --expected-source <已核对身份> --expected-incident <已核对事件>
```

没有 incident_id 时省略该参数。start 只尝试一次；rearm 是处理完环境后显式重新开放
自动预算的维护动作，不是普通按钮的副作用。正在恢复时合并到原请求；ready 时无操作。
收据损坏、消费结果未知、维护中或身份冲突时拒绝再启动，先由用户决定是否调查。
必要事实记录于 `bootstrap-failure.json`（绑定启动ID）、`supervision.json` 和
`recovery_requests/`；不扫描任意日志、不猜测安全软件、不自动安装依赖或修库。
故障历史与回执随数据保留，临时启动命令和进程描述符不迁移重放。

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
