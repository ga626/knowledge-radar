# KnowledgeRadar Structure

<!-- KR-GENERATED:STRUCTURE-MATRIX:START -->
Source of truth: `config/project-structure.manifest.json`.

Development source is the only editable truth. Package output is regenerated from source and manifests.

## Source roles

| Role | Boundary |
| --- | --- |
| governance_root | Private project rules and historical reports; not an installed product identity. |
| product_source | Current explicit checkout; do not infer it from cwd or governance-root copies. |
| runtime_identity | Installed active manifest and live process identity, verified separately. |

## Areas

| Area | Purpose | Rule |
| --- | --- | --- |
| Development runtime area | The editable source tree used by Codex and local development. | Source files, scripts, configs, docs, and manifests are edited here first. |
| Clean package output area | Generated release/package directories such as dist/ or release/. | Never edit generated output directly. Regenerate from source and manifests. |
| Local runtime and private state area | Host-local logs, reports, browser state, profiles, caches, secrets, and downloaded media. | Never publish or commit local state. Keep only placeholders or README files when explicitly allowed. |
| Sanitized historical backup area | User-confirmed sanitized migration records and evidence packs. | Archive only sanitized artifacts. Raw profiles, cookies, secrets, and logs are forbidden. |

## Directory Matrix

| Path | Area | Role | Codex Edit | Release |
| --- | --- | --- | --- | --- |
| `browser_link/` | dev_runtime | KnowledgeRadar Browser Link：单一原生通道、随产品交付的本机Host源码及打包的站点内容脚本；不含账号、编译产物或运行状态。 知乎使用固定只读站点适配器：目标ID校验、可见正文与有界目录/评论、分段快照；不导出Cookie、不接受任意脚本或任意请求。 默认创建同Profile的非聚焦专属工作窗口，按租约/代际核验归属并回收；最小持久归属用于异常对账，不记录凭据。 | allowed_with_secret_scan | include |
| `src/runtime/` | dev_runtime | 入口中立业务装配 composition、类型合同、唯一执行所有者、统一请求回执与维护边界；MCP只挂载应用合同，CLI批量复用同一执行内核。公共媒体获取/增强不含站点规则。 阶段③ research_index 在私密研究数据中维护增量候选、问题关系和变化投影；knowledge 只读固定水位及可回查证据。 研究归档的展示合同统一为项目/首次创建日期与任务，任务下按来源中文前缀平铺Markdown；证据版本、轮次与哈希留在KR内部，新布局不自动搬动旧成果。 归档维护按实际状态根定位；研究快照独立解析任务库和任务产物路径，记录来源布局并在新目录恢复时重绑定，不迁移现有数据。 公共PDF合同在pdf_text/document_contract中维护选页、加工身份、页账本和可恢复OCR；comments_contract/comments_view统一六平台评论和可回查筛选。浏览器资源由既有租约协调器管理，扩展只拥有专属工作窗口/页面，借用进程不关闭。 日常Chrome冷启动使用Windows非激活最小化的带随机标记空白窗口；独立短命守护仅回收该窗口，不关闭用户浏览器或读取Profile。 Windows后台Python入口由process模块核验GUI子系统并沿用同一venv，execution_owner保留单owner和Job脱离；统一拒绝覆盖NO_WINDOW的冲突标志，不以控制台解释器回退。站点冷却按已有时间戳纯读到期，未知执行结果仍独立对账。 | allowed_with_secret_scan | include |
| `src/` | dev_runtime | Python package source. search_providers/catalog.py is the shared search capability directory; SearXNG is retired, with old configuration read only for compatibility. runtime/search_configuration.py owns versioned configuration snapshots. generic_web uses one bounded acquisition artifact and adapter-declared read-only POST; runtime/pdf_text.py completes missing PDF pages using the shared OCR path before public evidence submission. collectors/platform/zhihu.py owns Zhihu borrowed-browser collection and target contracts; detail_strategies/zhihu.py consumes collected snapshots and shared media/OCR with resumable checkpoints. Managed Zhihu CDP, disk Cookie and signer routes are retired; CNKI retains its separate managed channel. GitHub uses github_api for explicit read-only transport and identity, gh_cli_sidecar for unmodified repository discovery, github_content for commit-pinned objects and paged discussions, and detail_strategies/github.py to submit original text to shared evidence. Shared leases, task checkpoints and evidence storage own runtime state; no browser or separate evidence database is required. 微信由collectors/platform/wechat.py提供公开页面状态、账号身份、正文与清单解析；collectors/platform/wechat_media.py只从正常原文页解析具有精确媒体身份的资源，公共层下载/转写/存证；collectors/platform/weread.py在既有OpenCLI桥上提供公众号文章发现，不复制Profile或账号存储，use_cases/wechat_search.py复用公共配置和缓存；wechat_adapter及detail_strategies/wechat.py接公共任务、媒体和证据，浏览器只借用现有OpenCLI只读桥，不增加后台写入或独立存储。 | allowed | include |
| `src/onboarding/` | dev_runtime | 同一HTML页面壳与TypeScript构建；研究与交付回执只读投影、维护者门禁及组件安装；不执行任务交付。 | allowed_with_secret_scan | include |
| `src/onboarding/assets/` | dev_runtime | Curated, non-login official documentation screenshots used by the loopback console configuration guides. They contain no account, key, or local runtime data. | allowed_with_secret_scan | include |
| `src/strategy_trees/` | dev_runtime | Unified strategy-tree declarations, shadow consistency checks, and governance/cache policy manifests. | allowed | include |
| `scripts/` | dev_runtime | 安装、配置、生成和维护脚本；verify_architecture_chain 以隔离owner验证获准的真实评论/PDF链路，保存源码身份、逐项覆盖与固定版本回读，不替代原生MCP或安装验收。 | allowed | include |
| `.codex/` | dev_runtime | Codex project hook adapter and local worktree container; never a product input. | allowed_with_trust_review | exclude |
| `.githooks/` | dev_runtime | Repository-local Git hooks that call the unified quality gate. | allowed | exclude |
| `.github/` | dev_runtime | Public GitHub automation, issue intake, and pull-request templates. | allowed_with_review | public_source_only |
| `tools/` | dev_runtime | Local maintenance and guard utilities. | allowed | exclude |
| `config/` | dev_runtime | Templates, package manifest, selectors, MCP templates, and structure manifest. | allowed_with_secret_scan | partial_include |
| `config/codex-product/` | dev_runtime | Versioned Codex plugin source and deployment contract templates. | allowed_with_secret_scan | partial_include |
| `docs/` | dev_runtime | User documentation, validation reports, and productization planning. | allowed | include |
| `project_status/` | dev_runtime | Sanitized project state packets, open issue registers, and validation semantics for AI handoff. | allowed | exclude |
| `runtime/` | local_state | Logs, reports, task state, generated media placeholders, and local runtime data. | readme_and_placeholders_only | readme_and_placeholders_only |
| `local/` | local_state | Machine-local state; local/evaluation is the canonical development ledger when explicitly configured. History is imported with evidence hashes; evaluation summaries never assert installed-product acceptance. | local_only | exclude |
| `browser_data/` | local_state | Browser automation data, CDP/session state, and platform profile working data. | local_only | exclude |
| `data/` | local_state | Legacy or runtime data; keep out of product packages unless explicitly migrated. | avoid_new_writes | exclude |
| `archive/` | history_backup | Sanitized archive index, evidence packs, and user-confirmed migration records. | sanitized_only | exclude |
| `dist/` | package_output | Generated package/build output. | generated_only | generated_output |
| `release/` | package_output | Optional generated release staging area. | generated_only | generated_output |
| `.python312/` | local_state | Optional bundled/local Python runtime. | do_not_edit | exclude_from_git_optional_in_distribution |
| `config/recruitment_city_registry.json` | dev_runtime | 公开招聘城市参数表；随产品分发，不含用户状态或凭据。 | allowed | include |
| `scripts/kr_eval/` | dev_runtime | 既有持续评测引擎：追加账本、受控历史/真实回执导入、一次CI执行及摘要投影；开发治理不随产品打包。 | allowed | exclude |
| `config/evaluation/` | dev_runtime | 版本化能力与用例合同；报告不创建新数据集，新增能力或故障仅增量维护。 | allowed_with_secret_scan | exclude |
| `tests/evaluation/` | dev_runtime | 评测机制与产品离线合同的定向回归。 | allowed | exclude |
| `src/use_cases/` | dev_runtime | 详情与本地材料的请求、任务交接和结果编排；MCP门面委托单一实现，不拥有账号、安装或第二份调度状态。 | allowed | include |
| `src/onboarding/static/` | dev_runtime | 本地控制台静态样式和脚本，含只读持续评测摘要；读取不运行评测。 | allowed | include |
| `runtime/task_artifacts/` | local_state | 持久任务输入、阶段记录和原子结果；实际位于配置数据根。 | local_only | exclude |
| `local/evaluation/` | local_state | 项目持续评测账本：不可覆盖run与基线、去重资产和数据集快照、只读摘要及待审核候选；开发治理数据不打包。 | local_only | exclude |
| `src/capability_manifest/` | dev_runtime | Thin source-ecology catalog joins registered tools/platforms to one semantic declaration; no network probes, fixed research routing or inferred verification. | allowed | include |

## Release Forbidden Patterns

- `.env`
- `.codex/**`
- `.githooks/**`
- `AGENTS.md`
- `config/runtime.env`
- `config/mcp-approvals.json`
- `config/profile_registry.json`
- `local/**`
- `project_status/**`
- `tools/**`
- `archive/**`
- `browser_data/**`
- `archive/local/**`
- `archive/**/*.sqlite`
- `archive/**/*.sqlite-*`
- `archive/**/cookies*`
- `archive/**/Cookies*`
- `archive/**/key4.db`
- `archive/**/Login Data*`
- `archive/**/Local State`
- `archive/**/Singleton*`
- `runtime/logs/**`
- `runtime/reports/**`
- `runtime/tasks/**`
- `runtime/cache/**`
- `runtime/media_cache/**`
- `runtime/*.json`
- `runtime/*.jsonl`
- `runtime/*.sqlite3`
- `runtime/*.sqlite`
- `runtime/*.sqlite-*`
- `runtime/*.db`
- `data/**`
- `src/data/**`
- `**/*.log`
- `**/*.m4a`
- `**/*.mp4`
- `**/*.part`
- `**/*.inprogress`
- `**/node_modules/**`
- `.git/**`
- `scripts/kr_eval/**`
- `scripts/kr_evaluate.py`
- `scripts/kr_test_selector.py`
- `scripts/kr_natural_calling_eval.py`
- `scripts/kr_source_ecology_eval.py`
- `config/evaluation/**`
- `tests/evaluation/**`
- `runtime/task_artifacts/**`

## Self-Documentation Watch Paths

- `.gitignore`
- `STRUCTURE.md`
- `config/package-manifest.product-lite.json`
- `config/public-source-manifest.json`
- `config/mcp-config-template.json`
- `config/profile_registry.example.json`
- `config/project-structure.manifest.json`
- `config/storage-ownership.manifest.json`
- `config/selectors.json`
- `scripts/generate-mcp-config.py`
- `scripts/setup_codex_product.py`
- `scripts/install.bat`
- `scripts/setup_accounts.bat`
- `scripts/setup_agent.bat`
- `scripts/setup_wizard.bat`
- `scripts/start_setup_wizard.py`
- `scripts/setup_product_wizard.py`
- `scripts/product_console_launcher.py`
- `src/onboarding/configuration.py`
- `src/onboarding/console_page.py`
- `src/onboarding/console_access.py`
- `src/onboarding/product_status.py`
- `src/onboarding/setup_wizard.py`
- `scripts/build_product_lite_package.py`
- `scripts/verify_release_candidate.py`
- `scripts/product_install.py`
- `scripts/kr_mcp_continuity.py`
- `scripts/build_public_source_projection.py`
- `scripts/verify_package_integrity.py`
- `scripts/verify_all_capabilities.py`
- `scripts/verify_codex_product.py`
- `scripts/verify_api_keys.py`
- `start.cmd`
- `start-knowledgeradar-server.cmd`
- `start-knowledgeradar-server-detached.py`
- `start-knowledgeradar-server-hidden.ps1`
- `pyproject.toml`
- `config/recruitment_city_registry.json`
- `config/runtime-win-py312.lock`
- `config/runtime-artifacts.json`
- `config/optional-asr-win-py312.lock`
- `config/optional-docling-win-py312.lock`
- `config/retirement-matrix.json`
- `config/license-source-inventory.json`
- `THIRD_PARTY_NOTICES.md`
- `scripts/generate_structure_docs.py`
- `scripts/generate_docs_index.py`
- `scripts/generate_script_lifecycle_inventory.py`
- `scripts/generate_process_docs_index.py`
- `scripts/check_structure_docs.py`
- `scripts/check_doc_drift.py`
- `scripts/check_docs_governance.py`
- `scripts/kr_freeze_runtime.py`
- `scripts/kr_candidate_acceptance.py`
- `scripts/kr_browser_acceptance.py`
- `src/onboarding/__init__.py`
- `src/onboarding/research_api.py`
- `scripts/verify_web_chain.py`
- `scripts/verify_github_chain.py`
- `scripts/verify_wechat_chain.py`
- `scripts/verify_architecture_chain.py`
- `scripts/generate_tool_manual.py`
- `scripts/check_current_docs.py`
- `scripts/check_mcp_tool_schema.py`

## Self-Documentation Watch Globs

- `.npmrc`
- `.codex/hooks*.json`
- `.codex/hooks/*.py`
- `.githooks/*`
- `config/mcp*.json`
- `config/delivery-profile.json`
- `config/package-manifest*.json`
- `config/public-source-manifest.json`
- `config/quality-gates.manifest.json`
- `config/project-structure.manifest.json`
- `config/storage-ownership.manifest.json`
- `scripts/*package*.py`
- `scripts/verify_release_candidate.py`
- `scripts/product_install.py`
- `scripts/kr_mcp_continuity.py`
- `scripts/build_public_source_projection.py`
- `scripts/generate-mcp-config.py`
- `scripts/setup_*.bat`
- `src/onboarding/*.py`
- `scripts/verify_*.py`
- `start*.cmd`
- `start-knowledgeradar-server*`

## Forbidden Local-State Paths

- `archive/local`

## Codex Closeout

- Update config/project-structure.manifest.json before structural, packaging, installer, profile, or runtime-boundary changes.
- `Run python scripts/build_product_lite_package.py --dry-run.`
- `Run python scripts/verify_release_candidate.py --candidate-dir <candidate-dir> after building a release candidate.`
- `Run python scripts/verify_package_integrity.py.`
- Do not hand-edit KR-GENERATED blocks.
<!-- KR-GENERATED:STRUCTURE-MATRIX:END -->
