"""仅在获批的隔离Python内运行；stdout仅结果，模型日志转stderr。"""
import contextlib
import importlib.metadata
import json
from pathlib import Path
import sys


def native_asr(request):
    """原生 Transformers 单模型、逐块有界转写；不启用旧 qwen-asr/vLLM 路径。"""
    import tempfile
    import soundfile as sf
    import torch
    from transformers import AutoProcessor, AutoModelForMultimodalLM
    device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
    dtype = (torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16) if device != 'cpu' else torch.float32
    processor = AutoProcessor.from_pretrained(request['model_root'], local_files_only=True)
    model = AutoModelForMultimodalLM.from_pretrained(request['model_root'], local_files_only=True, dtype=dtype, attn_implementation='eager').to(device).eval()
    # FFmpeg 统一容器/采样率，避免 m4a 在 libsndfile 中不受支持。
    import subprocess
    import os
    with tempfile.TemporaryDirectory(prefix='kr-asr-source-') as source_directory:
        converted = str(Path(source_directory) / 'source.wav')
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        conversion = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', request['input_path'], '-t', '14401', '-ar', '16000', '-ac', '1', converted], capture_output=True, timeout=180, creationflags=flags)
        if conversion.returncode: raise ValueError('audio_decode_failed')
        return transcribe_chunks(request, model, processor, sf, torch, converted)


def transcribe_chunks(request, model, processor, sf, torch, source):
    import tempfile
    parts, blocks = [], []
    with sf.SoundFile(source) as audio, tempfile.TemporaryDirectory(prefix='kr-asr-') as temporary:
        if audio.frames / audio.samplerate > 4 * 3600: raise ValueError('audio_duration_exceeds_budget')
        cursor = 0
        while True:
            samples = audio.read(30 * audio.samplerate, dtype='float32', always_2d=True)
            if not len(samples): break
            start, end = cursor / audio.samplerate, (cursor + len(samples)) / audio.samplerate
            chunk = str(Path(temporary) / 'chunk.wav')
            sf.write(chunk, samples.mean(axis=1), audio.samplerate)
            inputs = processor.apply_transcription_request(audio=chunk, language=request.get('language') or None).to(model.device, model.dtype)
            with torch.inference_mode():
                output = model.generate(**inputs, max_new_tokens=1024, do_sample=False)
            generated = output[:, inputs['input_ids'].shape[1]:]
            if generated.shape[1] >= 1024: raise ValueError('asr_token_budget_exhausted_no_complete_transcript')
            text = processor.decode(generated, return_format='transcription_only')[0].strip()
            if text:
                parts.append(text)
                blocks.append({'text':text, 'start_s':start, 'end_s':end, 'kind':'audio_chunk'})
            cursor += len(samples)
    text = '\n'.join(parts)
    # 时间范围是实际输入切片边界，不能冒充词级强制对齐。
    locations, cursor = [], 0
    for block in blocks:
        locations.append({'start':cursor, 'end':cursor+len(block['text']), 'unit':'unicode_code_point', 'start_s':block['start_s'], 'end_s':block['end_s'], 'kind':'audio_chunk'})
        cursor += len(block['text']) + 1
    return {'status':'acquired', 'text':text, 'source_kind':'asr', 'component':request['component'], 'content_locators':locations, 'timestamp_precision':'input_chunk_only', 'word_timestamps':'not_acquired', 'device':str(model.device), 'dtype':str(model.dtype)}


def docling_blocks(document):
    blocks = []
    for item, _level in document.iterate_items():
        kind = str(getattr(item, 'label', 'text')).split('.')[-1].lower()
        text = getattr(item, 'text', '')
        if kind == 'table': text = item.export_to_markdown(doc=document)
        if not text and kind == 'picture': text = '[图片：未提取图内文字]'
        if not text: continue
        provenance = list(getattr(item, 'prov', []) or [])
        block = {'text':str(text), 'kind':kind}
        if provenance:
            prov = provenance[0]
            block['page'] = int(prov.page_no)
            box = prov.bbox
            block['bbox'] = [float(box.l), float(box.t), float(box.r), float(box.b)]
            block['coordinate_system'] = str(getattr(box, 'coord_origin', 'parser_native'))
        blocks.append(block)
    return blocks


def execute(request):
    for package,version in request['expected_packages'].items():
        if importlib.metadata.version(package)!=version:raise ValueError('package_version_mismatch')
    component=request['component'];source=request['input_path'];model=request['model_root']
    if component in {'qwen3-asr-1.7b','qwen3-asr-0.6b'}:
        return native_asr(request)
    if component=='docling':
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        options=PdfPipelineOptions(artifacts_path=Path(model), enable_remote_services=False, allow_external_plugins=False)
        result=DocumentConverter(format_options={InputFormat.PDF:PdfFormatOption(pipeline_options=options)}).convert(source)
        return {'status':'acquired','text':result.document.export_to_markdown(),'document_blocks':docling_blocks(result.document),'source_kind':'document_parse','component':component}
    if component=='paddleocr-vl-1.6':
        from paddleocr import PaddleOCRVL
        if not request.get('layout_model_root'): raise ValueError('paddle_layout_model_required')
        pipeline=PaddleOCRVL(vl_rec_model_dir=model, layout_detection_model_dir=request['layout_model_root'])
        rows=list(pipeline.predict(input=source))
        markdown=[];blocks=[]
        for row in rows:
            value=row.markdown
            if isinstance(value,dict):markdown.append(str(value.get('markdown_texts') or ''))
            data = row.json
            if isinstance(data, str): data = json.loads(data)
            data = data.get('res', data)
            for block in data.get('parsing_res_list', []):
                item = {'text':str(block.get('block_content') or ''), 'kind':str(block.get('block_label') or 'ocr'), 'page':int(data.get('page_index') or 0)+1}
                if block.get('block_bbox') is not None: item.update(bbox=block['block_bbox'], coordinate_system='image_pixels')
                blocks.append(item)
        return {'status':'acquired','text':'\n'.join(markdown),'document_blocks':blocks,'source_kind':'ocr','component':component}
    raise ValueError('unsupported_component')


if __name__=='__main__':
    raw=sys.stdin.read(1024*1024+1)
    if len(raw)>1024*1024:raise ValueError('request_too_large')
    with contextlib.redirect_stdout(sys.stderr):result=execute(json.loads(raw))
    print(json.dumps(result,ensure_ascii=False))
