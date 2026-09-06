# 本地控制台

稳定控制台的固定地址是 `http://127.0.0.1:18882/`。它不是外部网站：页面和 API 只监听当前电脑的回环地址，其他设备无法访问；不会显示或上传 API Key、账号、Cookie、任务正文或本机路径。

安装完成后会生成两个等价入口：

- `%LOCALAPPDATA%\KnowledgeRadar\console.cmd`：打开或复用控制台；未运行时启动它。
- `%LOCALAPPDATA%\KnowledgeRadar\configure.cmd`：保留的兼容入口，行为与 `console.cmd` 相同。

`apply` 默认注册当前用户的 Windows Task Scheduler 任务，而不再写入“启动”目录。该任务使用有限次数的失败重试、`IgnoreNew` 多实例策略和登录后可用设置；它只启动 `active.json` 指向的 stable 安装，绝不会引用开发目录。因此电脑重启、插件重启或 worker 异常后都能在同一固定地址恢复。

若不希望登录时常驻，可在安装时加入 `--no-console-autostart`，或随后运行：

```bat
%LOCALAPPDATA%\KnowledgeRadar\console.cmd --disable-autostart
```

恢复常驻：

```bat
%LOCALAPPDATA%\KnowledgeRadar\console.cmd --enable-autostart
```

控制台由 stable supervisor 单独拥有：它用 Windows named mutex 排除重复守护，记录脱敏状态和日志，并通过 `role + fingerprint + generation` 校验实际 worker 身份。入口会复用同一 supervisor；若固定端口被非 KnowledgeRadar 程序或错误 generation 占用，会报告冲突，绝不抢占或悄悄切换随机端口。更新后使用 `console.cmd --restart` 请求 supervisor 受控重启到当前 `active.json` 指定的产品版本。

开发预览是独立入口，固定为 `http://127.0.0.1:18883/`，只能由显式 candidate program root 启动，也不能代替 stable `18882`。每次打开开发预览时，Windows 会创建或更新一个仅可按需运行的 Task Scheduler 任务；它没有登录触发器，不会把开发候选注册成常驻自启动，但能使 supervisor 脱离当前终端或 Codex 会话的进程树。任务名称包含 candidate fingerprint，启动参数同时固化该候选的程序根和独立状态根。

诊断时可运行开发入口的 `--status`。除了端口、角色、身份摘要、generation、supervisor 状态和日志相对位置外，它还会核对记录中的 supervisor PID：端口已关闭而 PID 已不存在时明确显示 `STALE_SUPERVISOR_STATE`，不会再把陈旧的 `READY` 当作可用；端口仍由孤立 worker 占用时显示 `WORKER_WITHOUT_LIVE_SUPERVISOR`，下一次打开会先安全接管，再创建新的 task-owned supervisor。
