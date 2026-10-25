"""只为与原文逐字对齐的字幕提供code point/时间定位。"""
import math


def document_text(blocks):
    """由真实解析块组装唯一正文；定位引用其原生页码/几何信息，不猜测坐标。"""
    if not isinstance(blocks, list) or len(blocks) > 50000:
        raise ValueError('invalid_document_blocks')
    parts, locators, cursor = [], [], 0
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError('invalid_document_block')
        text = block.get('text')
        if not isinstance(text, str) or not text:
            continue
        if parts:
            parts.append('\n')
            cursor += 1
        locator = {key: block[key] for key in ('page', 'bbox', 'coordinate_system', 'kind', 'table_id', 'row', 'column', 'image_index', 'comment_id', 'parent_comment_id') if key in block}
        locator.update(start=cursor, end=cursor + len(text), unit='unicode_code_point')
        locators.append(locator)
        parts.append(text)
        cursor += len(text)
    body = ''.join(parts)
    return body, validate_locators(body, locators)


def validate_locators(text, locators):
    if not isinstance(locators, list) or len(locators) > 50000:
        raise ValueError('invalid_evidence_locators')
    result = []
    for item in locators:
        if not isinstance(item, dict):
            raise ValueError('invalid_evidence_locator')
        start, end = item.get('start'), item.get('end')
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(text):
            raise ValueError('invalid_evidence_locator_range')
        clean = {'start': start, 'end': end, 'unit': 'unicode_code_point'}
        for key in ('page', 'row', 'column', 'image_index'):
            if key in item:
                value = item[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < (1 if key == 'page' else 0):
                    raise ValueError('invalid_evidence_locator_number')
                clean[key] = value
        if 'bbox' in item:
            bbox = item['bbox']
            if not isinstance(bbox, (tuple, list)) or len(bbox) != 4 or not all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) for n in bbox):
                raise ValueError('invalid_evidence_bbox')
            clean['bbox'] = list(bbox)
        for key in ('start_s', 'end_s'):
            if key in item:
                value = item[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError('invalid_evidence_time')
                clean[key] = value
        if clean.get('end_s', float('inf')) < clean.get('start_s', 0):
            raise ValueError('invalid_evidence_time')
        for key in ('coordinate_system', 'kind', 'table_id', 'comment_id', 'parent_comment_id'):
            if key in item:
                clean[key] = str(item[key])[:200]
        result.append(clean)
    return result


def paragraph_locators(text):
    import re
    return [{'start': item.start(), 'end': item.end(), 'unit': 'unicode_code_point', 'kind': 'paragraph'} for item in re.finditer(r'[^\r\n]+', text)][:50000]


def transcript_locators(text, segments):
    if not isinstance(text,str) or not isinstance(segments,list):
        return {'status':'missing','segments':[],'reason':'original_timed_segments_unavailable'}
    result,cursor=[],0
    previous=-1.0
    for segment in segments:
        try:
            value=str(segment['text']).strip()
            start=float(segment.get('start_s',segment.get('start')))
            end=float(segment.get('end_s',segment.get('end')))
        except (ValueError,TypeError,KeyError):
            return {'status':'unaligned','segments':[],'reason':'invalid_timed_segment'}
        if not value or not math.isfinite(start) or not math.isfinite(end) or not 0<=start<=end or start<previous or text[cursor:cursor+len(value)]!=value:
            return {'status':'unaligned','segments':[],'reason':'text_or_time_does_not_align'}
        result.append({'start':cursor,'end':cursor+len(value),'unit':'unicode_code_point','start_s':start,'end_s':end})
        previous=start
        cursor+=len(value)
        if cursor<len(text) and text[cursor]=='\n':cursor+=1
    if cursor!=len(text) or not result:
        return {'status':'unaligned','segments':[],'reason':'timed_segments_do_not_cover_text'}
    return {'status':'aligned','segments':result,'coverage':'segment_mapping_only','media_completeness':'unknown'}
