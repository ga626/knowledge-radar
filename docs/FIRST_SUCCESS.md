# First success / 首次成功

本页面向 Release 用户。源码 checkout、HTTP 调试和 pytest 请看 [开发者指南](DEVELOPER.md)。

## 你需要准备什么

- Windows 与 Python 3.12。
- 从同一个 GitHub Release 下载的 ZIP、SHA-256 文件和 `candidate-receipt.json`。
- 任选一个你愿意启用的 Provider；不需要一次配置全部能力。

## 正确的安装与配置路径

1. 解压 Release，在目录内执行 `python scripts\product_install.py apply --channel stable --archive KnowledgeRadar.zip --receipt candidate-receipt.json`。
2. 打开 `%LOCALAPPDATA%\KnowledgeRadar\configure.cmd`；不要从解压目录或 app 目录运行 `setup_wizard.bat`。
3. 只填写需要启用的字段，保存后关闭页面。配置写入 `%LOCALAPPDATA%\KnowledgeRadar\data\config\runtime.env`，不回显任何值。
4. 按 Codex 的方式重启/刷新 MCP，再调用 `health_check` 与 `get_capabilities`。

## 什么算第一次成功

- Codex 中只有一个 `mcp_servers.knowledgeradar` 配置 block。
- `health_check` 能返回本机状态；`get_capabilities` 返回当前工具面。
- 未配置或需要登录的可选能力会给出状态和下一步，不会导致核心工具“假通过”。

## 升级与回滚

对新 Release 重复安装命令即可更新。数据根不会被覆盖；如需退回上一稳定版本，使用：

```bat
python scripts\product_install.py rollback
```

回滚会同步恢复 `active.json`、Codex MCP block 和 `configure.cmd` 指向，但不会删除你的 data 根。完整细节见 [产品安装与更新](PRODUCT_INSTALL.md)。
