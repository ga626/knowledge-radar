# API 与服务商配置

KnowledgeRadar 不需要一次配置所有服务商；只填写你实际要使用的项目即可。Release 产品的唯一配置文件是 data 根 `config\runtime.env`，请通过 `configure.cmd` 写入；源码 checkout 兼容使用仓库根 `.env`。两者都不会读取 OpenClaw JSON、`local\secrets` 或其他秘密文件作为兜底。

| 配置项 | 是否必填 | 用途 |
| --- | --- | --- |
| `TAVILY_API_KEY` | 可选 | 通用网页搜索 |
| `ANYSEARCH_API_KEY` | 可选 | 通用网页搜索备用服务 |
| `BRAVE_SEARCH_API_KEY` | 可选 | Brave Search |
| `EXA_API_KEY` | 可选 | Exa Search |
| `YOUTUBE_API_KEY` | 可选 | YouTube Data API |
| `OPENALEX_API_KEY` | 可选 | OpenAlex 学术元数据额度 |
| `SEMANTIC_SCHOLAR_API_KEY` | 可选 | Semantic Scholar 学术元数据额度 |
| `WFDATA_APP_KEY` | 可选 | 万方官方文献检索/元数据详情；需开放平台应用授权，网页登录不等于 API 权限 |
| `SERPAPI_API_KEY` | Optional | SerpAPI Google Scholar metadata |
| `KR_CORE_API_KEY` | Optional | CORE academic metadata |
| `DASHSCOPE_API_KEY` | Optional | 百炼 Qwen3.8-Omni-Flash 图片与音视频理解；本地 ASR 不需要 Key |
| `TIKHUB_API_KEY` | Optional | Explicit Xiaohongshu fallback experiments |

绝不提交真实 Key。Release 用户运行 `configure.cmd`；源码开发者可运行 `scripts\setup_wizard.bat`。两种页面都只监听本机，均不会显示或上传密钥。

服务按出口独立限额，没有全局总预算。百炼默认10元/日本地预算，按北京/新加坡地域和返回Token（含缓存）估价；媒体缺用量时保留预留值。Tavily最多1000 credits/月，Brave最多5美元免费额/月，Exa当前auto搜索最多10美元免费额/月，超额阻断且不自动转付费。标准AnySearch最多1000次/日，自定义端点价格未核定时仅记录。OpenAlex有Key最多1美元/UTC日，无Key最多0.10美元/UTC日。TikHub最多2个实际API请求/日。云OCR 1.5免费，服从远端配额；万方关闭。未配置的SerpAPI不纳入本轮费用管理。

可用 `KR_PAID_POLICY_PATH` 指向 `config/paid-policy.example.json` 的本地副本，调整百炼预算、降低免费额度或关闭服务。月计数按UTC自然月、本地日上限按电脑日期；账号实际免费余额与充值开关仍以服务商为准。其他程序共用Key的消耗可通过该服务的 `external_used` 和 `external_period` 扣除（仅填KR以外的增量，月填YYYY-MM、日填YYYY-MM-DD）；KR不会自动查询私人账单或开启续费。未知价格仅记录，不占其他出口额度。并发先预留、发送未知不退款也不自动重发；这是本地准入与用量估价，不能宣称服务商实际扣费硬封顶。旧策略v2仅迁移开关，旧全局金额不再生效。

公共费用账本只保留一份；存在 TikHub 历史账本时沿用它，旧发送即使跨午夜仍不能自动重发。不同币种分别限额，不自动换汇；超时或未知结果继续占完整预留。`KR_PAID_USAGE_PATH` 只供隔离测试／显式数据迁移使用，日常不要改路径来重置额度。供应商账单仍是实际收费依据，KR只对经过核实的配置上界执行硬准入。

The runtime loader ignores empty values. If a provider key is absent or blank,
the related feature reports `not_configured`/`EXPECTED_DEGRADED` instead of
borrowing a value from OpenClaw JSON, `local\secrets`, or `config\runtime.env`.

可在不打印密钥的情况下检查配置：

```bat
python scripts\verify_api_keys.py
```

图片和扫描 PDF 默认使用硅基流动 `PaddlePaddle/PaddleOCR-VL-1.5`，需要 `SILICONFLOW_API_KEY`。处理时上传对应页面；文字 PDF 留在本地直接解析。云端失败明确返回错误，不自动转慢速 CPU。此模型当前免费，服从账号实际配额；不安装本地 OCR。

学术检索按任务选择来源：OpenAlex 为综合入口，中文增加 PubScholar，再按学科、地区或文献类型补查。Semantic Scholar 自动补查需要 `SEMANTIC_SCHOLAR_API_KEY`，共享本机限速并遵守服务端冷却；匿名仅显式尝试。万方付费接口默认暂停（`WFDATA_ENABLED=false`）；保存 `WFDATA_APP_KEY` 或显式选择来源也不会产生请求。本人核清费用、决定恢复后才设置 `WFDATA_ENABLED=true`；自动补查还要求 `authorized_sources=["wanfang"]`，其他来源授权不算。默认集合为 `OpenPeriodical`，其他集合必须在账号授权范围内。`WFDATA_BASE_URL` 默认 `https://api.wfdata.com`，仅接受 HTTPS，不通过明文传输 Key。

NSTRS 的公开记录不需账号；检索可能等待约 25 秒，单次等待上限为 45 秒。全文需本人实名注册、通过站方身份审核并在线阅读，不能据此承诺下载或完整归档。脉脉通过受管浏览器本人扫码/验证码登录，不需要另填 Cookie；首次授权后才能核验社区搜索、帖子和当前账号可见评论。

### 学术账号的实际办理入口

- Semantic Scholar：`https://www.semanticscholar.org/product/api#api-key-form`。填写真实身份、所属机构/项目网站与使用计划；本人确认声明及许可后提交，Key 经审核通过后由邮件发放。不能把匿名请求的 HTTP 429 当作成功记录。
- 独立开发者申请时可如实填写独立项目归属和项目公开仓库，不编造学校或公司；内部开发试用按实际情况选择 Private。若匿名调用持续 429，保留状态和请求地址，通过官网 API 许可页列出的 `s2-api@semanticscholar.org` 询问申请前置条件；不能勾选不真实的“已成功调用”声明。API Key 仅保存到本人本地配置，获批后才核验认证检索。
- 国家科技期刊平台开放获取库无需 Key：中文关键词查询会使用 `napstic_oa`，也可在 `search_academic` 中显式指定。公开接口提供题名、摘要、作者、年份与 DOI；文章的 OA 标记不代表已经取得全文。下载预检样本要求登录，当前适配不保存平台账号，也不自动下载全文。
- 万方：`https://www.wfdata.com/skill-detail?id=wfdata-search`。登录 → 去订阅 → 本人确认一键订阅 → 我的订阅 → 管理APP KEY，核对已激活后配置 `WFDATA_APP_KEY`。登录、订阅、Key 激活和实际 API 请求成功是不同状态；未标价不代表永久免费。
- NSTRS：`https://www.nstrs.cn/nstrs/sys/register/termsOfRegister`。表单提供“个人”机构性质，但仍要求身份证号码、正反面照片及学校、专业、职称、工作单位/地址等资料并由站方审核。个人是否免填单位没有明确说明，不能伪造信息；需要时向官方确认。公开摘要检索不需要实名注册。全文许可仅限个人学习、研究或教学，不等于商用、下载或归档授权。

## GitHub 公开研究

无需 Key 即可使用公开 REST，匿名额度由 GitHub 管理。`KR_GITHUB_TRANSPORT=auto|rest|gh` 在请求开始时选择并冻结传输身份：auto 优先已有 `GH_TOKEN`、其次 `GITHUB_TOKEN`，否则使用有效的既有 gh 登录，没有登录则使用匿名 REST。认证无效会报错，不偷偷换匿名身份。KR 不代登录、不导出 Keychain token、不自动切账号。

`KR_GITHUB_ENABLED=0` 关闭整个来源；`KR_GH_CLI_ENABLED=0` 只关闭 gh 传输。仅支持 github.com 的公开仓库，私有与企业仓库需独立授权及适配。限流进入同一身份的公共冷却；后台任务让出执行槽后恢复，取消与截止时间传入获取循环。已批准更换凭据时可递增 `KR_GITHUB_AUTH_EPOCH`，隔离旧检查点。

用 `search_github_repositories(query, page, refresh)` 发现仓库，再以 `get_content_detail(url, github={kind,ref,path,page,per_page,max_pages,comment_stream})` 提交持久详情任务。kind 支持 overview/readme/tree/file/issue/pull_request；按返回 task_id 查询，按 evidence_assets.version_id 回读。分支执行时固定 commit；评论只承诺请求的页窗，下一页见 comments_pagination。非默认或含斜线 ref 请显式传 ref；不通过浏览器兜底。大文件、LFS、二进制、symlink 与 submodule 返回明确缺口。`KR_GITHUB_MAX_FILE_BYTES` 默认 2 MiB、上限 16 MiB。
