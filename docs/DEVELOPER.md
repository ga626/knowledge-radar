# 开发者指南

本页仅面向源码 checkout。普通用户请从 [首次成功](FIRST_SUCCESS.md) 开始。

## 本地开发

在源码根目录准备 Python 3.12，按项目依赖安装后可使用 `scripts\install.bat`、`scripts\setup_wizard.bat`、`start.cmd` 和 `scripts\setup_agent.bat`。源码向导兼容读取/写入源码根 `.env`；它不等于 Release 产品的 `configure.cmd`。

运行时验证示例：

```bat
.python312\python.exe -m pytest -q tests
.python312\python.exe scripts\verify_api_keys.py
.python312\python.exe scripts\verify_all_capabilities.py --safe
```

`verify_all_capabilities.py` 在 Release 激活环境会自动把报告、日志和状态写入 `KR_DATA_ROOT`；源码 checkout 则维持 `runtime/` 开发语义。

## 开发边界

- 不提交 `.env`、Cookie、Profile、SQLite、运行日志、媒体缓存或绝对个人路径。
- 发布只从隔离 public checkout 的干净候选 artifact 进行；开发源和当前稳定安装不互相覆盖。
- 修改安装、配置、包或运行时路径时，必须做全新安装、升级、回滚与 MCP stdio 合同验证。
