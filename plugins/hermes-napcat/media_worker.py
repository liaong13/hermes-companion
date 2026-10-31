"""No-network parser entry point. Launched only inside MediaSandbox, never in host home."""
from __future__ import annotations

import base64
import csv
import io
import json
import math
import os
from pathlib import Path
import re
import resource
import struct
import subprocess
import sys
import wave
import zipfile
import xml.etree.ElementTree as ET

MAX_BYTES = 10 * 1024 * 1024
TEXT_LIMIT = 12000


def limited_text(lines):
    output, used, truncated = [], 0, False
    for line in lines:
        size = len(line.encode('utf-8')) + 1
        if used + size > TEXT_LIMIT:
            truncated = True
            break
        output.append(line)
        used += size
    return {'text': '\n'.join(output), 'warnings': ['内容已截断'] if truncated else []}


def xml(data):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('active_content')
    return ET.fromstring(data)


def ooxml(data, suffix):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 512 or len({e.filename for e in entries}) != len(entries):
            raise ValueError('archive_limit')
        total = 0
        for entry in entries:
            name = entry.filename
            total += entry.file_size
            if (name.startswith('/') or '..' in name.split('/') or '\\' in name
                    or entry.flag_bits & 1 or (entry.external_attr >> 16) & 0o170000 == 0o120000
                    or entry.file_size > 8 * 1024 * 1024 or total > 16 * 1024 * 1024
                    or entry.file_size > max(1, entry.compress_size) * 100):
                raise ValueError('archive_limit')
            if any(x in name.lower() for x in ('vba', 'activex', 'embeddings/', 'externallinks/')):
                raise ValueError('active_content')
        # Read all XML and relationships under the validated expansion budget.
        trees = {}
        for entry in entries:
            if entry.filename.endswith(('.xml', '.rels')):
                value = archive.read(entry)
                if b'macroEnabled' in value or b'vbaProject' in value:
                    raise ValueError('active_content')
                root = xml(value)
                for node in root.iter():
                    if (node.attrib.get('TargetMode', '').lower() == 'external'
                            or re.match(r'(?:[a-z]+:|//)', node.attrib.get('Target', ''), re.I)
                            or node.tag.rsplit('}', 1)[-1] in {'altChunk', 'object', 'oleObject'}):
                        raise ValueError('active_content')
                trees[entry.filename] = root
        if '[Content_Types].xml' not in trees:
            raise ValueError('format_mismatch')
        if suffix == '.docx':
            root = trees.get('word/document.xml')
            if root is None:
                raise ValueError('format_mismatch')
            lines = []
            for n, node in enumerate(root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'), 1):
                text = ''.join(x.text or '' for x in node.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'))
                lines.append(f'段落{n}: {text}')
            return limited_text(lines)
        if suffix != '.xlsx' or 'xl/workbook.xml' not in trees:
            raise ValueError('unsupported_format')
        ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
        shared = trees.get('xl/sharedStrings.xml')
        strings = [''.join(x.itertext()) for x in shared] if shared is not None else []
        lines = []
        sheets = sorted((k, v) for k, v in trees.items() if re.fullmatch(r'xl/worksheets/sheet[0-9]+\.xml', k))
        for name, root in sheets:
            for cell in root.iter(ns+'c'):
                coord = cell.attrib.get('r', '')
                if not re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]{0,6}', coord):
                    raise ValueError('invalid_cell')
                value = cell.find(ns+'v')
                text = value.text or '' if value is not None else ''
                if cell.attrib.get('t') == 's':
                    text = strings[int(text)]
                elif cell.attrib.get('t') == 'inlineStr':
                    text = ''.join(x.text or '' for x in cell.iter(ns+'t'))
                formula = cell.find(ns+'f')
                if formula is not None:
                    text = '[公式仅展示，不执行] '+(formula.text or '')+' [缓存值] '+text
                lines.append(f'{name}:{coord}: {text}')
        return limited_text(lines)


def command(args, limit=1024 * 1024):
    # Child tools inherit this process's namespace, rlimits and empty credential environment.
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=12, check=False)
    if result.returncode or len(result.stdout) > limit:
        raise ValueError('decode_failed')
    return result.stdout


def document(data, suffix):
    if suffix in {'.txt', '.md', '.csv'}:
        text = data.decode('utf-8-sig')
        if any(ord(c) < 32 and c not in '\r\n\t' for c in text):
            raise ValueError('format_mismatch')
        if suffix == '.csv':
            csv.field_size_limit(100000)
            reader = csv.reader(io.StringIO(text))
            return limited_text(f'行{reader.line_num}: '+json.dumps(row, ensure_ascii=False) for row in reader)
        return limited_text(f'行{n}: {line}' for n, line in enumerate(text.splitlines(), 1))
    if suffix == '.pdf':
        if not data.startswith(b'%PDF-'):
            raise ValueError('format_mismatch')
        names = re.sub(rb'#([a-fA-F0-9]{2})', lambda m: bytes([int(m[1], 16)]), data)
        if re.search(rb'/(?:JavaScript|JS|OpenAction|AA|Launch|EmbeddedFile|Filespec|URI|RichMedia|AcroForm|XFA|Encrypt|ObjStm)\b', names):
            raise ValueError('active_content')
        Path('/work/input.pdf').write_bytes(data)
        info = command(['pdfinfo', '/work/input.pdf']).decode('utf-8', 'replace')
        pages = re.search(r'^Pages:\s+(\d+)', info, re.M)
        if not pages or int(pages[1]) > 100:
            raise ValueError('page_limit')
        body = command(['pdftotext', '-f', '1', '-l', '20', '-layout', '/work/input.pdf', '-']).decode('utf-8')
        lines = [f'页{n}: {page.strip()}' for n, page in enumerate(body.split('\f')[:-1], 1)]
        result = limited_text(lines)
        if not result['text'].strip():
            result['warnings'].append('扫描件或无文本层；未进行OCR')
        if int(pages[1]) > 20:
            result['warnings'].append('仅提取前20页')
        return result
    if suffix in {'.docx', '.xlsx'} and data.startswith(b'PK\x03\x04'):
        return ooxml(data, suffix)
    raise ValueError('unsupported_format')


def image(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        mime = 'image/png'
    elif data.startswith(b'\xff\xd8\xff'):
        mime = 'image/jpeg'
    elif data.startswith((b'GIF87a', b'GIF89a')):
        mime = 'image/gif'
    elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        mime = 'image/webp'
    else:
        raise ValueError('unsupported_format')
    Path('/work/input').write_bytes(data)
    info = json.loads(command(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', '/work/input']))
    streams = info.get('streams', [])
    if len(streams) != 1:
        raise ValueError('image_stream_limit')
    stream = streams[0]
    width, height = stream.get('width', 0), stream.get('height', 0)
    if not 0 < width * height <= 32000000 or max(width, height) > 16000:
        raise ValueError('pixel_limit')
    # No shell and no metadata copy. Preserve screenshot resolution up to 4096px;
    # larger images are explicitly reported as scaled, never silently called exact OCR.
    command(['ffmpeg', '-v', 'error', '-nostdin', '-threads', '1', '-i', '/work/input',
             '-frames:v', '1', '-vf', 'scale=w=min(4096\\,iw):h=min(4096\\,ih):force_original_aspect_ratio=decrease',
             '-map_metadata', '-1', '-threads', '1', '-y', '/work/normalized.png'])
    output = Path('/work/normalized.png').read_bytes()
    if len(output) > MAX_BYTES:
        raise ValueError('size_limit')
    animated = mime == 'image/gif' or (mime == 'image/webp' and b'ANIM' in data) or (mime == 'image/png' and b'acTL' in data)
    warnings = ['动画仅取首帧'] if animated else []
    if max(width, height) > 4096:
        warnings.append('已缩放至最长边4096；小字需另发局部截图')
    return {'data': base64.b64encode(output).decode(), 'mime': 'image/png',
            'source_mime': mime, 'width': width, 'height': height, 'warnings': warnings}


def audio(data):
    # The real decoder probes bytes, never a renamed extension. SILK support is
    # deployment-dependent; unsupported codecs fail rather than masquerading as WAV.
    Path('/work/input').write_bytes(data)
    info = json.loads(command(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', '/work/input']))
    streams = info.get('streams', [])
    if len(streams) != 1 or streams[0].get('codec_type') != 'audio':
        raise ValueError('decode_failed')
    try:
        duration = float(info.get('format', {}).get('duration', streams[0].get('duration', 'nan')))
    except (TypeError, ValueError):
        raise ValueError('decode_failed') from None
    if not math.isfinite(duration) or not 0 < duration <= 120:
        raise ValueError('duration_limit')
    command(['ffmpeg', '-v', 'error', '-nostdin', '-threads', '1', '-i', '/work/input', '-t', '121',
             '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '-map_metadata', '-1', '-y', '/work/audio.wav'])
    output = Path('/work/audio.wav').read_bytes()
    if len(output) > 16000 * 2 * 120 + 4096:
        raise ValueError('duration_limit')
    with wave.open(io.BytesIO(output)) as wav:
        pcm = wav.readframes(wav.getnframes())
        if wav.getnframes() > 16000 * 120:
            raise ValueError('duration_limit')
    samples = struct.iter_unpack('<h', pcm)
    power = sum(n[0] * n[0] for n in samples) / max(1, len(pcm)//2)
    if power < 32 * 32:
        raise ValueError('silence')
    return {'data': base64.b64encode(output).decode(), 'mime': 'audio/wav', 'duration': duration, 'warnings': []}


def whisper(data):
    if not Path('/models').is_dir():
        raise ValueError('whisper_unavailable')
    Path('/work/audio.wav').write_bytes(data)
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise ValueError('whisper_unavailable') from None
    model = WhisperModel('/models', device='cpu', compute_type='int8', local_files_only=True, cpu_threads=2)
    segments, info = model.transcribe('/work/audio.wav', beam_size=1, vad_filter=True)
    lines, weak = [], False
    for segment in segments:
        lines.append(f'[{segment.start:.2f}-{segment.end:.2f}秒] {segment.text}')
        # Heuristic only, explicitly not a calibrated confidence measure.
        weak |= segment.avg_logprob < -1 or segment.no_speech_prob > .6
        if len(lines) > 256:
            raise ValueError('transcript_limit')
    result = limited_text(lines)
    result.update(language=info.language, low_quality=weak or not lines)
    if result['low_quality']:
        result['warnings'].append('Whisper低质量启发式命中，原转写保留；非校准准确率')
    return result


def run(request):
    data = base64.b64decode(request['data'], validate=True)
    if len(data) > MAX_BYTES:
        raise ValueError('size_limit')
    kind = request['kind']
    if kind == 'image':
        return image(data)
    if kind == 'record':
        return audio(data)
    if kind == 'whisper':
        return whisper(data)
    if kind == 'file':
        return document(data, request.get('suffix', ''))
    raise ValueError('unsupported_format')


if __name__ == '__main__':
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024**2, 16 * 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    os.umask(0o077)
    os.chmod('/work', 0o700)
    try:
        raw = sys.stdin.buffer.read(14 * 1024**2 + 1)
        if len(raw) > 14 * 1024**2:
            raise ValueError('size_limit')
        result = {'status': 'ok', **run(json.loads(raw))}
    except FileNotFoundError:
        result = {'status': 'error', 'error': 'dependency_unavailable'}
    except Exception as exc:
        codes = {'active_content', 'archive_limit', 'format_mismatch', 'unsupported_format', 'page_limit',
                 'decode_failed', 'image_stream_limit', 'pixel_limit', 'size_limit', 'duration_limit',
                 'silence', 'whisper_unavailable', 'transcript_limit'}
        result = {'status': 'error', 'error': str(exc) if str(exc) in codes else 'parse_failed'}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
