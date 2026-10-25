# 开发者指南

本页仅面向源码 checkout。普通用户请从 [首次成功](FIRST_SUCCESS.md) 开始。

## 本地开发

在源码根目录准备 Python 3.12，按项目依赖安装后可使用 `scripts\install.bat`、`scripts\setup_wizard.bat`、`start.cmd` 和 `scripts\setup_agent.bat`。源码向导兼容读取/写入源码根 `.env`；它不等于 Release 产品的 `configure.cmd`。

运行时验证示例：

```bat
.python312\python.exe -m pytest -q tests
.python312\python.exe scripts\verify_api_keys.py
.python312\python.exe scripts\verify_all_capabilities.py --safe
```

`verify_all_capabilities.py` 在 Release 激活环境会自动把报告、日志和状态写入 `KR_DATA_ROOT`；源码 checkout 则维持 `runtime/` 开发语义。

## 开发边界

持续评测沿用[既有用例与触发约定](../config/evaluation/README.md)：修改后按影响运行，真实故障增量补缺口；报告不创建新评测集。CI统一记录JSON/JUnit，native业务与开发合同分层，账本和金标不自动覆盖。

- 不提交 `.env`、Cookie、Profile、SQLite、运行日志、媒体缓存或绝对个人路径。
- 发布只从隔离 public checkout 的干净候选 artifact 进行；开发源和当前稳定安装不互相覆盖。
- 修改安装、配置、包或运行时路径时，必须做全新安装、升级、回滚与 MCP stdio 合同验证。

## 对话驱动的研究与可选证据查看

用户在Codex对话中给出调研目标，Codex调用kr_research、自主选择普通MCP工具并用finalize_research_task记录完成检查。research_ledger是唯一研究真值；不需要网页建单、交接、认领、租约或逐次预留。预算档位仍为原路由建议，账号、付费调用和高成本媒体沿既有权限边界；未知费用不表示免费。

网页“研究记录”只读展示同一账本的任务、工具次数和交付文件，不负责启动研究；证据可从任务文件显式导出到笔记软件。开发18883只读接入稳定研究账本和固定证据，配置、浏览器、状态、日志和缓存指向独立预览根，不能把新源码验证说成稳定MCP已升级。旧预览research_workspace/tasks.sqlite3仅留作历史，不再读写或迁移为真实研究。

## 本地网站的启动与恢复

正式名称为 KnowledgeRadar，地址 `http://localhost:18882/`；维护者入口为 KnowledgeRadar 开发版，地址 `http://localhost:18883/`。两者只绑定 IPv4 回环，兼容 127.0.0.1 地址；不依赖公网、反向代理或 Codex 窗口。

每个角色只保留一个应用宿主。正式安装通过 `console.cmd` 使用固定 Windows 任务 `KnowledgeRadar Stable Local Console`；开发版首次须明确以 `--role dev --port 18883 --program-root <checkout> --preview-state-root <isolated-root> --enable-autostart` 选择当前源码，再由固定任务 `KnowledgeRadar Development Local Console` 维持。任务只在当前用户登录时运行，无运行时长上限，允许电池启动和运行，失败最多按两分钟间隔重试五次。隔离测试和未启用自启动的临时预览仍可按需运行，不注册任务。

仅设置失败重试没有覆盖本机实测的宿主被结束路径，因此同一任务另含每分钟补启动的触发器，服务已经运行时由 IgnoreNew 忽略，不新增 watcher。`--stop-supervisor` 会先暂停该任务再停止，防止把有意停止误当故障；重新点击启动入口会恢复任务。此为新源码合同，旧安装须按正式制品流程升级后才能获得完整行为。

宿主管理自有 worker 的崩溃和持续二十秒无健康响应，使用有界重试与退避。Windows venv 的转发进程和其子进程一起回收；不抢占外来端口，不随机换网址。状态每五秒保留心跳，读取时同时核对进程和 HTTP，旧 READY 不能作为可访问证据。

本地启动入口向当前浏览器交接六十秒内、单次有效的票据，票据只放在 URL fragment，门页立即移除。密钥使用当前 Windows 用户的 DPAPI 封存；会话 cookie 按角色区分，HttpOnly、SameSite=Strict，最长三十天。重启服务不使已有会话和 CSRF 令牌失效；过期后重新点击本机启动入口，无需手填令牌。匿名根页面不返回任务、配置或有效 CSRF；API 同时校验会话与 CSRF，宿主停止使用独立短时证明。Cookie 按主机区分，因此首次从 localhost 授权后，另用 127.0.0.1 访问需要再次从启动入口进入。

默认支持个人电脑一个活跃 Windows 用户，各自安装并使用其本地数据根。固定端口在同机多账号并发时会拒绝冲突，不读取其他账号的数据。此机制不防同账号恶意程序或管理员；自选数据根还须保留用户专属 NTFS 权限。页面失联会明确标注旧内容并自动重试，不能把短测试当作重启、睡眠唤醒或跨七十二小时的长期验收。

普通详情返回evidence_assets版本ID。Codex可直接用get_task_status(research_action="evidence_fetch",research_payload={"version_id":"...","start":0,"max_chars":8000})分段回读，用evidence_search查询；list/inspect只读原账本，不增加新工具或完成流程。原有后台任务查询、等待与取消保持独立语义。

scripts/kr_workspace.py提供list/inspect/search/fetch/export、显式证据包materialize及migration-plan/migration-apply。迁移必须确认源SHA和授权，只补入历史部分原文，不改旧JSONL。可用--state-root和--evidence-root选择运行态；不创建研究任务。正文和定位按版本保存；容量同时限制独特原文逻辑字节与SQLite/FTS等物理字节，写入前核对。旧摘要无法恢复丢失正文。导出须确认来源许可；不启用后台同步或私人笔记扫描。

前端使用显式page.html与console.ts/research_workspace.ts模块，node scripts/build_console.mjs生成随包JS，--check检查漂移；不等于完整tsc检查，产品运行不要求Node。旧动态HTML补丁与11份旧片段已退役。

语音唯一主路径为 Qwen3-ASR-0.6B-hf（原生 Transformers）；没有 Whisper 或 1.7B 自动回退。文本 PDF 由 pypdf 读取；任一页无文本则整份交给 PaddleOCR-VL-1.6；Office/HTML/Markdown 由 Docling 转换且不加载 OCR/ASR 模型。三项隔离组件共用 local_heavy=1，默认 CPU、4 线程，完成/取消后退出进程。模型角色和固定版本集中在 src/runtime/model_roles.py，云端仅百炼 Qwen3.8-Omni-Flash；评论完整保留并精确去重，相关性由当前研究 Agent 判断。

组件页显式安装，或运行 scripts/kr_optional_media.py：所有命令均须 --component <组件>；plan 只读；prepare --acknowledge-download 按随包 config/optional-*-win-py312.lock 下载全量固定 wheel 与权重，不重新解析候选版本；install --payload-file <install-spec.json> 按 hash 离线安装到数据根的独立环境。推理不下载，失败不更换模型。uninstall 卸载隔离环境且保留证据；加 --purge-downloads 才删除该组件下载缓存。核心 Python 不变。激活升级时只迁移已退役模型控制项，保留凭据和搜索设置，并保存本地备份/哈希回执；回滚按项恢复，保留升级后用户修改的值；只暂存制品时不改现有配置。模型超时或取消须回收实际子进程树。安装身份有效不等于人工质量或正式宿主验收。
