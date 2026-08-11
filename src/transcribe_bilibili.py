"""B站视频转写：下载音频 + faster-whisper 语音转文字"""
import logging
import glob
import os, sys, time, json
from contextlib import contextmanager

from runtime.media_cache import media_cache_subdir, record_media_cache_entry
from runtime.asr_policy import AsrPolicy

log = logging.getLogger("mcp-server")

OUTPUT_DIR = os.environ.get("KR_TRANSCRIBE_OUTPUT_DIR") or str(media_cache_subdir("transcripts"))
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Ensure ffmpeg is in PATH for faster-whisper
ffmpeg_bin = os.environ.get("KR_FFMPEG_BIN", "")
if ffmpeg_bin:
    os.environ["PATH"] = ffmpeg_bin + os.pathsep + os.environ.get("PATH", "")

_MODEL_CACHE = {}


@contextmanager
def _redirect_stdout_to_stderr():
    """Redirect sys.stdout to sys.stderr to prevent MCP stdio pollution.
    
    yt-dlp and faster-whisper may write directly to stdout via C extensions
    or low-level sys.stdout.write(), bypassing any builtins.print monkeypatch.
    This context manager physically redirects the file descriptor.
    """
    if os.environ.get("KR_MCP_TRANSPORT", "").strip().lower() != "stdio":
        yield
        return
    
    old_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = old_stdout


def download_audio(bvid: str) -> str:
    """Download audio from a B站 video using yt-dlp. Returns audio file path."""
    from yt_dlp import YoutubeDL

    audio_path = os.path.join(OUTPUT_DIR, f"{bvid}.m4a")
    if os.path.exists(audio_path):
        log.info(f"[download] Audio already exists: {audio_path}")
        record_media_cache_entry(audio_path, kind="audio", content_id=bvid, source_url=f"https://www.bilibili.com/video/{bvid}")
        return audio_path

    existing_audio = _find_downloaded_audio(bvid)
    if existing_audio:
        log.info(f"[download] Audio already exists: {existing_audio}")
        record_media_cache_entry(existing_audio, kind="audio", content_id=bvid, source_url=f"https://www.bilibili.com/video/{bvid}")
        return existing_audio

    url = f"https://www.bilibili.com/video/{bvid}"
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(OUTPUT_DIR, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "verbose": False,
    }
    with _redirect_stdout_to_stderr():
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    log.info(f"[download] Audio downloaded: {info.get('title', '?')} ({bvid})")

    downloaded_audio = _find_downloaded_audio(bvid)
    if downloaded_audio:
        record_media_cache_entry(downloaded_audio, kind="audio", content_id=bvid, source_url=url)
        return downloaded_audio
    raise FileNotFoundError(f"Audio download finished but no m4a file found for {bvid}")


def _find_downloaded_audio(bvid: str) -> str:
    """Return the best existing audio file for a B站 BV id.

    yt-dlp writes multi-part B站 videos as ``<bvid>_p1.m4a`` even when we only
    download the first page. The previous code always returned ``<bvid>.m4a``,
    which made background transcription fail after a successful download.
    """
    candidates = [os.path.join(OUTPUT_DIR, f"{bvid}.m4a")]
    candidates.extend(sorted(glob.glob(os.path.join(OUTPUT_DIR, f"{bvid}_p*.m4a"))))
    existing = [path for path in candidates if os.path.exists(path) and os.path.getsize(path) > 0]
    if not existing:
        return ""
    preferred_p1 = os.path.join(OUTPUT_DIR, f"{bvid}_p1.m4a")
    if preferred_p1 in existing:
        return preferred_p1
    return existing[0]


def _model_cache_key(model_size: str, policy: AsrPolicy) -> tuple[str, str, str]:
    return (model_size, policy.device, policy.compute_type)


def _load_model(model_size: str, policy: AsrPolicy):
    from faster_whisper import WhisperModel

    key = _model_cache_key(model_size, policy)
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached, 0.0, True
    log.info(
        "[transcribe] Loading model '%s' device=%s compute=%s...",
        model_size,
        policy.device,
        policy.compute_type,
    )
    t0 = time.time()
    with _redirect_stdout_to_stderr():
        kwargs = {}
        if policy.model_cache_dir:
            kwargs["download_root"] = policy.model_cache_dir
        model = WhisperModel(model_size, device=policy.device, compute_type=policy.compute_type, **kwargs)
    elapsed = time.time() - t0
    _MODEL_CACHE[key] = model
    log.info(f"[transcribe] Model loaded in {elapsed:.1f}s")
    return model, elapsed, False


def transcribe(audio_path: str, model_size: str = "base", policy: AsrPolicy | None = None) -> dict:
    """Transcribe audio using faster-whisper. Returns transcript data."""
    policy = policy or AsrPolicy.from_env()
    model, model_load_s, model_cache_hit = _load_model(model_size, policy)

    log.info(f"[transcribe] Transcribing {audio_path}...")
    t0 = time.time()
    with _redirect_stdout_to_stderr():
        segments, info = model.transcribe(
            audio_path,
            language=policy.language or None,
            beam_size=policy.beam_size,
            vad_filter=policy.vad_enabled,
        )
    log.info(f"[transcribe] Detected: {info.language} (p={info.language_probability:.2f})")

    txt_lines = []
    srt_lines = []
    for i, seg in enumerate(segments, 1):
        start = seg.start
        end = seg.end
        text = seg.text.strip()
        if not text:
            continue
        txt_lines.append(text)
        # SRT format
        srt_lines.append(f"{i}")
        srt_lines.append(
            f"{int(start//3600):02d}:{int((start%3600)//60):02d}:{int(start%60):02d},{int((start%1)*1000):03d}"
            f" --> "
            f"{int(end//3600):02d}:{int((end%3600)//60):02d}:{int(end%60):02d},{int((end%1)*1000):03d}"
        )
        srt_lines.append(text)
        srt_lines.append("")

    duration = time.time() - t0

    return {
        "segments": len(txt_lines),
        "duration_s": duration,
        "model_load_s": model_load_s,
        "model_cache_hit": model_cache_hit,
        "device": policy.device,
        "compute_type": policy.compute_type,
        "beam_size": policy.beam_size,
        "vad_enabled": policy.vad_enabled,
        "model": model_size,
        "txt": "\n".join(txt_lines),
        "srt": "\n".join(srt_lines),
        "language": info.language,
        "confidence": info.language_probability,
    }


def process_video(bvid: str, model_size: str = "base", policy: AsrPolicy | None = None) -> dict:
    """Full pipeline: download audio + transcribe for a B站 video."""
    policy = policy or AsrPolicy.from_env()
    log.info(f"{'='*60}")
    log.info(f"Processing B站 video: {bvid}")
    log.info(f"{'='*60}")

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
    log.info(f"[result] Text preview (first 200 chars):")
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
