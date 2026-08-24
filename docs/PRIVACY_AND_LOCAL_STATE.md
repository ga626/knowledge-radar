# 隐私与本地状态

公开仓库和 Release 只包含产品代码与配置模板，不包含任何人的密钥、账号、浏览记录、Cookie、Profile、任务内容、报告、日志或下载媒体。

安装后，程序版本与用户数据分离：程序位于 `%LOCALAPPDATA%\KnowledgeRadar\app\<version>`，数据默认位于 `%LOCALAPPDATA%\KnowledgeRadar\data`。更新只替换程序版本，不覆盖数据。可在安装时用 `--install-root`、`--data-root` 选择其他本地可写盘；已安装数据迁移必须遵循 [产品安装与更新](PRODUCT_INSTALL.md) 的 `plan → 确认 → apply → verify/rollback` 流程。

## 数据分类与清理边界

本地健康台只显示分类容量，不会显示路径、文件名或内容。分类由公开的 `config/storage-ownership.manifest.json` 定义：

| 分类 | 自动操作 |
| --- | --- |
| 配置、受保护备份、浏览器资料、运行状态、日志、历史归档 | 永不自动处理 |
| 模型、Playwright、通用缓存、隔离区 | 仅由用户手动管理 |
| 已登记且过期的媒体缓存 | 只会移入可恢复隔离区，不会直接删除 |

隔离操作只接受媒体缓存 manifest 已登记、且 TTL 已过期的文件；未知文件保持原状。隔离区内文件保留到用户确认恢复或最终删除为止。Chrome 派生组件与模型没有被自动删除、硬链接或强制共用：它们可能由上游浏览器更新机制管理，产品只提供安全的数据根迁移。

## 公开与诊断边界

服务默认仅监听 `127.0.0.1`。本地配置页面不回显密钥，也不写访问日志；诊断仅返回脱敏的就绪状态和分类总量。不要把 `.env`、`config/profile_registry.json`、浏览器资料、SQLite、Cookie、日志、报告、缓存或本机截图上传到 issue、仓库、归档或 Release。

发布前由包清单与完整性检查拒绝常见私密路径和凭据模式：

```bat
python scripts\build_product_lite_package.py
python scripts\verify_package_integrity.py --path dist\product-lite\KnowledgeRadar
python scripts\build_public_source_projection.py
python scripts\verify_package_integrity.py --path dist\public-source\KnowledgeRadar
```
