# 第三方组件与许可

离线 wheel 保留上游原始许可文件。本清单是来源记录，不扩大任何组件的授权范围。
带用途限制且来源提交尚未确认的 69 个历史爬虫文件已从当前源码退役；逐路径排除清单保留为防止重新引入的门禁，见 config/license-source-inventory.json。退役不代表历史来源已经获得重新许可。

| 组件 | 版本 | 上游许可元数据 | wheel 内许可文件 |
|---|---|---|---|
| aiofiles | 23.2.1 | Apache-2.0 | 2 |
| annotated-types | 0.7.0 | 未声明，需人工核对 | 1 |
| anyio | 4.13.0 | MIT | 1 |
| attrs | 26.1.0 | MIT | 1 |
| babel | 2.18.0 | BSD-3-Clause | 1 |
| beautifulsoup4 | 4.14.3 | MIT License | 2 |
| certifi | 2026.4.22 | MPL-2.0 | 1 |
| cffi | 2.0.0 | MIT | 2 |
| chardet | 7.4.3 | 0BSD | 1 |
| charset-normalizer | 3.4.7 | MIT | 1 |
| click | 8.3.3 | BSD-3-Clause | 1 |
| colorama | 0.4.6 | 未声明，需人工核对 | 1 |
| courlan | 1.3.2 | Apache 2.0 | 1 |
| cryptography | 48.0.0 | Apache-2.0 OR BSD-3-Clause | 3 |
| cssselect | 1.4.0 | BSD-3-Clause | 2 |
| dateparser | 1.4.0 | BSD | 2 |
| defusedxml | 0.7.1 | PSFL | 1 |
| greenlet | 3.5.1 | MIT AND PSF-2.0 | 2 |
| h11 | 0.16.0 | MIT | 1 |
| htmldate | 1.9.4 | Apache 2.0 | 1 |
| httpcore | 1.0.9 | BSD-3-Clause | 1 |
| httpx | 0.28.1 | BSD-3-Clause | 1 |
| httpx-sse | 0.4.3 | MIT | 1 |
| idna | 3.15 | BSD-3-Clause | 1 |
| jsonschema | 4.26.0 | MIT | 1 |
| jsonschema-specifications | 2025.9.1 | MIT | 1 |
| jusText | 3.0.2 | The BSD 2-Clause License | 1 |
| lxml | 6.1.0 | BSD-3-Clause | 2 |
| lxml_html_clean | 0.4.4 | BSD-3-Clause | 1 |
| mcp | 1.27.1 | MIT | 1 |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause | 3 |
| pillow | 12.2.0 | MIT-CMU | 1 |
| playwright | 1.60.0 | Apache-2.0 | 2 |
| pycparser | 3.0 | BSD-3-Clause | 1 |
| pydantic | 2.13.4 | MIT | 1 |
| pydantic_core | 2.46.4 | MIT | 1 |
| pydantic-settings | 2.14.1 | MIT | 1 |
| pyee | 13.0.1 | MIT | 1 |
| PyJWT | 2.12.1 | MIT | 2 |
| pypdf | 6.13.1 | BSD-3-Clause | 1 |
| python-dateutil | 2.9.0.post0 | Dual License | 1 |
| python-dotenv | 1.0.1 | BSD-3-Clause | 1 |
| python-multipart | 0.0.28 | Apache-2.0 | 1 |
| pytz | 2026.2 | MIT | 1 |
| pywin32 | 311 | PSF | 0 |
| readability-lxml | 0.8.4.1 | Apache License 2.0 | 1 |
| referencing | 0.37.0 | MIT | 1 |
| regex | 2026.5.9 | Apache-2.0 AND CNRI-Python | 1 |
| requests | 2.34.0 | Apache-2.0 | 2 |
| rpds-py | 0.30.0 | MIT | 1 |
| setuptools | 81.0.0 | MIT | 15 |
| six | 1.17.0 | MIT | 1 |
| soupsieve | 2.8.3 | MIT | 1 |
| sse-starlette | 3.4.4 | BSD-3-Clause | 2 |
| starlette | 1.0.0 | BSD-3-Clause | 1 |
| tld | 0.13.2 | MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later | 4 |
| trafilatura | 2.0.0 | Apache 2.0 | 1 |
| typing_extensions | 4.15.0 | PSF-2.0 | 1 |
| typing-inspection | 0.4.2 | MIT | 1 |
| tzdata | 2026.2 | Apache-2.0 | 2 |
| tzlocal | 5.3.1 | MIT | 1 |
| urllib3 | 2.7.0 | MIT | 1 |
| uvicorn | 0.46.0 | BSD-3-Clause | 1 |
| websocket-client | 1.9.0 | Apache-2.0 | 1 |
| wheel | 0.47.0 | MIT | 1 |
| youtube-transcript-api | 1.2.4 | MIT | 1 |

浏览器、ASR模型、FFmpeg、下载器等可选资产不因安装核心 wheel 而获得许可或可用状态；其来源与身份单独记录。

## 按需安装的本地处理组件

本地转写采用 Qwen3-ASR-0.6B-hf 与 Transformers，扫描件采用 PaddleOCR-VL-1.6 与 PP-DocLayoutV3；其项目代码和模型顶层许可为 Apache-2.0。Office 转换采用 MIT 许可的 Docling，不启用其 PDF OCR 或语音模型。具体版本、官方来源和固定权重 revision 由 `src/runtime/model_roles.py` 统一声明。

这些组件不放入核心安装包，也不在推理时自动下载。显式准备使用随包全量 wheel 版本及 SHA 锁，生成 `wheel-inventory.json` 和独立安装回执。顶层许可记录与传递依赖元数据清单不等于完整法律审查；正式分发可选依赖或权重前须按其实际清单履行许可义务。云端媒体理解仅调用用户配置的百炼服务，不分发云模型权重。
