# 发布候选合同

本仓库的候选包只从干净的公开 checkout 生成：

```bat
python scripts\build_product_lite_package.py --candidate
python scripts\verify_release_candidate.py --candidate-dir release\candidates\v<版本>-<完整提交 SHA>
```

候选目录包含同一份 `KnowledgeRadar.zip`、`candidate-receipt.json` 与 Actions artifact。收据绑定源码提交、归档 SHA-256、包清单与 SBOM 内容清单。验证会在临时目录解压，并以临时运行状态执行 MCP `initialize` 和 `tools/list`；它不会读取、迁移或写入用户的 API Key、浏览器资料、profile、日志或 Codex 配置。

候选不是 Release，也不会自动安装或激活。正式发布只能提升已经验证的同一 ZIP，并在 GitHub 下载后再次核对 SHA-256。
