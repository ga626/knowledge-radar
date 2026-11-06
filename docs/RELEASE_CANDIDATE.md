# 发布候选合同

本仓库的候选包只从干净的公开 checkout 生成：

```bat
python scripts\build_product_lite_package.py --candidate
python scripts\verify_release_candidate.py --candidate-dir release\candidates\v<版本>-<完整提交 SHA>
```

候选目录包含同一份 `KnowledgeRadar.zip` 与 `candidate-receipt.json`。收据绑定源码提交、归档 SHA-256、包清单与 SBOM 内容清单；CI构建另外绑定repository/run_id/run_attempt/event/ref。验证在隔离临时状态执行 MCP `initialize`、`notifications/initialized` 和完整 `tools/list` 合同比对；不读取、迁移或写入用户认证或Codex配置，也不证明实际宿主已经重载。

公开Actions只保留评测证据，不上传内部安装ZIP。非纯文档main CI验包后，向核实为仅本人可访问的 `ga626/knowledge-radar-internal-builds` 私有仓库交付。内部tag为 `internal-v<version>-r<run_id>.<run_attempt>`；先建草稿、上传ZIP/原始receipt及 `KnowledgeRadar-<version>-SHA256SUMS.txt`，核验远端摘要后才发布私有prerelease。同构建相同资产可恢复；不同字节或已发布却缺附件则拒绝覆盖。旧experiment-SHA构建保留历史身份。

内部交付不自动安装或激活。下载者先核源码、run/attempt、ZIP/receipt摘要，再使用现有安装器的maintainer-main渠道；安装回读、原生双摘要和业务测试分别记录。正式公开须获针对该制品的明确认可，提升同一ZIP/原始receipt，公开下载后再次核SHA-256，不重建。
