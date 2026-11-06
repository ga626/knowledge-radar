# 第三方组件与许可

离线 wheel 保留上游原始许可文件。本清单是来源记录，不扩大任何组件的授权范围。
带用途限制且来源提交尚未确认的 69 个历史爬虫文件已从当前源码退役；逐路径排除清单保留为防止重新引入的门禁，见 config/license-source-inventory.json。退役不代表历史来源已经获得重新许可。

| 组件 | 版本 | 上游许可元数据 | wheel 内许可文件 |
|---|---|---|---|
| aiofiles | 23.2.1 | Apache-2.0 | 2 |
| annotated-types | 0.7.0 | 未声明，需人工核对 | 1 |
| anyio | 4.13.0 | MIT | 1 |
| attrs | 26.1.0 | MIT | 1 |
| av | 17.0.1 | BSD-3-Clause | 4 |
| babel | 2.18.0 | BSD-3-Clause | 1 |
| beautifulsoup4 | 4.14.3 | MIT License | 2 |
| certifi | 2026.4.22 | MPL-2.0 | 1 |
| cffi | 2.0.0 | MIT | 2 |
| chardet | 7.4.3 | 0BSD | 1 |
| charset-normalizer | 3.4.7 | MIT | 1 |
| click | 8.5.0 | BSD-3-Clause | 1 |
| colorama | 0.4.6 | 未声明，需人工核对 | 1 |
| courlan | 1.3.2 | Apache 2.0 | 1 |
| cryptography | 48.0.0 | Apache-2.0 OR BSD-3-Clause | 3 |
| cssselect | 1.4.0 | BSD-3-Clause | 2 |
| dateparser | 1.4.0 | BSD | 2 |
| ddgs | 9.16.1 | MIT | 1 |
| defusedxml | 0.7.1 | PSFL | 1 |
| greenlet | 3.5.1 | MIT AND PSF-2.0 | 2 |
| h11 | 0.16.0 | MIT | 1 |
| htmldate | 1.9.4 | Apache 2.0 | 1 |
| httpcore | 1.0.9 | BSD-3-Clause | 1 |
| httpx | 0.28.1 | BSD-3-Clause | 1 |
| httpx-sse | 0.4.3 | MIT | 1 |
| idna | 3.20 | BSD-3-Clause | 1 |
| jsonschema | 4.26.0 | MIT | 1 |
| jsonschema-specifications | 2025.9.1 | MIT | 1 |
| jusText | 3.0.2 | The BSD 2-Clause License | 1 |
| lxml | 6.1.0 | BSD-3-Clause | 2 |
| lxml_html_clean | 0.4.4 | BSD-3-Clause | 1 |
| mcp | 1.27.1 | MIT | 1 |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | 45 |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause | 3 |
| pillow | 12.2.0 | MIT-CMU | 1 |
| playwright | 1.60.0 | Apache-2.0 | 2 |
| primp | 2.0.1 | MIT License | 0 |
| pycparser | 3.0 | BSD-3-Clause | 1 |
| pydantic | 2.13.4 | MIT | 1 |
| pydantic_core | 2.46.4 | MIT | 1 |
| pydantic-settings | 2.14.1 | MIT | 1 |
| pyee | 13.0.1 | MIT | 1 |
| PyJWT | 2.12.1 | MIT | 2 |
| pypdf | 6.13.1 | BSD-3-Clause | 1 |
| pypdfium2 | 5.14.0 | BSD-3-Clause, Apache-2.0, dependency licenses | 18 |
| python-dateutil | 2.9.0.post0 | Dual License | 1 |
| python-dotenv | 1.0.1 | BSD-3-Clause | 1 |
| python-multipart | 0.0.28 | Apache-2.0 | 1 |
| pytz | 2026.2 | MIT | 1 |
| pywin32 | 311 | PSF | 0 |
| readability-lxml | 0.8.4.1 | Apache License 2.0 | 1 |
| referencing | 0.37.0 | MIT | 1 |
| regex | 2026.5.9 | Apache-2.0 AND CNRI-Python | 1 |
| requests | 2.34.2 | Apache-2.0 | 2 |
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

PyAV wheel 捆绑的 FFmpeg 库与其顶层代码分别保留原始许可；完整许可文件登记包括 .libs/，不能仅用顶层许可覆盖二进制依赖。NumPy 同样保留其二进制依赖通知。

## 按需安装的本地处理组件

本地转写采用 Qwen3-ASR 0.6B 与 sherpa-onnx 2.0.1 CPU INT8，代码和模型顶层许可为 Apache-2.0；转换权重来自 sherpa-onnx 官方 asr-models 资产，固定归档及文件 SHA，仅接受中文、英文及中英混合音频。默认扫描件 OCR 调用硅基流动 PaddleOCR-VL 1.5，PDFium 只在本地渲染页面，不加载 OCR 模型。Office 转换采用 MIT 许可的 docling-slim，仅启用 format-office、format-web、convert-core；与 ASR 在同一解释器执行，不安装 Docling 推理、PDF OCR 或语音模型。具体职责和固定权重 revision 由 `src/runtime/model_roles.py` 统一声明。

本地模型组件不放入核心安装包，也不在推理时自动下载。显式准备使用随包全量 wheel 版本及 SHA 锁，生成 `wheel-inventory.json` 和独立安装回执。顶层许可记录与传递依赖元数据清单不等于完整法律审查；正式分发可选依赖或权重前须按其实际清单履行许可义务。云端 OCR 和媒体理解调用用户配置的硅基流动与百炼服务，不分发云模型权重。

## 微信只读解析参考

微信DOM字段与懒加载图片的适配参考jackwener/OpenCLI，
上游提交97dc0cd5214b9602bcda78d7422f231ee8a803e9，Apache-2.0：
https://github.com/jackwener/OpenCLI 。KR只复用现有OpenCLI浏览器桥与重新实现的只读提取合同；
不引入该模块的草稿写入、文件下载和验证轮询。原Apache-2.0许可与OpenCLI致谢继续适用。

## 微信读书关联的文章搜一搜字段

卡片选择器和搜索入口参考 jayhigh425/wechat-article-search-skill，固定提交
50efea1a0ebd6a29be6a64e5761a382cf94bbc05，MIT License。
仅借鉴文章字段与页面状态；未采用独立CDP/Profile、Cookie导出或window.open改写。
Copyright (c) 2026 jayhigh425。

MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
