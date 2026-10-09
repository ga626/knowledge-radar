# 本地控制台

稳定控制台的固定地址是 `http://127.0.0.1:18882/`。它不是外部网站：页面和 API 只监听当前电脑的回环地址，其他设备无法访问；不会显示或上传 API Key、Cookie、任务正文或本机路径。账号恢复页在本机显示登记名称、槽位和登记编号，用来确认该扫码的账号。

## 平台账号恢复

调用遇到明确的登录或验证要求时，同一事件会更新能力中心的账号卡和全局待处理提示，打开对应账号窗口，并请求 Windows 桌面提醒。网站打开时每两秒读取该事件；未打开网站也能收到桌面提醒，但 Windows 通知设置可能隐藏提醒。辅助进程的显示回执不代表你已看到。

小红书显示正式 A/B/C 的登记全名和数字，实验通道不参与。其他平台使用同一恢复入口。关闭窗口只回收浏览器，未验证的恢复待办保留；点击“已登录，验证恢复”后，必须通过对应 Profile 的探针才解除待办。共享软件错误、限频和模型欠费不会被当作扫码要求。

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

开发预览是独立入口，固定为 `http://127.0.0.1:18883/`，只能由显式 candidate program root 启动，也不能代替 stable `18882`。每次打开开发预览时，会启动一个脱离当前终端和 Codex 会话进程树的 supervisor；它不是 Windows 自启动任务，因此候选不会在登录后自行常驻。启动参数同时固化该候选的程序根和独立状态根，开发者可随时再次运行入口恢复预览。

诊断时可运行开发入口的 `--status`。除了端口、角色、身份摘要、generation、supervisor 状态和日志相对位置外，它还会核对记录中的 supervisor PID：端口已关闭而 PID 已不存在时明确显示 `STALE_SUPERVISOR_STATE`，不会再把陈旧的 `READY` 当作可用；端口仍由孤立 worker 占用时显示 `WORKER_WITHOUT_LIVE_SUPERVISOR`，下一次打开会先安全接管，再创建新的受管 supervisor。
