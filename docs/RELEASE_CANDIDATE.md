# 发布候选合同

本仓库的候选包只从干净的公开 checkout 生成：

```bat
python scripts\build_product_lite_package.py --candidate
python scripts\verify_release_candidate.py --candidate-dir release\candidates\v<版本>-<完整提交 SHA>
```

候选目录包含 `KnowledgeRadar.zip` 与 `candidate-receipt.json`，绑定源码提交、归档 SHA-256、包清单、SBOM 和生产 run/attempt。公开 PR 对公共运行边界变更并行打临时样本，在 checkout 外以锁定运行时启动 owner、读回固定证据、验临时双端交付和退出；只留脱敏诊断，不上传 ZIP。合并不自动制包。

确需安装时，先核 `ga626/knowledge-radar-internal-builds` 仅本人可访问，再用其 `Private KR candidate` workflow 指定已合并的当前产品 main 完整 SHA 和成功 PR run。私有 runner 制包一次；无 checkout 的消费 job 按生产制品 ID 取原包，在隔离环境复验并生成安装交接回执。消费者重试仍用原生产 attempt；不靠重打包处理传输失败。只下载同时有成功交接和匹配摘要的原包；保留本地已验原包用于回退，私有 Actions 临时留存不当长期仓库。早期私有 tag 保留历史身份，不再作日用交付入口。

内部交付不自动激活。核源码、制品 ID、生产 run/attempt、ZIP 与安装交接回执后，用现有安装器的 `maintainer-main` 渠道。安装回读、原生双摘要和受影响业务分别记录。正式公开须针对同一已验 ZIP 明确批准，公开下载后再核 SHA-256。

激活前先验同包身份、所选组件及注册所有权；准备冲突即停止，不切active或写Codex配置。旧制品、数据目录和回滚记录保留；切回旧程序不等于撤销数据迁移。激活后核真实运行身份、原生双摘要及本次受影响的真实边界，不重跑全部离线或大考。
