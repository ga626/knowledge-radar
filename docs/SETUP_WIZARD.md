# 本地配置向导（源码 checkout）

本页只描述源码 checkout。Release 用户请运行安装器生成的 `%LOCALAPPDATA%\KnowledgeRadar\configure.cmd`，并阅读 [首次成功](FIRST_SUCCESS.md)；它会从 `active.json` 解析当前 data 根，不会把配置误写进版本化 app 目录。

`scripts\setup_wizard.bat` 会在当前源码项目中启动一个一次性的配置页面，帮助开发者填写自己要用的服务商配置。

## 使用

```bat
scripts\setup_wizard.bat
```

页面只监听 `127.0.0.1` 的随机端口，并且只写入仓库根目录 `.env`。它不会上传、打印或回显 API Key；已配置项目只显示“已配置”，留空不会清除原有值。

完成后关闭向导，再运行：

```bat
python scripts\verify_api_keys.py
python scripts\verify_all_capabilities.py --safe
```

## 安全边界

- 服务不监听局域网地址，也不使用云端或远程服务器。
- 写入请求必须同时带有同源 Origin 和每次启动随机生成的本地会话令牌。
- 配置页面禁止被嵌入，禁用缓存和 Referer，并且不写访问日志。
- 向导只接受明确列出的公开文档中的服务商字段；不会接收或改写 profile、Cookie、MCP 配置或任意环境变量。

如果你更喜欢手工配置，源码 checkout 可继续编辑根 `.env`；向导不是唯一入口，也不会影响源码的现有启动或 MCP 命令。
