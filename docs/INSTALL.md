# KnowledgeRadar 安装指南

## 环境要求

- Windows 10/11。
- Python 3.12 或更高版本，或可选的 `.python312` 项目内置运行时。
- Node.js 用于浏览器/CDP 桥接能力。
- Chrome 用于需要交互式登录态的平台。

## 安装

1. 下载或克隆仓库。
2. 在仓库根目录打开 PowerShell 或命令提示符。
3. 执行：

```bat
scripts\install.bat
```

安装脚本只在当前项目目录内创建本地文件和安装依赖，不会迁移旧的 WorkBuddy/OpenClaw 配置，也不会归档旧项目目录。

安装脚本默认使用国内加速源安装常规依赖：

- Python 依赖：清华 PyPI 镜像 `https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple`
- Node.js 依赖：npmmirror `https://registry.npmmirror.com`
- Playwright 浏览器下载：优先尝试官方源，失败后回退到 `https://cdn.npmmirror.com/binaries/playwright` 和 `https://npmmirror.com/mirrors/playwright`

如果 Playwright 浏览器下载失败，安装流程会继续，但浏览器相关能力需要补齐 `runtime\ms-playwright` 或配置代理后重新验证。可在运行安装脚本前设置 `KR_PLAYWRIGHT_PROXY=http://代理地址:端口`。

## 配置服务商

如果安装脚本尚未创建 `.env`，请从 `.env.example` 复制一份。只填写你实际要用的服务商 Key。

```bat
python scripts\verify_api_keys.py
```

缺少可选服务商时安装可以继续，但这不是最终验收状态。如果没有配置任何 Web 搜索服务商或 SearXNG 端点，Web 搜索能力会提示需要配置；配置完成后应重新验证并通过。

## 启动

```bat
start.cmd
```

默认 MCP 地址：

```text
http://127.0.0.1:18765/mcp
```

## 验证

```bat
python scripts\verify_all_capabilities.py --safe
```

首次安装后的合理预期：公开、低风险工具应通过；需要浏览器登录态的平台在完成 `scripts\setup_accounts.bat` 前可能提示需要交互或配置。完成账号/Profile 配置后，应重新运行验证；仍未通过的项目需要继续排查，不应长期停留在降级状态。
