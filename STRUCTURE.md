# KnowledgeRadar Structure

<!-- KR-GENERATED:STRUCTURE-MATRIX:START -->
Source of truth: `config/project-structure.manifest.json`.

Development source is the only editable truth. Package output is regenerated from source and manifests.

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
| `src/runtime/` | dev_runtime | Task-local account failover and spend admission; one browser-session event source for account alerts, exact-profile recovery and desktop notification; installed component resolution. | allowed_with_secret_scan | include |
| `src/` | dev_runtime | Python package source. | allowed | include |
| `src/onboarding/` | dev_runtime | 同一显式 HTML 页面壳、原生 TypeScript 构建；研究账本只读投影、维护者身份门禁和显式成果导出。 | allowed_with_secret_scan | include |
| `src/onboarding/assets/` | dev_runtime | Curated, non-login official documentation screenshots used by the loopback console configuration guides. They contain no account, key, or local runtime data. | allowed_with_secret_scan | include |
| `src/strategy_trees/` | dev_runtime | Unified strategy-tree declarations, shadow consistency checks, and governance/cache policy manifests. | allowed | include |
| `scripts/` | dev_runtime | Install, setup, validation, generation, and maintenance scripts. | allowed | include |
| `.codex/` | dev_runtime | Codex project hook adapter and local worktree container; never a product input. | allowed_with_trust_review | exclude |
| `.githooks/` | dev_runtime | Repository-local Git hooks that call the unified quality gate. | allowed | exclude |
| `.github/` | dev_runtime | Public GitHub automation, issue intake, and pull-request templates. | allowed_with_review | public_source_only |
| `tools/` | dev_runtime | Local maintenance and guard utilities. | allowed | exclude |
| `config/` | dev_runtime | Templates, package manifest, selectors, MCP templates, and structure manifest. | allowed_with_secret_scan | partial_include |
| `config/codex-product/` | dev_runtime | Versioned Codex plugin source and deployment contract templates. | allowed_with_secret_scan | partial_include |
| `docs/` | dev_runtime | User documentation, validation reports, and productization planning. | allowed | include |
| `project_status/` | dev_runtime | Sanitized project state packets, open issue registers, and validation semantics for AI handoff. | allowed | exclude |
| `bridge/` | dev_runtime | Node bridge and MCP integration helpers. | allowed | include_without_node_modules |
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
| `src/use_cases/` | dev_runtime | 业务用例编排，MCP门面委托单一实现。 | allowed | include |
| `src/onboarding/static/` | dev_runtime | 本地控制台静态样式和脚本，含只读持续评测摘要；读取不运行评测。 | allowed | include |
| `runtime/task_artifacts/` | local_state | 持久任务输入、阶段记录和原子结果；实际位于配置数据根。 | local_only | exclude |
| `local/evaluation/` | local_state | 项目持续评测账本：不可覆盖run与基线、去重资产和数据集快照、只读摘要及待审核候选；开发治理数据不打包。 | local_only | exclude |

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
- `bridge/.npmrc`
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

## Self-Documentation Watch Globs

- `.npmrc`
- `bridge/.npmrc`
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
