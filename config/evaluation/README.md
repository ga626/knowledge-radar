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
| CI | `ci --extra-node <已有测试节点>`：离线用例合批一次pytest；需要真实子进程的控制台生命周期测试单独在临时端口/数据/互斥名执行一次，均留JUnit并映射case；manual保持NOT_RUN，治理测试失败也阻断 |
| 前台 | 执行/导入后自动更新摘要；`publish-summary`可只更新投影。网站“设置与帮助→持续评测”只读，源码变化或摘要过期提示更新，不自动跑平台 |
| 只写报告 | 文档最小检查；不调用全量产品、账号或付费评测 |

所有全局参数放在子命令之前。真实回执必须有具体 `receipt_contract`；目前health/capabilities轻量摘要已有机器断言，其余业务按各case的 `receipt_requirements` 和success增量核对响应合同，未核对前不得赋PASS。宿主包装call_id明确标为 `codex_exec_wrapper`，不是服务端自己生成的调用ID。仅接受可配对的单一原生分发；混合脚本、截断输出、错身份、缺输入关联均拒绝。

## 数据和费用

候选只存工具、错误族、匿名trace引用和次数；不存查询、URL、账号、Cookie或原输出。失败样本和约1%正常采样分开；30天、500候选、10000条去重事件上限。它们不能估计总体成功率，也不是金标。人工只处理语言/视觉/扫码等必要判断。费用未知记unknown；不自动登录、付费评分或新建巡检。

账本、基线、运行证据和大输入不进Git或用户包。原生/安装观测保留自己的产品身份，不能抬高当前开发源通过率；未测外部环境保持unknown。历史已接受基线仍可查询；不同合同、环境或产品目标不可强行比较。哈希用于完整性与去重，不是防本机恶意篡改的数字签名。

候选代码在获得PR/发布授权后才进入云端与稳定安装；本地验证或18883预览不等于稳定交付。
