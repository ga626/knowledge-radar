"""B站视频转写：下载音频 + 同进程 Qwen3-ASR 0.6B"""

import logging
import os
import time
from pathlib import Path

from runtime.media_cache import media_cache_subdir, record_media_cache_entry
from runtime.asr_policy import AsrPolicy

log = logging.getLogger("mcp-server")

OUTPUT_DIR = os.environ.get("KR_TRANSCRIBE_OUTPUT_DIR") or str(media_cache_subdir("transcripts"))
os.makedirs(OUTPUT_DIR, exist_ok=True)



def download_audio(bvid: str) -> str:
    """旧CLI消费者也只走公共获取器；不保留第二套下载。"""
    from media_bundle import MediaRef, MediaKind
    from runtime.media_acquisition import acquire_media_ref
    return acquire_media_ref(MediaRef(MediaKind.AUDIO, f'https://www.bilibili.com/video/{bvid}'))['path']




def _chunk_srt(segments):
    def stamp(seconds):
        milliseconds = max(0, round(seconds * 1000))
        hours, milliseconds = divmod(milliseconds, 3600000)
        minutes, milliseconds = divmod(milliseconds, 60000)
        seconds, milliseconds = divmod(milliseconds, 1000)
        return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"
    return "\n\n".join(f"{i}\n{stamp(row['start_s'])} --> {stamp(row['end_s'])}\n{row['text']}"
                        for i, row in enumerate(segments, 1))


def transcribe(audio_path: str, model_size: str = "qwen3-asr-0.6b", policy: AsrPolicy | None = None) -> dict:
    """通过唯一公共 ASR 执行器 转写；缺少安装或执行失败明确返回缺口。"""
    from runtime.optional_media import run_optional, file_sha
    from runtime.model_roles import ASR_COMPONENT

    policy = policy or AsrPolicy.from_env()
    started = time.time()
    source = Path(audio_path).resolve()
    native = run_optional(
        {
            "component": ASR_COMPONENT,
            "input_path": str(source),
            "approved_input_root": str(source.parent),
            "input_sha256": file_sha(source),
            "language": policy.language or "auto",
        }
    )
    locations = native.get("content_locators") or []
    segments = [
        {"start_s": loc["start_s"], "end_s": loc["end_s"], "text": native["text"][loc["start"] : loc["end"]]}
        for loc in locations
    ]
    return {
        "segments": len(segments),
        "timestamped_segments": segments,
        "duration_s": time.time() - started,
        "model_load_s": (native.get("asr_metrics") or {}).get("model_load_s"),
        "model_cache_hit": (native.get("asr_metrics") or {}).get("model_cache_hit", False),
        "device": native.get("device"),
        "compute_type": native.get("dtype"),
        "beam_size": 1,
        "vad_enabled": False,
        "model": ASR_COMPONENT,
        "engine": "sherpa_onnx_int8",
        "txt": native["text"],
        "srt": _chunk_srt(segments),
        "language": native.get('detected_language', ''),
        "language_hint": policy.language,
        "detected_languages": native.get('detected_languages', []),
        "confidence": None,
        "content_locators": locations,
        "timestamp_precision": "input_chunk_only",
        "evidence_assets": native.get("evidence_assets", []),
        "asr_metrics": native.get("asr_metrics", {}),
        "execution_timings": native.get("execution_timings", {}),
    }


def process_video(bvid: str, model_size: str = "qwen3-asr-0.6b", policy: AsrPolicy | None = None) -> dict:
    """Full pipeline: download audio + transcribe for a B站 video."""
    policy = policy or AsrPolicy.from_env()
    log.info(f"{'=' * 60}")
    log.info(f"Processing B站 video: {bvid}")
    log.info(f"{'=' * 60}")

    # Step 1: Download audio
    t0 = time.time()
    audio_path = download_audio(bvid)
    dl_time = time.time() - t0
    log.info(f"[download] Time: {dl_time:.1f}s")

    # Step 2: Transcribe
    result = transcribe(audio_path, model_size, policy=policy)

    # Save outputs
    base = os.path.join(OUTPUT_DIR, bvid)
    with open(f"{base}_transcript.txt", "w", encoding="utf-8") as f:
        f.write(result["txt"])
    with open(f"{base}_transcript.srt", "w", encoding="utf-8") as f:
        f.write(result["srt"])
    record_media_cache_entry(
        f"{base}_transcript.txt",
        kind="transcript",
        content_id=bvid,
        source_url=f"https://www.bilibili.com/video/{bvid}",
        metadata={"format": "txt", "segments": result["segments"]},
    )
    record_media_cache_entry(
        f"{base}_transcript.srt",
        kind="transcript",
        content_id=bvid,
        source_url=f"https://www.bilibili.com/video/{bvid}",
        metadata={"format": "srt", "segments": result["segments"]},
    )

    result["bvid"] = bvid
    result["audio_path"] = audio_path
    result["download_s"] = dl_time
    result["transcribe_s"] = result.get("duration_s", 0)
    result["total_time_s"] = time.time() - t0
    result["txt_path"] = f"{base}_transcript.txt"
    result["srt_path"] = f"{base}_transcript.srt"

    log.info(f"[result] Segments: {result['segments']}")
    log.info(f"[result] Transcribe time: {result['duration_s']:.1f}s")
    log.info(f"[result] Total time: {result['total_time_s']:.1f}s")
    log.info(f"[result] TXT: {result['txt_path']}")
    log.info(f"[result] SRT: {result['srt_path']}")
    log.info("[result] Text preview (first 200 chars):")
    log.info(f"  {result['txt'][:200]}")

    return result


if __name__ == "__main__":
    # Test with 2 B站 videos that have clear speech
    test_bvids = [
        "BV1cUoDYaEdb",  # DeepSeek教程 - clear mandarin speech
    ]

    for bvid in test_bvids:
        try:
            process_video(bvid)
        except Exception as e:
            log.error(f"[ERROR] {bvid}: {e}")
