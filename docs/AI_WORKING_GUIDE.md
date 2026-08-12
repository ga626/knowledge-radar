# AI Working Guide

<!-- KR-GENERATED:AI-STRUCTURE-RULES:START -->
Codex must treat `AGENTS.md` as the short auto-loaded guard and this file as the expanded working guide.

Before changing directory layout, path rules, package manifests, installer entrypoints, profile templates, runtime boundaries, or generated package content, update `config/project-structure.manifest.json` first.

Generated package output is one-way only: source -> generated package. Never patch package output and sync it back into source.

`docs/` 是公开源码的用户文档入口。公共投影不携带维护者的文档生成器、hooks 或巡检任务；不要在此 checkout 引用私有开发根才拥有的脚本。

公开发行边界由 `config/project-structure.manifest.json`、`config/package-manifest.product-lite.json` 和 `scripts/verify_package_integrity.py` 共同验证。新增用户文档或产品文件时，先更新相应 manifest，再做候选包验证。

Required closeout:

1. Update config/project-structure.manifest.json before structural, packaging, installer, profile, or runtime-boundary changes.
2. Run python scripts/build_product_lite_package.py --dry-run.
3. Run python scripts/verify_package_integrity.py.
7. Do not hand-edit KR-GENERATED blocks.
<!-- KR-GENERATED:AI-STRUCTURE-RULES:END -->
