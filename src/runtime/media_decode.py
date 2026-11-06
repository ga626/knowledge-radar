"""同进程解码：有界音频块与带真实PTS的候选帧，不调用FFmpeg进程。"""
import base64
from io import BytesIO
from pathlib import Path
from runtime.execution_context import check_execution


def audio_chunks(source, *, seconds=30, rate=16000):
    import av
    import numpy as np
    target = int(seconds * rate)
    pending = np.empty(0, dtype='float32')
    cursor = 0
    with av.open(str(source)) as container:
        if not container.streams.audio:
            raise ValueError('media_has_no_audio')
        resampler = av.AudioResampler(format='fltp', layout='mono', rate=rate)
        def emit(frame):
            nonlocal pending
            pending = np.concatenate((pending, frame.to_ndarray().reshape(-1)))
        for frame in container.decode(audio=0):
            check_execution()
            for converted in resampler.resample(frame):
                emit(converted)
            while len(pending) >= target:
                if cursor + target > rate * 4 * 3600:
                    raise ValueError('audio_duration_exceeds_budget')
                yield cursor / rate, pending[:target].copy(), rate
                cursor += target
                pending = pending[target:]
        for converted in resampler.resample(None):
            emit(converted)
        if len(pending):
            if cursor + len(pending) > rate * 4 * 3600:
                raise ValueError('audio_duration_exceeds_budget')
            yield cursor / rate, pending, rate


def sample_frames(source, *, max_frames=8):
    import av
    from PIL import ImageStat, ImageChops
    if not Path(source).is_file():
        raise FileNotFoundError(source)
    limit = max(1, min(32, int(max_frames)))
    accepted, previous = [], None
    with av.open(str(source)) as container:
        if not container.streams.video:
            raise ValueError('media_has_no_video')
        stream = container.streams.video[0]
        duration = float(stream.duration * stream.time_base) if stream.duration else float(container.duration or 0) / av.time_base
        # 只解码有界候选点；保留静态课件第一帧，黑底代码不能按平均亮度删除。
        candidates = min(96, limit * 4)
        targets = [duration * i / candidates for i in range(candidates)] if duration else [0]
        for target in targets:
            check_execution()
            container.seek(int(target / stream.time_base), stream=stream, backward=True)
            frame = None
            for index, candidate in enumerate(container.decode(stream)):
                check_execution()
                if candidate.pts is not None and float(candidate.pts * candidate.time_base) >= target:
                    frame = candidate
                    break
                if index >= 240:
                    break
            if frame is None or frame.pts is None:
                continue
            image = frame.to_image().convert('RGB')
            image.thumbnail((640, 640))
            thumb = image.convert('L').resize((96, 64))
            extrema = thumb.getextrema()
            if extrema[1] <= 8:  # 仅近全黑，含亮字/图形的暗画面保留。
                continue
            if previous is not None and ImageStat.Stat(ImageChops.difference(thumb, previous)).mean[0] < 1:
                continue
            previous = thumb
            buffer = BytesIO()
            image.save(buffer, format='JPEG', quality=85)
            accepted.append({'pts_s': float(frame.pts * frame.time_base), 'image_base64': base64.b64encode(buffer.getvalue()).decode()})
        if len(accepted) > limit:
            accepted = [accepted[round(i * (len(accepted) - 1) / max(1, limit - 1))] for i in range(limit)]
    return accepted
