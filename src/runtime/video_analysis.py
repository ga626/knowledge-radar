"""下载和站点均在外部完成；分析只消费已有文本/帧。"""

def filter_video_comments(data, *, query='', limit=12):
    from runtime.comments_view import comment_view
    return comment_view(data.get('comments') or [],query=query,limit=limit)['items']


def analyze_video_evidence(data, *, frames):
    from video_processor import VideoInfo, VideoProcessor
    from runtime.execution_context import check_execution, execution_value
    from runtime.operation_checkpoint import OperationCheckpoint
    from runtime.media_cache import media_cache_subdir
    from runtime.optional_media import file_sha
    from runtime.model_roles import CLOUD_MODEL
    from pathlib import Path
    check_execution()
    video = VideoInfo(title=data.get('title', ''), description=data.get('desc', ''),
        transcript=data.get('transcript', ''), filtered_comments=data.get('filtered_comments', data.get('comments', [])),
        duration_seconds=data.get('duration', 0), frame_samples=frames)
    identity = {'input': vars(video), 'model': CLOUD_MODEL,
                'implementation': file_sha(Path(__file__).parents[1] / 'video_processor.py'),
                'scope': execution_value('KR_PAID_TASK_SCOPE', '')}
    checkpoint = OperationCheckpoint(media_cache_subdir('video_analysis'), identity, identity)
    saved = checkpoint.read()
    if saved.get('result', {}).get('status') == 'completed':
        return saved['result']
    result = VideoProcessor().process(video, force_deep=True)
    check_execution()
    result = {'status': 'completed', **result.to_dict()}
    checkpoint.write({'result': result})
    return result
