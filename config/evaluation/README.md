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
| CI | `ci --extra-node <已有测试节点>`：离线用例合批一次pytest；需要真实子进程的控制台、执行内核和 stdio 探针单独在临时端口/数据/互斥名执行一次；仍禁止外网和日常浏览器，stdio 探针退出时收回自身临时内核，均留JUnit并映射case；manual保持NOT_RUN，治理测试失败也阻断 |
| 前台 | 执行/导入后自动更新摘要；`publish-summary`可只更新投影。网站“设置与帮助→持续评测”只读，源码变化或摘要过期提示更新，不自动跑平台 |
| 只写报告 | 文档最小检查；不调用全量产品、账号或付费评测 |

所有全局参数放在子命令之前。真实回执必须有具体 `receipt_contract`；health v2/capabilities目录摘要绑定当前execution_owner，其余业务按各case的 `receipt_requirements` 和success增量核对响应合同，未核对前不得赋PASS。旧exec包装call_id标为 `codex_exec_wrapper`，带namespace的直接function_call标为 `codex_function_call`，都不是服务端自己生成的调用ID。仅接受可配对的单一原生分发；重复配对、混合脚本、截断输出、错工具/namespace、错身份、缺输入关联均拒绝。优先保留响应中的runtime.generated_at；没有该字段的目录摘要使用原始宿主输出记录timestamp，并显式记observation_time_source，不能使用导入时间。

Windows 隔离批次使用自己创建的嵌套 Job 回收普通子进程；仅 owned-host 批次和 stdio 候选探针允许执行内核显式脱离该 Job，退出时仍按临时身份停止内核。产品若遭遇禁止脱离的 Job，明确返回 `execution_owner_job_breakaway_denied_start_from_user_terminal`，不能退回依附短命宿主。CI 离线批次硬期限为 1200 秒、owned-host 为 240 秒；保留逐例输出、最慢 20 项及 90 秒堆栈，超时仍阻断，延长期限不代表测试通过。

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

PR前先plan和定向测试，CI沿既有`ci`合批节点跑维护合同；安装后仅按同制品身份重验受影响的read/update、观察采集、片段读取和纠错持久化。新MCP schema必须完整预检，正式宿主刷新后才赋原生PASS。
