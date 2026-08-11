"""
通用视频内容处理模块 video_processor.py
=============================================
三层视频处理流水线：
  层1: 长文本摘要 (transcript > 3000 chars → 压缩)
  层2: 信息密度评分 + 决策分流 (FULL_ANALYSIS ≥ 7 | FAST_SUMMARY < 7)
  层3: 精品通道 - 千问多模态深度视频理解 (仅在 FULL_ANALYSIS 时触发)

依赖:
  - httpx, yt-dlp, faster-whisper
  - ffmpeg (通过 pyffmpeg 安装至 ~/.pyffmpeg/bin)
  - 多模态 L2: Bailian Qwen3-VL-Flash；SiliconFlow 仅显式 fallback
  - 文本评分/摘要: Bailian text model；SiliconFlow 仅显式 fallback
"""

import base64
import json
import logging
import os
import re
from runtime.process import silent_subprocess_run
import tempfile
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple


from runtime.media_cache import media_cache_subdir, record_media_cache_entry
from runtime.paths import resolve_runtime_media_file
from runtime.proxy_config import get_yt_dlp_proxy

# ─── 日志 ──────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="[video_processor] %(message)s")
log = logging.getLogger(__name__)

# ─── API 配置 ──────────────────────────────────────────────────────────

SUMMARIZE_THRESHOLD_CHARS = 3000       # transcript 摘要阈值
MAX_FRAMES = 8                          # 多模态分析最大帧数
FRAME_INTERVAL_SEC = 15                 # 帧提取间隔（秒）
FFMPEG_PATH = os.environ.get("KR_FFMPEG_EXE", "ffmpeg")


# ─── 数据结构 ──────────────────────────────────────────────────────────

@dataclass
class VideoInfo:
    """视频处理模块的输入数据"""
    title: str
    description: str = ""
    transcript: str = ""
    filtered_comments: List[Dict] = field(default_factory=list)
    duration_seconds: int = 0
    video_url: str = ""                  # 原始视频URL（如B站地址）
    video_path: str = ""                 # 本地视频文件路径（如有缓存）

    @property
    def transcript_chars(self) -> int:
        return len(self.transcript)

    @property
    def comment_count(self) -> int:
        return len(self.filtered_comments)


@dataclass
class ProcessingResult:
    """视频处理完整结果"""
    score: int
    decision: str
    summary: str
    scoring_rationale: str
    summary_was_needed: bool = False
    original_transcript_chars: int = 0
    used_model: str = ""
    deep_analysis: Optional[Dict] = None   # 精品通道分析结果
    deep_analysis_time_s: float = 0.0
    stages: List[Dict] = field(default_factory=list)

    def to_dict(self) -> Dict:
        d = asdict(self)
        # 移除冗余的 stages（打印时已有）
        if not d.get("deep_analysis"):
            d.pop("deep_analysis", None)
        return d


# ─── LLM 文本调用层 ───────────────────────────────────────────────────

def _call_llm(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> Tuple[str, str]:
    from media_policy import MediaModelPolicy
    from understanding.bailian import call_text_model

    policy = MediaModelPolicy.from_env()
    from runtime.media_provider_preflight import classify_provider_error, preflight_contract
    models = tuple(_first_provider_model((candidate,), "bailian") for candidate in policy.ordered_models("comment_filter"))
    models = tuple(model for model in models if model)
    last_error: Exception | None = None
    for model in models:
        contract = preflight_contract(f"bailian:{model}", capability="video_text_analysis")
        if contract["status"] == "blocked_model":
            log.warning("[provider] skip blocked model %s", model)
            continue
        try:
            return call_text_model(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=120,
                capability="video_text_analysis",
            )[:2]
        except Exception as exc:
            last_error = exc
            log.warning("[provider] text model %s failed: %s", model, classify_provider_error(exc)["failure_class"])
    if last_error is not None and not _siliconflow_fallback_enabled():
        raise last_error
    if not _siliconflow_fallback_enabled():
        raise RuntimeError("No usable Bailian text model configured for video analysis")
    from understanding.siliconflow import call_text_models, configured_models
    return call_text_models(
        models=configured_models("video"),
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=120,
    )


def _call_multimodal(
    system_prompt: str,
    user_text: str,
    images_base64: List[str],
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> Tuple[str, str]:
    """
    调用多模态模型（支持 base64 图片）。
    默认走 Bailian Qwen3-VL-Flash；只有显式启用 fallback 时才回退 SiliconFlow。
    """
    from media_policy import MediaModelPolicy
    from understanding.bailian import call_frame_images_model
    from runtime.media_provider_preflight import classify_provider_error, preflight_contract

    model = _first_provider_model(MediaModelPolicy.from_env().frame_vision_models, "bailian") or "qwen3-vl-flash"
    contract = preflight_contract(f"bailian:{model}", capability="frame_vision")
    if contract["status"] == "blocked_model":
        raise RuntimeError(f"Blocked media model: {model}")
    try:
        return call_frame_images_model(
            images_base64,
            model=model,
            prompt=user_text,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            timeout=300,
        )[:2]
    except Exception as exc:
        log.warning("[provider] frame model %s failed: %s", model, classify_provider_error(exc)["failure_class"])
        if not _siliconflow_fallback_enabled():
            raise
        from understanding.siliconflow import call_multimodal_models, configured_models

        return call_multimodal_models(
            models=configured_models("video"),
            system_prompt=system_prompt,
            user_text=user_text,
            images_base64=images_base64,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=300,
        )


def _siliconflow_fallback_enabled() -> bool:
    return (os.environ.get("KR_ENABLE_SILICONFLOW_FALLBACK") or "").strip().lower() in {"1", "true", "yes", "on"}


def _first_provider_model(models: Tuple[str, ...], provider: str) -> str:
    prefix = f"{provider}:"
    for model in models:
        if model.lower().startswith(prefix):
            return model.split(":", 1)[1].strip()
    return ""


def _extract_json(text: str) -> dict:
    """从 LLM 输出中提取 JSON"""
    for marker in ["```json", "```"]:
        if marker in text:
            for p in text.split(marker):
                p = p.strip()
                if (p.startswith("{") and p.endswith("}")) or (p.startswith("[") and p.endswith("]")):
                    try:
                        return json.loads(p)
                    except json.JSONDecodeError:
                        continue
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end+1])
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise ValueError(f"Cannot extract JSON: {text[:200]}")


# ─── 视频帧提取 ─────────────────────────────────────────────────────

def _extract_frames(video_path: str, max_frames: int = MAX_FRAMES) -> List[str]:
    """从视频提取关键帧，返回 base64 列表"""
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    with tempfile.TemporaryDirectory() as tmpdir:
        pattern = os.path.join(tmpdir, "frame_%03d.jpg")
        cmd = [
            FFMPEG_PATH, "-i", video_path,
            "-vf", f"fps=1/{FRAME_INTERVAL_SEC},scale=640:-1",
            "-q:v", "2", "-y", pattern,
        ]
        silent_subprocess_run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

        frames = sorted(os.listdir(tmpdir))
        if not frames:
            # 视频太短，尝试截取第一帧
            single = os.path.join(tmpdir, "single.jpg")
            silent_subprocess_run(
                [FFMPEG_PATH, "-i", video_path, "-vframes", "1", "-q:v", "2", "-y", single],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            frames = sorted(os.listdir(tmpdir))

        # 限制帧数，均匀采样
        selected = frames[:max_frames]
        # 如果帧数超过 max_frames，均匀采样
        if len(frames) > max_frames:
            indices = [int(i * len(frames) / max_frames) for i in range(max_frames)]
            selected = [frames[i] for i in indices]

        result = []
        for fname in selected:
            fpath = os.path.join(tmpdir, fname)
            with open(fpath, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            result.append(b64)

        log.info(f"[frames] Extracted {len(result)} frames from {os.path.basename(video_path)}")
        return result


# ─── 下载B站视频 ─────────────────────────────────────────────────────

def _download_bilibili_video(bvid: str, output_dir: str) -> str:
    """通过 yt-dlp 下载 B 站视频画面流，返回 mp4 文件路径"""
    from yt_dlp import YoutubeDL

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, f"{bvid}_video.mp4")
    url = f"https://www.bilibili.com/video/{bvid}"

    if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
        log.info(f"[download] Video already cached: {out_path}")
        record_media_cache_entry(out_path, kind="video", content_id=bvid, source_url=url)
        return out_path

    ydl_opts = {
        "format": "bv[ext=mp4]/bv",
        "outtmpl": out_path,
        "noplaylist": True,
        "playlist_items": "1",
        "socket_timeout": 30,
        "retries": 1,
        "fragment_retries": 1,
        "http_headers": {
            "Origin": "https://www.bilibili.com",
            "Referer": "https://www.bilibili.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
        },
        "quiet": True,
        "no_warnings": True,
    }
    proxy = get_yt_dlp_proxy()
    if proxy:
        ydl_opts["proxy"] = proxy
    with YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(url, download=True)

    if not os.path.exists(out_path) or os.path.getsize(out_path) < 1024:
        raise RuntimeError(f"Failed to download video: {out_path}")

    record_media_cache_entry(out_path, kind="video", content_id=bvid, source_url=url)
    log.info(f"[download] Video saved: {out_path} ({os.path.getsize(out_path)//1024}KB)")
    return out_path


def _download_generic_video(video_url: str, output_dir: str, video_key: str) -> str:
    """Download a low-resolution video stream for frame extraction."""
    from yt_dlp import YoutubeDL

    os.makedirs(output_dir, exist_ok=True)
    safe_key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", video_key).strip("_") or "video"
    out_path = os.path.join(output_dir, f"{safe_key}_video.mp4")
    if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
        log.info(f"[download] Video already cached: {out_path}")
        record_media_cache_entry(out_path, kind="video", content_id=safe_key, source_url=video_url)
        return out_path

    proxy = get_yt_dlp_proxy()
    ydl_opts = {
        "format": "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/worst[ext=mp4]/worst",
        "outtmpl": out_path,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 1,
        "fragment_retries": 1,
        "noplaylist": True,
    }
    if proxy:
        ydl_opts["proxy"] = proxy

    with YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(video_url, download=True)

    if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
        record_media_cache_entry(out_path, kind="video", content_id=safe_key, source_url=video_url)
        log.info(f"[download] Video saved: {out_path} ({os.path.getsize(out_path)//1024}KB)")
        return out_path

    candidates = [
        os.path.join(output_dir, name)
        for name in os.listdir(output_dir)
        if name.startswith(f"{safe_key}_video.") and os.path.getsize(os.path.join(output_dir, name)) > 1024
    ]
    if candidates:
        candidates.sort(key=os.path.getmtime, reverse=True)
        record_media_cache_entry(candidates[0], kind="video", content_id=safe_key, source_url=video_url)
        log.info(f"[download] Video saved: {candidates[0]} ({os.path.getsize(candidates[0])//1024}KB)")
        return candidates[0]

    raise RuntimeError(f"Failed to download video: {video_url}")


# ─── 阶段1: 长文本摘要 ──────────────────────────────────────────────

def _should_summarize(transcript: str) -> bool:
    return len(transcript) > SUMMARIZE_THRESHOLD_CHARS


def _summarize(transcript: str, title: str) -> Tuple[str, str]:
    system_prompt = """你是一个专业视频内容摘要助手。请对以下视频的语音转写文本进行精炼摘要。

要求：
1. 保留核心技术要点、关键数据、操作步骤
2. 压缩冗余的口语表达
3. 维持逻辑连贯性
4. 输出字数控制在原文的 30%-40%
5. 只输出摘要文本本身，不要附加任何说明"""

    user_prompt = f"""视频标题：{title}

转写文本（{len(transcript)}字符）：
{transcript[:8000]}

请输出精炼摘要："""

    return _call_llm(system_prompt, user_prompt, temperature=0.2)


# ─── 阶段2: 信息密度评分 ────────────────────────────────────────────

def _score_density(
    title: str, description: str, summary: str, comments: List[Dict], is_summarized: bool,
) -> Tuple[int, str, str]:
    comments_text = ""
    for c in comments:
        if c.get("verdict") == "keep":
            comments_text += f"- {c['content'][:120]}\n"
    if not comments_text:
        comments_text = "（无有效评论）"

    system_prompt = """你是一个"视频信息密度评估专家"。请根据以下维度对视频进行1-10分评分：

## 评分维度（各占2分，总分10分）

### 1. 技术/知识深度 (0-2分)
- 2分：提供系统的技术原理、架构分析、方法论
- 1分：有部分技术讲解或实践知识
- 0分：纯娱乐/情绪内容

### 2. 信息原创性与创新度 (0-2分)
- 2分：提出新颖观点、独特方法、原创方案
- 1分：整合已有信息但有其独特角度或对比分析
- 0分：搬运已知信息，无增量价值

### 3. 实用价值 (0-2分)
- 2分：可直接指导实践的操作教程、代码方案、工具链
- 1分：部分实用建议或参考
- 0分：无实操指导价值

### 4. 论据质量与可信度 (0-2分)
- 2分：有数据支撑、案例验证、完整论证链条
- 1分：有一定论证但不够严谨
- 0分：纯主观感想

### 5. 社区讨论价值 (0-2分)
- 2分：评论区有深度讨论、用户补充关键信息、修正/质疑视频内容
- 1分：有少量有价值的讨论
- 0分：评论区无实质内容

## 输出格式
必须输出严格的JSON（不要任何其他文字）：
{
  "score": <整数1-10>,
  "rationale": "<中文评分理由，包含各维度得一分的说明，总共30-80字>"
}"""

    user_prompt = f"""## 视频标题
{title}

## 视频简介
{description or "（无）"}

## 语音内容摘要
{"【已摘要】" if is_summarized else "【原始转写】"}{summary[:5000]}

## 社区高价值评论（已过滤）
{comments_text[:2000]}

请严格按JSON格式输出评分。"""

    content, model_label = _call_llm(system_prompt, user_prompt, temperature=0.2)
    result = _extract_json(content)
    score = max(1, min(10, int(result.get("score", 5))))
    rationale = result.get("rationale", "无理由")
    return score, rationale, model_label


VISUAL_TUTORIAL_TERMS = {
    "教程": 1.0,
    "入门": 0.8,
    "上手": 0.8,
    "演示": 1.0,
    "实操": 1.0,
    "操作": 1.0,
    "界面": 1.0,
    "安装": 0.8,
    "配置": 0.8,
    "开发": 0.8,
    "代码": 0.8,
    "调试": 0.7,
    "软件": 0.7,
    "建模": 1.0,
    "生成": 0.7,
    "评测": 0.9,
    "实测": 0.9,
    "对比": 0.7,
    "开箱": 0.9,
}


LANGUAGE_FIRST_TERMS = {
    "访谈": 1.2,
    "播客": 1.2,
    "讲座": 0.8,
    "演讲": 0.8,
    "观点": 0.5,
    "解读": 0.4,
}


def _match_weighted_terms(text: str, terms: Dict[str, float]) -> Tuple[float, List[str]]:
    text_l = text.lower()
    score = 0.0
    matched: List[str] = []
    for term, weight in terms.items():
        if term.lower() in text_l:
            score += weight
            matched.append(term)
    return score, matched


def _visual_dependency_adjustment(video: VideoInfo, base_score: int) -> Tuple[int, str]:
    """Boost visual tutorials/reviews that text-density scoring underrates."""
    text = f"{video.title}\n{video.description}"
    visual_score, visual_terms = _match_weighted_terms(text, VISUAL_TUTORIAL_TERMS)
    language_score, language_terms = _match_weighted_terms(text, LANGUAGE_FIRST_TERMS)
    if re.search(r"(visual studio|vscode|ide|ui|gui|ppt|screen|demo)", text, re.I):
        visual_score += 1.0
        visual_terms.append("screen_tool_marker")

    adjusted = base_score
    reason = ""
    transcript_pending = video.transcript.strip().startswith("[transcribe]") or video.transcript_chars < 200

    if visual_score >= 3.0 and language_score < 1.0:
        floor = 7 if transcript_pending else 6
        if adjusted < floor:
            adjusted = floor
            reason = (
                "视觉依赖校准：标题/简介命中"
                f"{', '.join(visual_terms[:6])}，"
                "该类教程/评测的核心信息依赖画面，不能仅按转写密度降级"
            )
    if language_terms and adjusted > base_score:
        reason += f"；语言优先信号较弱/未覆盖主类型: {', '.join(language_terms[:3])}"

    return adjusted, reason


# ─── 阶段3: 决策分流 ──────────────────────────────────────────────

def _decide(score: int) -> str:
    return "FULL_ANALYSIS" if score >= 7 else "FAST_SUMMARY"


# ═══════════════════════════════════════════════════════════════
# 精品通道: 千问多模态深度视频理解
# ═══════════════════════════════════════════════════════════════

def deep_analyze_video(
    video_info: VideoInfo,
    summary: str,
) -> Dict:
    """
    精品通道深度分析：用千问多模态模型理解视频画面+音频。

    流程:
    1. 下载视频画面流（如未缓存）
    2. 提取关键帧（base64）
    3. 调用 SiliconFlow 千问多模态分析
       - qwen3.5-122b-a10b 失败 → 自动切换 qwen3-vl-8b-instruct
       - 全部失败 → 抛异常

    Returns:
        {
            "core_summary": "视频核心内容摘要（200字内）",
            "key_steps": ["步骤1", "步骤2", ...],
            "visual_information": "画面中的图表、参数、代码等文本层获取不到的信息",
            "author_viewpoint": "作者观点/结论",
        }
    """
    t_start = time.time()

    # ── 步骤1: 获取视频文件 ──
    import re
    bvid_match = re.search(r"BV[a-zA-Z0-9]+", video_info.title + " " + video_info.video_url)
    bvid = bvid_match.group(0) if bvid_match else None
    video_path = video_info.video_path

    if not video_path and bvid:
        log.info("[deep] Downloading video stream from Bilibili...")
        data_dir = os.path.dirname(video_info.video_path) if video_info.video_path else \
                   str(media_cache_subdir("video", content_id=bvid))
        video_path = _download_bilibili_video(bvid, data_dir)
    elif not video_path and video_info.video_url:
        log.info("[deep] Downloading video stream from generic provider...")
        video_key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", video_info.video_url).strip("_")[-64:]
        data_dir = os.path.dirname(video_info.video_path) if video_info.video_path else \
                   str(media_cache_subdir("video", content_id=video_key))
        video_path = _download_generic_video(video_info.video_url, data_dir, video_key)
    elif not video_path:
        raise ValueError("No video path or BVID available for deep analysis")

    log.info(f"[deep] Video path: {video_path}")

    # ── 步骤2: 提取关键帧 ──
    log.info("[deep] Extracting key frames...")
    frames = _extract_frames(video_path, max_frames=MAX_FRAMES)
    log.info(f"[deep] {len(frames)} frames extracted")

    # ── 步骤3: 调用多模态模型 ──
    system_prompt = """你是一个视频深度分析专家。请根据提供的视频截图画面和语音转写内容，输出结构化分析。

请严格按以下JSON格式输出（只输出JSON，不要其他文字）：

{
  "core_summary": "视频核心内容摘要，200字以内，覆盖视频主题、主要内容和价值点",
  "key_steps": [
    "步骤1：具体操作描述",
    "步骤2：具体操作描述"
  ],
  "visual_information": "画面中提取到的关键信息：图表数据、配置参数、代码片段、UI界面描述等。注意只描述纯文本层获取不到的画面信息。如无特别画面信息则写"无显著画面信息"",
  "author_viewpoint": "作者的核心观点、结论或推荐"
}

注意：
- key_steps 仅在视频有教程性质时填写，否则为 []
- visual_information 是从画面中看到的、文本中不包含的信息"""

    user_text = f"""## 视频标题
{video_info.title}

## 视频简介
{video_info.description or "（无）"}

## 语音转写全文
{video_info.transcript[:5000]}

## 视频截图（共{len(frames)}张，每{FRAME_INTERVAL_SEC}秒一张）
请结合截图画面和语音转写内容，分析这个视频。

请严格按JSON格式输出。"""

    content, model_label = _call_multimodal(system_prompt, user_text, frames, temperature=0.2)

    # ── 步骤4: 解析JSON ──
    result = _extract_json(content)

    elapsed = time.time() - t_start
    log.info(f"[deep] Analysis complete in {elapsed:.1f}s using {model_label}")

    return {
        "model_used": model_label,
        "analysis_time_s": round(elapsed, 1),
        "frame_count": len(frames),
        "core_summary": result.get("core_summary", ""),
        "key_steps": result.get("key_steps", []),
        "visual_information": result.get("visual_information", ""),
        "author_viewpoint": result.get("author_viewpoint", ""),
    }


# ─── 主流程 ─────────────────────────────────────────────────────────

class VideoProcessor:
    """通用视频内容处理器"""

    def process(
        self,
        video: VideoInfo,
        skip_deep: bool = False,
    ) -> ProcessingResult:
        """
        完整三层处理流水线。

        Args:
            video: 视频信息
            skip_deep: 跳过精品通道（测试用）

        Returns:
            ProcessingResult 包含评分、决策、深度分析结果
        """
        self.stages = []
        log.info("=" * 55)
        log.info(f"  {video.title[:45]}")
        log.info(f"  transcript: {video.transcript_chars} chars")
        log.info(f"  comments: {video.comment_count} filtered")
        log.info("=" * 55)

        # ── 层1: 摘要 ──
        summary = video.transcript
        was_summarized = False
        orig_chars = video.transcript_chars

        if _should_summarize(video.transcript):
            self._log("层1-摘要", f"transcript {orig_chars} > {SUMMARIZE_THRESHOLD_CHARS}，开始摘要...")
            summary, model = _summarize(video.transcript, video.title)
            was_summarized = True
            self._log("层1-摘要完成", f"摘要后 {len(summary)} chars，使用 {model}")
        else:
            self._log("层1-摘要", f"跳过摘要（{orig_chars} ≤ {SUMMARIZE_THRESHOLD_CHARS}）")

        # ── 层2: 评分 ──
        self._log("层2-评分", "调用 LLM...")
        score, rationale, score_model = _score_density(
            title=video.title,
            description=video.description,
            summary=summary,
            comments=video.filtered_comments,
            is_summarized=was_summarized,
        )
        adjusted_score, adjustment_reason = _visual_dependency_adjustment(video, score)
        if adjusted_score != score:
            self._log("层2-视觉校准", f"{score}/10 → {adjusted_score}/10 | {adjustment_reason}")
            rationale = f"{rationale}；{adjustment_reason}"
            score = adjusted_score
        self._log("层2-评分完成", f"{score}/10 | 模型: {score_model} | {rationale[:50]}...")

        # ── 层2: 决策 ──
        decision = _decide(score)
        self._log("层2-决策", f"评分 {score} → {decision}")

        result = ProcessingResult(
            score=score,
            decision=decision,
            summary=summary,
            scoring_rationale=rationale,
            summary_was_needed=was_summarized,
            original_transcript_chars=orig_chars,
            used_model=score_model,
            stages=self.stages,
        )

        # ── 层3: 精品通道（仅在 FULL_ANALYSIS 时触发）──
        if decision == "FULL_ANALYSIS" and not skip_deep:
            self._log("层3-精品通道", "评分≥7，启动千问多模态深度分析...")
            try:
                deep_result = deep_analyze_video(video, summary)
                result.deep_analysis = deep_result
                result.deep_analysis_time_s = deep_result.get("analysis_time_s", 0)
                self._log("层3-完成",
                    f"耗时 {deep_result['analysis_time_s']:.1f}s | "
                    f"模型: {deep_result.get('model_used','?')} | "
                    f"帧数: {deep_result.get('frame_count',0)} | "
                    f"摘要: {deep_result.get('core_summary','')[:40]}...")
            except Exception as e:
                log.error(f"[deep_analyze] Failed: {e}")
                raise  # 不静默丢失
        else:
            branch = "跳过（FAST_SUMMARY 分支）" if decision != "FULL_ANALYSIS" else "跳过（skip_deep=True）"
            self._log("层3-精品通道", branch)

        log.info("=" * 55)
        log.info(f"  → {decision} (Score={score}/10)")
        if result.deep_analysis:
            log.info(f"  → 精品通道: ✅ 完成 ({result.deep_analysis_time_s:.1f}s)")
        log.info("=" * 55)

        return result

    def _log(self, name: str, detail: str):
        entry = {"stage": name, "detail": detail}
        self.stages.append(entry)
        log.info(f"  [{name}] {detail}")


# ─── 测试入口 ───────────────────────────────────────────────────────

def demo_deepseek_video():
    """用 DeepSeek 教程视频完整测试三层流水线"""

    demo_data_dir = os.environ.get("KR_DEMO_DATA_DIR")
    transcript_path = os.environ.get("KR_DEMO_TRANSCRIPT_PATH") or (
        os.path.join(demo_data_dir, "BV1cUoDYaEdb_transcript.txt")
        if demo_data_dir
        else str(resolve_runtime_media_file("BV1cUoDYaEdb_transcript.txt"))
    )
    filter_path = os.environ.get("KR_DEMO_COMMENT_FILTER_PATH") or (
        os.path.join(demo_data_dir, "comment_filter_result.json")
        if demo_data_dir
        else str(resolve_runtime_media_file("comment_filter_result.json"))
    )

    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript = f.read()
    with open(filter_path, "r", encoding="utf-8") as f:
        filter_result = json.load(f)

    video = VideoInfo(
        title="语音、画图、PPT、联动全网，我这个完美版的DeepSeek，你们都没有用过！",
        description='视频里搭的智能体已经上线了，扣子搜"GitHub查询器"、"暴打弱智吧"就可以体验',
        transcript=transcript,
        filtered_comments=filter_result.get("kept_comments", []),
        duration_seconds=394,
        video_url="https://www.bilibili.com/video/BV1cUoDYaEdb",
    )

    processor = VideoProcessor()
    result = processor.process(video, skip_deep=False)

    # ── 打印报告 ──
    print("\n" + "=" * 60)
    print("  DeepSeek 教程视频 - 完整处理报告")
    print("=" * 60)

    print(f"\n【层1-摘要】{'跳过（文本未超阈值）' if not result.summary_was_needed else f'已完成 ({len(result.summary)} chars)'}")

    print(f"\n【层2-粗筛】Score: {result.score}/10 → {result.decision}")
    print(f"  模型: {result.used_model}")
    print(f"  理由: {result.scoring_rationale}")

    print("\n【层3-精品通道】", end="")
    if result.deep_analysis:
        da = result.deep_analysis
        print(f" ✅ 完成 ({da['analysis_time_s']:.1f}s, {da.get('frame_count',0)}帧, {da.get('model_used','?')})")
        print(f"\n  核心摘要: {da.get('core_summary', '')}")
        print("\n  关键步骤:")
        for i, step in enumerate(da.get('key_steps', []), 1):
            print(f"    {i}. {step}")
        print(f"\n  画面信息: {da.get('visual_information', '')}")
        print(f"\n  作者观点: {da.get('author_viewpoint', '')}")
    else:
        print(f"  {'FAST_SUMMARY 分支，未触发' if result.decision != 'FULL_ANALYSIS' else '跳过'}")

    print(f"\n{'='*60}")
    print(f"  精品通道 {'✅ 跑通' if result.deep_analysis else '❌ 未执行'}")
    print(f"  总耗时（仅层3）: {result.deep_analysis_time_s:.1f}s" if result.deep_analysis else "")
    print(f"{'='*60}")

    return result


if __name__ == "__main__":
    demo_deepseek_video()
