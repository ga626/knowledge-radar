# 持续评测使用约定

这是长期维护的同一套能力、用例和判定合同。报告只引用结果；新功能、真实缺陷或覆盖缺口才增量维护用例。重跑产生新run，不重建数据集，不改历史结果，不自动接受新基线。

## 日常入口

在当前源码根用开发Python执行 `scripts/kr_evaluate.py`。`--store` 或进程级 `KR_EVALUATION_STORE` 可显式指定账本；受管public checkout/worktree默认汇入项目源根的 `local/evaluation`，普通clone默认自己的 `local/evaluation`。

| 事件 | 命令与边界 |
| --- | --- |
| 相关代码或配置变更 | `plan` 查看失效及缺口，再 `run --case <已有ID>` 定向执行；`run --affected` 仅在确需整个受影响集合时使用 |
| 查看事实 | `status` / `history`；verdict、适用性、账号readiness分开；status整体INCOMPLETE返回1是事实，不是命令崩溃 |
| 比较同合同结果 | `compare <run或baseline> <run>`；PASS退化到任何非PASS均阻断，不可比较返回非零；结果不完整不能放行 |
| 接受参考 | `accept-baseline <run> --reason <适用范围>`；必须显式说明边界，不代表整个业务通过 |
| 保护旧账本 | `import-history <旧store>`；校验run、证据和基线，保留ID/原始字节；重复导入不产生重复run |
| 保护既有语音输入 | `import-speech-dataset <原manifest.json>`；CAS资产去重，保留待人工标注状态，不重跑ASR |
| 原生回执 | `prepare-native-receipt <Codex会话jsonl> --call-id <实际宿主ID> --case <ID> --expected-target <身份JSON> --output <空目录>`，再 `import-receipt <receipt.json> --expected-target <身份JSON>` |
| 日常故障 | 工具完成时被动收集低敏候选；`--candidate-store <数据库> candidates`；旧trace可用同一参数加 `collect-trace <jsonl>` 有界回填 |
| 候选审查 | `review-candidate <ID> --case <已有ID> --reviewer <审查者> --reason <依据> --input-sha256 <已核对输入身份> --oracle-version <版本>`；无可重现输入不能宣称已重放；有缺口先在本目录增量补合同 |
| CI | `ci --changed-from <base>`：底线＋粗分组；未知改动或 `--full` 运行完整离线集合。节点与组仅在本目录 cases.json 的 ci 中维护，含嵌套/Node；不截断12项。先验入口合同，失败阻断依赖批次；未选完整的case留NOT_RUN。pytest去重合批，owned-host隔离，失败证据留账本 |
| 前台 | 执行/导入后自动更新摘要；`publish-summary`可只更新投影。网站“设置与帮助→持续评测”只读，源码变化或摘要过期提示更新，不自动跑平台 |
| 只写报告 | 文档最小检查；不调用全量产品、账号或付费评测 |

所有全局参数放在子命令之前。真实回执必须有具体 `receipt_contract`；health v2/capabilities目录摘要绑定当前execution_owner，其余业务按各case的 `receipt_requirements` 和success增量核对响应合同，未核对前不得赋PASS。旧exec包装call_id标为 `codex_exec_wrapper`，带namespace的直接function_call标为 `codex_function_call`，都不是服务端自己生成的调用ID。仅接受可配对的单一原生分发；重复配对、混合脚本、截断输出、错工具/namespace、错身份、缺输入关联均拒绝。优先保留响应中的runtime.generated_at；没有该字段的目录摘要使用原始宿主输出记录timestamp，并显式记observation_time_source，不能使用导入时间。

Windows 隔离批次使用自己创建的嵌套 Job 回收普通子进程；仅 owned-host 批次和 stdio 候选探针允许执行内核显式脱离该 Job，退出时仍按临时身份停止内核。产品若遭遇禁止脱离的 Job，明确返回 `execution_owner_job_breakaway_denied_start_from_user_terminal`，不能退回依附短命宿主。CI 离线批次硬期限为 1200 秒；owned-host 取被选合同预算，最低 240 秒。保留逐例输出、最慢 20 项及 90 秒堆栈，超时仍阻断，延长期限不代表测试通过。

两条轻量摘要的expected-target除source_commit/artifact_sha256外，还必须有runtime_identity（owner进程身份）、owner_generation、runtime_source_identity、contract_hash、tool_count、tool_names。源码/ZIP来自已核验安装回执，工具集合/指纹来自同一制品生成合同，owner来自同一观察窗口；不能从待判响应抄出“预期值”完成自证。完整集合与数量分别核验，不把当前工具数写死成永久常量。判据变更递增oracle_version，历史run保留原判据，不自动接受新baseline。

## 数据和费用

候选只存工具、错误族、匿名trace/任务关联和次数；不存查询、URL、账号、Cookie或原输出。KR可见任务另记低敏观察、交付缺口与预先确定的成功抽样。失败样本和约1%正常采样分开；30天、500候选、10000条去重事件上限。过期/容量损失、最老待处理时间单列。工具调用、任务、候选簇、题、试次不得混算；工具成功不代表语义正确。它们不能估计总体成功率，也不是金标。费用未知记unknown；不自动登录、付费评分或新建巡检。

账本、基线、运行证据和大输入不进Git或用户包。原生/安装观测保留自己的产品身份，不能抬高当前开发源通过率；未测外部环境保持unknown。历史已接受基线仍可查询；不同合同、环境或产品目标不可强行比较。哈希用于完整性与去重，不是防本机恶意篡改的数字签名。

候选代码在获得PR/发布授权后才进入云端与稳定安装；本地验证或18883预览不等于稳定交付。

## Agent 维护与人工抽查

继续使用本目录、现有候选数据库和不可覆盖的 Ledger；维护题卡是同一用例ID的运行时版本记录，语义试次也进入同一 runs/。没有另建研究LLM或后台唤醒器。

两种可实施路线：仅扩开发CLI，改动较少但正式Agent不可持续维护；或者薄运行时维护核心，同时由现有MCP、CLI和只读前端接入。采用后一种：`read_knowledge`/`update_research`新增`action=evaluation`，仍保留原有工具集合，CLI和UI复用相同事务。前端沿设置页既有暗色桌面样式，仅新增评测审阅区，不改其他产品导航。

| 环节 | 入口与合同 |
| --- | --- |
| 接手工作 | `maintenance-queue` 或 `read_knowledge({request:{action:"evaluation",view:"queue"}})`；一次最多20项，只读、无常驻Agent，无Agent显示等待执行者 |
| 维护现状 | `maintenance-status`；原用例、维护revision、候选信号、试次独立统计。MCP各view可用after/limit分页 |
| 准备资料 | `evaluation-asset <文件> --approved-root <授权目录> --sha256 <已核对SHA>`；脱敏UTF-8文本，2MiB上限，沿Ledger assets/按内容保存，不能任意覆盖 |
| 影子/自动维护 | `maintain <请求.json>` 或`update_research({request:{action:"evaluation",...}})`；schema、request_id、expected_revision、actor/context/Skill/rule、理由和固定证据必填；首次只入shadow，独立上下文审阅和原样本仍可读才允许active |
| 假成功 | Agent确认工具ok但内容错误后用report_signal登记低敏信号；不伪造失败工具调用，不抬高调用分母 |
| 状态 | 生命周期、频率、可运行性正交。plan按题卡dependencies和记录时的源码快照给出受影响建议（缺依赖记unknown，不自动跑平台）；长期绿不直接退役，等价替换需固定证据和独立审阅 |
| 合并/恢复 | merge需目标expected revision、逐项coverage_map、独立审阅；保留全部子例、别名、旧revision与历史试次；compensate追加补偿，冲突先停并重读，不覆盖下游变化 |
| 纠错 | UI保存用户反馈或`evaluation-feedback <请求.json>`，显示waiting_for_agent；Agent据feedback_id执行补偿/锁定，resolve_feedback绑定已有审计动作。疑问可先标oracle_disputed，不能直接当已证实Bug。相应错误规则暂停，旧成绩不删 |
| 质量试次 | `quality-trial <请求.json>`；实际输入/答案/判定证据必须已固定；合同、材料、结论、成果分层。控制样本不是真实市场或日用质量；冷隔离缺访问审计、安装身份不符则不计分；同对话不是独立trial |
| 受控演练 | `maintenance-demo --output <新的空目录>`；两轮隔离维护演练，清楚标合成样本；不可声称两轮真实日用运营、原生安装或裁判统计可靠性 |

人负责抽查与纠错，不逐题审批。界面四页签展示分层记录、题库和决策谱系、风险/独立随机/稀有层抽查、历史配对趋势。技术ID与哈希折叠；资料片段需主动展开。所有纠错文本都视为用户内容，不能当执行指令。刷新不执行平台调用，旧/未知/争议不涂绿。

准入需合法输入、明确防错合同、独有覆盖、可查oracle、好例和回归坏例。能力题可无历史坏例，但不能叫已修回归。same-family只作候选聚合；不可当同病因。新规则先影子，不允许同一对话改名冒充独立评审；上下文身份属于可审查声明，不等于宿主签名证明。

维护策略启动版：一次20项、120秒建议预算、默认不授权付费；Agent到边界就停并保存request_id/checkpoint，下次重读队列。程序不替Agent自行消费语义队列。大样本总体误判率、轮换留出、长期漂移需要真实运行积累，本地fixture不能证明。冷Codex访问隔离需要执行者实际限制工具和资产，不能仅写一段提示词。

当前没有受支持的宿主工具访问隔离验证器，cold_replay即使自填access_audit_verified也保持UNVERIFIED/NOT_SCORED。installed_live必须绑定同一Ledger内由现有原生导入器核验过的native_run_id，源码/制品身份匹配；仅填native_receipt标签不计分。原生身份回执不证明答案语义正确，仍需独立判据。依赖快照只代表记录试次的源码checkout，不冒充原生安装源码证明。

历史固定材料可用同一 `quality-trial` 加 `diagnostic_only=true`、`profile=knowledge_reuse` 记录温态回顾。必须引用本题库已有case及其version、固定输入/答案/判定资料；不新建或激活题卡，结果强制NOT_SCORED并保留逐项FAIL/UNKNOWN/PASS及理由。同上下文审阅没有独立校准，不能用于宣称修复提升或正式环境通过。

本地定向；PR底线＋粗组，未知改动完整执行。CI复用同一选测结果拆为 offline、host、node，汇总保留 required check 名称；main仅在可信整批复用不成立时重跑完整离线，再独立构建、解包验证唯一候选。包检查回读固定证据并交付临时本地/Vault；安装后只核身份、原生及受影响真实路径，不把离线通过叫业务就绪。失败先在当前获准 PR 归拢修复，不自动另开 PR。

## 轻量交付接缝

`ci --group offline|host|node` 沿同一粗组选测，不另维护依赖图。offline 排除真实 Host/规模节点，host 单独跑这些节点，Node 不安装 Python 产品依赖。GitHub 用 **Re-run failed jobs** 保留成功 job；改代码后重新选测。所有组留退出码、日志、JUnit、耗时和实际源码字节身份；零收集、跳过、中断不能满足必需 PASS。没适用项明确 `applicable=false`。started 无 run.json 的历史显示 INCOMPLETE，其他完整结果仍可查询。

PR→main只复用本仓库维护者 `codex/` 分支的明确成功 run/attempt 中的完整整批：Git 树、实际源字节（含 .gitattributes）、判据/执行器、Python/Node/Git/C# 编译器、依赖和 runner image 必须一致，附件逐个核哈希。只读 artifact 数据，不执行其脚本；fork、缺件、局部绿灯、未知环境均回到正常执行。不是任意跨 SHA 缓存，制包/解包验证不复用。首次修改此流程仍完整执行；CI线上分组耗时/复用命中需运行后观察，不能预报节省分钟数。

每批用已有 `.preflight.json` 写目标、candidate、requirements（id/stage/expected/receipt/sha256/verdict_path/identity_paths/count_path）、unresolved_internal_blockers、authorization（本批ID、动作、用户消息引用）。治理根 `scripts/kr_closeout_router.py --check <文件> --stage pr|merge|activate|ready` 只读核现有证据；远端操作必须加 `--action pr|merge|install|publish` 检查当前批明确授权记录；建 PR 后把编号和 OPEN 状态登记到 authorization，已消耗创建授权不得续开。这防误操作，不是凭据权限锁，也不判断报告语义。pr/merge项只绑定源码，activate/ready项绑定当时最终源码/ZIP；pr只核本地必需项及当前授权；云端CI在merge到期，不能让建PR依赖尚未发生的CI。缺失、陈旧、零收集和必需失败均阻断。安装型 preflight 标 `kind=installable`，activate/ready 必需 `artifact-runtime` 回执：锁定运行时、真实 owner、带代际的 MCP 正文回读、临时双端归档、正常退出和包内导入均须为真，且 ZIP/源码相符，顶层 PASS 不替代这些事实。安装结果只能记 pending_native_confirmation；ready 仍需本批实际结果，用户验收另外记。

安装器 `plan --archive <ZIP> --receipt <JSON>` 共用 apply 校验，且不整理旧任务、不初始化数据、不停内核。将 stdout JSON保存后，`apply --prepared-plan <JSON>` 重核状态；变动报 stale。plan会在切换前列出客户端是否需刷新；明确接受后用 `--allow-host-refresh`，不会自动重启 Desktop。旧版有五分钟内、同 active 的 ready 回执且同数据合同，新 owner 内部就绪失败才自动恢复已写配置/插件/注册/启动项/active；外部429不触发回退。用户并发编辑保留并报冲突；旧 owner/宿主无法重验则明确报待确认，不承诺分布式原子事务。

MCP沿现有薄转发：工具输入/输出/错误外壳、回执、IPC及显式兼容版本一致，才允许已初始化旧客户端跟随已核 active。owner 的严格源码/代际/认证不减，未知结果不重发。语义不兼容须递增 `runtime_lifecycle_contract.client_compatibility_version`；新 schema/客户端自身变化可能仍需刷新。首次迁入此改造仍要刷新一次，受控 stdio成功不代替 Desktop确认。Browser Link另校活 Host 构建SHA、扩展build_id/操作集合；OpenCLI按原兼容合同，不要求与主程序版本号相同。

故障定位：同输入、同依赖/配置下旧实现过而新实现失败，才确认内部回归；新输入两版都失败只说明未支持该形态，不能据此判平台改版。认证/限流是响应分类，不是根因。回执先保留最后确认阶段、请求/组件身份与可用固定输入引用；缺输入或未知结果不自动重放，不记录Cookie/请求正文。

## 制品使用者检查

公共运行边界（runtime/MCP、包/安装、Host/扩展）命中 `requires_artifact_runtime()` 后，PR 用实际打包规则产临时 ZIP；它不进入正式交付。main 仅产一个候选。构建侧 `verify_release_candidate.py --content-only` 核私密内容、清单、锁；consumer job 不 checkout、不 editable 安装，只下载原 ZIP、同源 stdlib 控制脚本，执行 `--consumer-only --output consumer-verification.json`。控制脚本调用包内安装器、离线 wheelhouse 创建正式 Python，启动真实 owner，通过 stdio 回读固定证据并核执行回执后退出；原包和临时双端归档不依赖开发 manifest。此结果是 required check 的一部分，main 同原包通过后才私有上传。普通解析/文案改动不跑 PR 制品样本，main 可安装包仍必验。

激活与 rollback 共用 `_activation_files` / `_ActivationRecovery`：受管配置、plugin、Host 注册、扩展 loaded、启动项与 active。失败恢复只撤回自己的写入，保留并发修改；账号库和个人资料不入快照。磁盘恢复不等于 Chrome 已加载，也不等于 Desktop 已刷新。持续客户端 A→B→A 用真实 owner 验兼容，外部任务/注册入口仍在单测中显式隔离。
