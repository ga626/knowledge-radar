"""登记离线 wheel 身份和随包保留的许可；不下载、不更新依赖。"""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import zipfile


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_text_sha(path):
    """Git eol=lf 的制品文本身份，避免 Windows 工作区换行污染依赖回执。"""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def freeze(root: Path, wheelhouse: Path):
    rows = []
    for path in sorted(wheelhouse.glob("*.whl")):
        with zipfile.ZipFile(path) as wheel:
            metadata_name = next(n for n in wheel.namelist() if n.endswith(".dist-info/METADATA"))
            metadata = BytesParser().parsebytes(wheel.read(metadata_name))
            license_paths = [n for n in wheel.namelist() if ".dist-info/" in n and
                             any(x in n.lower() for x in ("license", "copying", "notice"))]
            rows.append({"file": path.name, "sha256": sha(path), "size": path.stat().st_size,
                         "name": metadata["Name"], "version": metadata["Version"],
                         "license_expression": metadata.get("License-Expression", ""),
                         "license_metadata": str(metadata.get("License", ""))[:4000],
                         "license_files": [{"path": n, "sha256": hashlib.sha256(wheel.read(n)).hexdigest()}
                                           for n in license_paths]})
    if not rows:
        raise ValueError("wheelhouse is empty")
    lock = root / "config/runtime-win-py312.lock"
    payload = {"schema": "knowledgeradar-runtime-artifacts/v1", "target": "win_amd64-cpython-3.12",
               "lock_sha256": canonical_text_sha(lock), "pyproject_sha256": canonical_text_sha(root / "pyproject.toml"), "wheels": rows,
               "optional_assets": {"browser": "separately provisioned; record executable hash at readiness check",
                                   "model": "separately provisioned; model identity is required by transcription cache"}}
    (root / "config/runtime-artifacts.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    notices = ["# 第三方组件与许可\n", "离线 wheel 保留上游原始许可文件。本清单是来源记录，不扩大任何组件的授权范围。",
               "带用途限制且来源提交尚未确认的 69 个历史爬虫文件已从当前源码退役；逐路径排除清单保留为防止重新引入的门禁，见 config/license-source-inventory.json。退役不代表历史来源已经获得重新许可。\n",
               "| 组件 | 版本 | 上游许可元数据 | wheel 内许可文件 |", "|---|---|---|---|"]
    for row in rows:
        license_text = (row["license_expression"] or row["license_metadata"] or "未声明，需人工核对").splitlines()[0].replace("|", "/")[:140]
        notices.append(f"| {row['name']} | {row['version']} | {license_text} | {len(row['license_files'])} |")
    notices += ["", "浏览器、ASR模型、FFmpeg、下载器等可选资产不因安装核心 wheel 而获得许可或可用状态；其来源与身份单独记录。"]
    notices += ["", "## 按需安装的本地处理组件", "",
                "本地转写采用 Qwen3-ASR-0.6B-hf 与 Transformers，扫描件采用 PaddleOCR-VL-1.6 与 PP-DocLayoutV3；其项目代码和模型顶层许可为 Apache-2.0。Office 转换采用 MIT 许可的 Docling，不启用其 PDF OCR 或语音模型。具体版本、官方来源和固定权重 revision 由 `src/runtime/model_roles.py` 统一声明。", "",
                "这些组件不放入核心安装包，也不在推理时自动下载。显式准备使用随包全量 wheel 版本及 SHA 锁，生成 `wheel-inventory.json` 和独立安装回执。顶层许可记录与传递依赖元数据清单不等于完整法律审查；正式分发可选依赖或权重前须按其实际清单履行许可义务。云端媒体理解仅调用用户配置的百炼服务，不分发云模型权重。"]
    (root / "THIRD_PARTY_NOTICES.md").write_text("\n".join(notices) + "\n", encoding="utf-8")
    print(json.dumps({"wheels": len(rows), "bytes": sum(r["size"] for r in rows), "lock_sha256": payload["lock_sha256"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheelhouse", default="local/runtime-wheelhouse")
    args = parser.parse_args()
    freeze(Path(__file__).resolve().parents[1], Path(args.wheelhouse))
