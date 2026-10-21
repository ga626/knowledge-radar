# P0–P6 实现与验收指南

范围：P0–P6 开发候选。PR、正式发布与稳定安装分别依照发布候选合同执行，不能以阶段实现记录代替交付回执。真实环境结果保存在本机验收报告，不能以本指南代替执行回执。

## 阶段与模块所有权

| 阶段 | 唯一职责入口 | 已实现的合同 |
|---|---|---|
| P0 | `scripts/kr_evaluate.py`、`scripts/kr_eval/`、`config/evaluation/` | 能力与用例版本、正文/依赖/环境身份、追加历史、失效、定向重测、显式基线与可比性；22 工具仅注册通过不代表业务通过 |
| P1 | `kr_core/models.py`、`understanding/evidence.py`、`runtime/tool_trace.py`、`generic_web/url_policy.py` | 空内容不验证、结构化错误、纯文本限长、URL逐跳/DNS固定、控制台Host与Origin防护 |
| P2 | `runtime/task_service.py`、`task_process.py`、`tasks.py`、`mcp_dispatch.py` | 有界接入、独立进程、deadline、取消终态CAS、重启中断、幂等artifact与查询 |
| P3 | `runtime/account_observations.py`、`account_business.py`、`desktop_notification.py` | 单账号事实/TTL、认证与业务分离、逐账号穷尽、事件去重、系统通知回执、账号精确深链与确认 |
| P4 | `kr_core/content_contracts.py`、各平台策略、`runtime/asset_identity.py` | 职位URL、诚实unsupported、媒体显式开关、秒数时长、正文图片归属、ASR缓存资产身份 |
| P5 | `use_cases/content_detail.py`、`onboarding/static/`、`runtime/storage_lifecycle.py`、`activity_index.py` | 详情用例共用、静态资源拆分、最小依赖、活动lease、逐文件隔离/恢复、增量统计与保留期 |
| P6 | `runtime-win-py312.lock`、`runtime-artifacts.json`、builder/installer | Windows Python3.12离线hash锁、wheel/SBOM/来源、干净源码候选身份、非激活隔离安装、同版本拒覆盖、退役矩阵 |

## 重放与验收

以本地 Python3.12 运行，先激活开发环境或直接指定其解释器：

```powershell
python scripts/kr_evaluate.py validate
python scripts/kr_evaluate.py demo
python scripts/kr_evaluate.py run
python scripts/kr_evaluate.py status
python scripts/kr_evaluate.py plan
python scripts/kr_evaluate.py run --affected
python scripts/kr_evaluate.py compare baseline <run-id>
python scripts/kr_candidate_acceptance.py --program-root <staged-program> --python <staged-python> --state-root <isolated-state> --live-public
python scripts/kr_browser_acceptance.py --output local/browser-acceptance.json
```

默认评测禁外网、禁用户凭据。真实SDK/浏览器/账号验收分别显式执行，原生Codex证据不得由SDK代替。未执行的账号/宿主用例保留NOT_RUN，整体可为INCOMPLETE。基线须明确使用 `accept-baseline <run-id> --reason <reason>`，不自动晋升。

## 网络与缓存边界

公网提取使用现有进程代理解析，不改系统/全局代理。只在域名解析全部落于Mihomo `198.18.0.0/15` Fake IP时，通过固定Cloudflare HTTPS DoH查询真实公网IP；只发送hostname，不发送页面query/token。literal Fake IP、私网、混合私网回答仍拒绝。连接固定已验证IP，SNI/证书校验保持原域名，逐跳重新校验。DoH结果最多256项、30秒。

动态浏览器所有GET经受控提取器，禁止非GET、WebSocket、WebTransport、WebRTC与service worker。导航重定向显式逐跳goto，防止浏览器跳过拦截。该模式可能拒绝依赖POST或实时连接的站点，返回受限/失败，不用原生网络绕过。

旧无身份transcript作为cache miss保留；ASR缓存绑定音频/模型文件SHA和策略。未知归属图片只保留元数据。账号旧JSON只读；新事实过期为unknown，不由旧绿色状态复活。通知“helper已请求显示”与“用户实际看到”分开记录。

## 安装、许可与回退

候选构建要求干净Git SHA。相同ZIP通过 `product_install.py verify-install` 安装到独立根，不写active、不注册宿主、不启动自启、不读日用账号。核心依赖从随包wheelhouse用 `--no-index --require-hashes` 安装；可选浏览器、下载器、ASR模型单独记录来源、许可、版本/哈希和readiness，不能冒充核心包已提供。

`config/license-source-inventory.json` 中历史受限源保留版权和限制，产品与未来源码投影均排除。其精确引入提交尚未确认，此清单不赋予重新分发许可；发布前仍需完成来源复核。原始MIT仅涵盖本项目原创代码。退役范围见 `config/retirement-matrix.json`；显式fixture兼容接口保留，不称为已完全删除。

缓存清理仅登记文件，先落计划和SHA再移入quarantine；活动lease保护，不触及未知文件。恢复检查边界/符号链接/内容SHA，已有目标不覆盖。新增profile禁用Chromium本地模型下载；不复制旧profile模型缓存。没有自动清理真实账号目录。

开发入口18883；稳定18882保持已安装身份。候选失败停在隔离根，保留证据；不通过手工更改active或复制缓存修饰验收。任何回退须先确认旧程序与runtime可用，保留用户数据。

## 验收限制

自然语音金标、真实账号业务、用户可见系统通知、真实浏览器125%缩放、宿主刷新、升级/回退分别需要对应证据。合成语音只是执行链检查；受控浏览器传输只是安全合同验收；等效CSS viewport不是实际浏览器缩放。没有相应记录时均不得宣称已通过。
