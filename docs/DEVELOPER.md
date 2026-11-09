# 开发者指南

本页仅面向源码 checkout。普通用户请从 [首次成功](FIRST_SUCCESS.md) 开始。

## 本地开发

执行内核独立于 MCP/CLI 会话存活。runtime/application.py 是入口中立合同与执行边界，execution_owner.py 只在本机回环传小请求，媒体仍留本地；MCP/CLI 不另建执行器。安装根 knowledgeradar.cmd 每次解析 active.json；capabilities、runtime status/receipt 不启动或恢复工作，runtime start 显式启动。knowledge batch/compare 将结果写新文件并返回哈希回执，拒绝覆盖。未知执行先查回执；进程崩溃标 interrupted，显式恢复沿原费用范围。安装、回滚和数据迁移与启动共享锁，只停空闲内核，忙时拒绝切换。此为候选代码合同，隔离进程测试不代表当前安装已升级。

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

scripts/kr_workspace.py提供list/inspect/search/fetch、与 MCP 同源的 artifacts 命令及 migration-plan/migration-apply。迁移必须确认源SHA和授权，只补入历史部分原文，不改旧JSONL。可用--state-root和--evidence-root选择运行态；不创建研究任务。正文和定位按版本保存；容量同时限制独特原文逻辑字节与SQLite/FTS等物理字节，写入前核对。旧摘要无法恢复丢失正文。交付先经 artifacts 授权目标，再按固定版本执行 create-only 发布并回读；不接受旧 ZIP materialize 写入口，不启用后台同步或私人笔记扫描。工具类型和近邻用途是唯一说明源；运行 scripts/generate_tool_manual.py 生成，--check 检查漂移；get_capabilities(tool_name=...) 按需展开。

前端使用显式page.html与console.ts/research_workspace.ts模块，node scripts/build_console.mjs生成随包JS，--check检查漂移；不等于完整tsc检查，产品运行不要求Node。旧动态HTML补丁与11份旧片段已退役。

语音唯一主路径为 Qwen3-ASR-0.6B-hf（原生 Transformers）；没有 Whisper 或 1.7B 自动回退。文本 PDF 由 pypdf 读取；仅缺文字层的请求页交给硅基流动 PaddleOCR-VL 1.5；显式增强才处理疑似缺正文页，保留已有文本与页账本；Office/HTML/Markdown 由 Docling 转换且不加载 OCR/ASR 模型。ASR 与 Docling 两项隔离组件共用 local_heavy=1，默认 CPU、4 线程，完成/取消后退出进程。模型角色和固定版本集中在 src/runtime/model_roles.py，云端仅百炼 Qwen3.8-Omni-Flash；评论原证据保留；程序按查询和可核查线索生成带理由的阅读视图，保留祖先上下文和缺口；当前研究 Agent 决定采信与充分性。

组件页显式安装，或运行 scripts/kr_optional_media.py：所有命令均须 --component <组件>；plan 只读；prepare --acknowledge-download 按随包 config/optional-*-win-py312.lock 下载全量固定 wheel 与权重，不重新解析候选版本；install --payload-file <install-spec.json> 按 hash 离线安装到数据根的独立环境。推理不下载，失败不更换模型。uninstall 卸载隔离环境且保留证据；加 --purge-downloads 才删除该组件下载缓存。核心 Python 不变。激活升级时只迁移已退役模型控制项，保留凭据和搜索设置，并保存本地备份/哈希回执；回滚按项恢复，保留升级后用户修改的值；只暂存制品时不改现有配置。模型超时或取消须回收实际子进程树。安装身份有效不等于人工质量或正式宿主验收。

## 研究归档的展示合同

新绑定任务的Vault目录为“项目/首次创建本地日期 任务名”，任务内直接放Markdown，不按轮次、资料类型增加子目录。目录名固定后不随重试或跨天变化；旧任务不自动迁移。资料名为“来源 - 内容名称.md”，来源按平台/生态（网页、学术、B站、小红书、BOSS直聘等）确定，不按抓取器或媒体类型命名；同名由程序追加自然数字。

来源域名与采集元数据自动提供来源标注。名称优先复用已有中文展示标题，否则保留净化后的原题；无须Codex逐文件整理，不增加翻译或命名模型。资料原文保留原语言和合法代码，未知英文标题不硬译或假造主题。标题、来源、类型、时间和完整性写入正文；采集、入库时间分开，不能用归档日期刷新旧证据。

展示标题仅“平台或大类 - 内容主题”两段；已知网站/文档产品尾缀和文档模块前缀不重复塞进文件名，完整原题留固定证据。网页、学术不按网站或工具细分，B站、YouTube、BOSS直聘、猎聘、脉脉、V2EX等分别标注；新适配器可随证据传入 `source_presentation={group: web|academic|platform, label: 平台展示名, site: 来源站点}`，不必再修改核心归档器。此元数据必须随正文固定版本保留，不能靠后续全局注册追改旧证据。

单篇正文不再添加与Obsidian内联文件名相同的大标题；开头的“资料摘要”只写来源网站/原文链接、处理方式、内容语言/覆盖和原采集时间。字幕来源随 `transcript_metadata.source` 入库，区分平台字幕与音频ASR；具体方式缺失时如实标注，不按平台猜。分卷文件在篇目间保留二级主题标题。

同一稳定 `project_id` 在同一授权根复用持久目录映射。新状态中若同名目录已存在，返回 `project_directory_exists_bind_context_with_vault_project_dir`，由Codex用 `bind_context.vault_project_dir` 明确绑定后复用；不再自动生成第二个项目，也不按显示名擅自认领私人目录。任务和资料的同名序号仍保留；不同真实项目重名须由调用方明确选不同目录。显式目录同样登记身份，后续人改名按身份定位，删除/替换不重建。

正文/转写/识图文字与可读评论/表格可入Vault。转写坐标、图片和媒体地址清单留KR，未知格式或转换失败在资料导航和回执中报缺口。派生文本关联原固定版本；AI仍从KR按版本检索/读取，原始证据不被清洗覆盖。选取交付只另产链接清单、复用同版全文。大批量按来源在同层分卷，默认每卷不超过2MiB/100篇，不把全任务一次装入内存。

扁平归档逐文件create-only发布，先固定计划、再保存各文件身份回执，最后发布资料导航；整批不声称文件系统事务原子性。取消/断电可沿同一计划续跑，人改/移动/删除不覆盖或重建。展示规则由research_archive与knowledge_delivery共用并经测试约束，工具说明由类型合同生成，不另维护一套手写操作手册。


## 归档、任务备份与制品身份

归档维护按 `KR_STATE_DIR` 与 `KR_EVIDENCE_ROOT` 定位交付和证据，不从任务库父目录反推状态根。任务库沿用 `KR_TASK_DB_PATH`，未指定时位于 `KR_LOG_DIR`；本次修复不迁移既有数据库。

研究快照读取实际任务库、其旁 `task_artifacts`、Owner请求库和费用账本路径，清单记录来源布局及数据库是否存在。任务引用的结果缺失或超出快照允许的产物范围时明确失败，不交付不完整的“成功备份”。SQLite副本切换为独立日志模式，校验不生成WAL伴生文件。恢复只写新目录，重绑定任务结果路径；具体路径在 `RESTORE-RECEIPT.json` 的 `runtime_paths` 中，不自动修改环境、恢复授权或执行任务。旧v1快照仍按其原state布局读取。

`kr_freeze_runtime.py` 仅登记版本和SHA命中依赖锁的wheel，缺少锁定文件时保持原清单；缓存中的旧版本不进入制品清单。清单绑定的是打包LF文本身份，不是Windows工作区CRLF原始字节。候选源码、包身份、active安装和宿主进程必须分别回读；本地测试通过不自动激活候选。

## 知乎借用浏览器链路

知乎使用 KR Browser Link 的固定只读站点脚本；搜索、指定回答/文章、问题的有界回答目录和可见评论由平台适配器处理。图片交给公共媒体下载/OCR，采集后先释放浏览器，增强可用采集快照恢复。评论和受限正文保留覆盖缺口；zvideo及正文内视频不承诺转写。

扩展版本与Native Host协议分别读取同一候选中的 `browser_link/extension/manifest.json` 和通道合同，不在文档复制版本号。知乎及专栏采用精确主机权限。安装升级须核对扩展与程序身份，并由浏览器正常授予新增站点权限。旧扩展不具备知乎路由时返回适配器不兼容，不回退到旧CDP、签名或磁盘Cookie。旧KR_ZHIHU_CHROME_DEBUG_PORT和KR_ZHIHU_CHROME_USER_DATA_DIR不再参与执行。

隔离验证使用tests/test_zhihu_browser_link.py；tests/test_zhihu_dom.py以本机Chrome的隔离世界运行打包脚本、全部页面来自本地夹具，单独执行，不放宽持续评测的离线保护。真实业务仍需在同一候选、获准日常Profile完成搜索、正文、公共存证和按版本回读；夹具、HTTP403分类或旧安装结果均不能替代。


## 当前能力与文档维护

[来源能力指南](CAPABILITIES.md)由同一语义目录与类型合同生成。工具参数、输出和错误见生成的工具参考；学术发现仅返回文献卡，Codex按固定引用发起独立全文任务，再查任务与证据。职位和职业讨论分别保留原生对象，不按平台标签确定可信度。

源代码的声明、启用状态、账号就绪和真实验收分开；无对应身份与范围的回执，不显示已观察/稳定边界。未知平台显示unmapped；全局失败统计不复制为某个平台的失败证据。现有账本的最近事件带时间、源码/运行代际、作用范围与日志/请求定位；它们是运行记录，仍须对应验收回执才能宣称已验。微信读者留言已暂停，显式请求保留暂停缺口，不调用读者浏览器，也不把未暴露写成零评论。

运行 scripts/generate_tool_manual.py 生成工具合同、展开说明和来源指南；--check只读检查。scripts/check_mcp_tool_schema.py比对实际MCP和业务schema，验证目录引用及学术全文示例。纯文档CI运行scripts/check_current_docs.py，不执行平台业务；完整值约束由现有schema合同测试验证。脚本清单使用内容指纹，不使用本机文件时间。

官方OpenCLI用于已接入的对应适配器，KR Browser Link用于其对应的原生站点通道，两者职责不同。无需安装KR修改版OpenCLI。日常Chrome默认独立工作窗口、后台最小化；有明确人工交互或已观察可见性阻断信号才前置，解除后回后台。窗口创建到最小化仍可能短暂可见；不得宣传绝对零闪现。后台流程的限定成功不代替冷启动、真实验证码和长期并发验收。
# 研究责任合同（2026-11-09）

1. 关键命题由Agent按可独立改变决策的粒度声明；程序校验身份、定位和修订，不裁定真假或语义饱和。
2. 推荐、比较与新颖性判断须处置直接替代问题；允许复用适用的旧比较，受阻/推迟明确保留，不强迫无限搜索。
3. 交付时给出可读的“关键结论与撤证审阅”：结论、核心依据、最强反证或替代、撤后变化和限制。用户无需阅读JSON字段。记录绑定报告和命题版本；缺审阅不冒充完成。
4. 问题、需求、尝试分离。失败尝试保留，产物按需求版本和范围贡献；未关联调用不自动变成探索，事后关联保留标记。

开发按批次先写能暴露旧错误的测试，实际确认红灯，再修改实现；该批定向检查全绿才进入下一批。现有评测库增量维护，源码、候选、原生/安装及用户验收分别记录。
