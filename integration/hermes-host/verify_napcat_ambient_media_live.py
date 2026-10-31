#!/usr/bin/env python3
"""真实视觉 → worker → 正式 native 群回合验收器（QQ 仅捕获，不联网）。

本脚本默认拒绝推理，只有 --live 才授权真实当前网关 GPT 视觉和一次当前
Codex 主模型请求；不连接 OneBot，不证明 QQ 下载、发送或客户端可见性。
标准库生成 512x512 白底 PNG，左红方块、右蓝圆；仅固定 example.invalid
URL 可由捕获 fetcher 返回这些真实字节。MediaSandbox、CodexMedia、
strict worker、持久化 FIFO/source codec、GatewayRunner 和 AIAgent 均运行原实现。
不会以假视觉结果或孤立 acomplete 替代正式链路。

沿用同目录 verify_napcat_social_live.py 的 managed bootstrap、隔离 home 和
官方 readonly config/auth resolver。只读取显式 owner-home 的模型、toolsets、
runtime/worker_settings；不复制 auth/.env、
私人历史、SOUL/USER/MEMORY，不发现主人的 MCP/plugins。worker 的既有审核目录
仅以 readonly_home=True 挂载 scoped-memory.json，不写生产配置或 worker 数据。
媒体使用既有 ProviderManagedBudget 策略；视觉固定当前网关配置的 Codex GPT
模型，单次请求不回退其它供应商。依赖、凭据或 API 不可用即阻断，绝不伪造。

用法（--live 会花费真实推理额度；未授权时仅运行 --help）：
  python verify_napcat_ambient_media_live.py --host "$HERMES_SOURCE" \
    --owner-home "$HERMES_HOME" --plugin-root "$NAPCAT_PLUGIN_ROOT" \
    --social-plugin-root "$SOCIAL_PLUGIN_ROOT" --live

先验收未点名纯图片经视觉识别进入同群短期上下文、但不进入 native/主模型/发送；再验收未点名文字+图。
加 --scenario followup 则先发送纯图，再单独发送“看一下上一张表情包是什么内容”，
提问消息不重复夹带图片；必须核对旧图 origin 与本轮 question route 不同且均正确。
所有生成物和中文报告留在 TMPDIR；报告包含合成原回答、视觉派生、实际 wire
工具定义、来源证明、worker/进程证据和凭据扫描。退出码：0 全部通过；1 验收
断言失败；2 依赖/API/合同阻断。--help / 无 --live 不读配置、凭据或调用 API。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import ExitStack
from contextvars import ContextVar
import copy
from dataclasses import asdict
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import threading
import traceback
from unittest.mock import patch
import zlib

sys.dont_write_bytecode = True

IMAGE_URL = 'https://example.invalid/napcat-ambient-media-live.png'
QUESTION = '看这张测试图：左边和右边各是什么颜色、形状？'
FOLLOWUP_QUESTION = '看一下上一张表情包是什么内容'
PURE_ID, QUESTION_ID = '1901', '1902'
EXPECTED_TOOL_COUNT = 20


class VerificationBlocked(RuntimeError):
    """固定原因码；不携带供应商响应、headers 或凭据。"""


def arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--live', action='store_true', help='显式授权真实视觉及主模型 API；永不连接 QQ')
    parser.add_argument('--host', type=Path, required=True, help='当前 Hermes source checkout')
    parser.add_argument('--owner-home', type=Path, required=True, help='当前 profile 的显式目录，仅 readonly 借用路由与凭据')
    parser.add_argument('--plugin-root', type=Path, help='审核过的 NapCat 源码/安装目录，默认仓库源码')
    parser.add_argument('--social-plugin-root', type=Path, help='审核过的 social 插件目录，默认与 NapCat 相邻')
    parser.add_argument('--label', default='ambient-media')
    parser.add_argument('--scenario', choices=['direct', 'followup'], default='direct',
                        help='direct:文字和图同条；followup:先纯图，再按真实自然说法单独提问')
    parser.add_argument('--timeout', type=int, default=240, help='每个回合的排空期限，秒（默认 240）')
    parser.add_argument('--managed', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.live:
        parser.exit(2, 'Refusing live inference without --live; 未读取配置/凭据，未调用模型或 QQ。\n')
    if not 30 <= args.timeout <= 600:
        parser.error('--timeout must be between 30 and 600')
    return args


def launch(args):
    """门槛在任何宿主导入前；使用官方安装绑定 bootstrap，不安装依赖。"""
    if args.managed:
        return
    sys.path.insert(0, str(args.host.resolve(strict=True)))
    from hermes_cli._launchers import runtime_command
    code = 'import runpy; runpy.run_path(' + repr(str(Path(__file__).resolve())) + ", run_name='__main__')"
    command = runtime_command(args.host, args=[*sys.argv[1:], '--managed'], code=code)
    raise SystemExit(subprocess.call(command))


def load_baseline():
    path = Path(__file__).with_name('verify_napcat_social_live.py')
    spec = importlib.util.spec_from_file_location('_napcat_social_live_helpers', path)
    if spec is None or spec.loader is None:
        raise VerificationBlocked('baseline_helpers_unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def synthetic_png():
    """不依赖 Pillow/网络；RGB PNG 的像素即本次验收的唯一图像真值。"""
    width, height = 512, 512
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # PNG scanline filter: None
        for x in range(width):
            if 80 <= x < 240 and 176 <= y < 336:
                color = (255, 0, 0)
            elif (x - 384) ** 2 + (y - 256) ** 2 <= 80 ** 2:
                color = (0, 0, 255)
            else:
                color = (255, 255, 255)
            rows.extend(color)

    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)

    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes(rows))) + chunk(b'IEND', b''))



def raw_image(baseline, text, mid):
    raw = baseline.raw_message(text, int(mid))
    if not text:
        raw['message'] = []
    raw['message'].append({'type': 'image', 'data': {
        'url': IMAGE_URL, 'file': 'synthetic-' + str(mid) + '.png'}})
    return raw


def answer_evidence(text, *, require_positions=True):
    """检查左右颜色/形状配对，不以非空或出现四个孤立关键词冒充看懂。"""
    if not isinstance(text, str):
        text = ''
    lowered = text.lower()
    # 保留原回答供人工复核；这里只给有界、可解释的语义验收。
    left = re.search(r'(?:左(?:边|侧|面)?|left)[\s\S]{0,180}', lowered)
    right = re.search(r'(?:右(?:边|侧|面)?|right)[\s\S]{0,180}', lowered)
    left_text = left.group() if left else ''
    right_text = right.group() if right else ''
    # 截断另一方向，避免在同一句里把左右颠倒误判为正确。
    left_text = re.split(r'右|right', left_text, maxsplit=1)[0]
    right_text = re.split(r'左|left', right_text, maxsplit=1)[0]
    red_square = bool(re.search(r'红|red', left_text) and re.search(r'方块|正方|方形|square', left_text))
    blue_circle = bool(re.search(r'蓝|blue', right_text) and re.search(r'圆|circle|round', right_text))
    # 也接受“红色方块在左边 / red square on the left”，不限定输出模板。
    red_square = red_square or bool(re.search(
        r'(?:红|red)[^，,。;；\n]{0,20}(?:方块|正方|方形|square)[^，,。;；\n]{0,16}(?:左|left)', lowered))
    blue_circle = blue_circle or bool(re.search(
        r'(?:蓝|blue)[^，,。;；\n]{0,20}(?:圆|circle|round)[^，,。;；\n]{0,16}(?:右|right)', lowered))
    if require_positions:
        return {'left_red_square': red_square, 'right_blue_circle': blue_circle,
                'passed': red_square and blue_circle}
    # A general "what is in this meme" request does not require unsolicited
    # left/right wording. Still require each color to be paired with its shape,
    # and reject incorrect positions if the answer chooses to state them.
    red_content = bool(re.search(r'(?:红|red)\s*(?:色\s*)?(?:的\s*)?(?:大|小)?\s*'
                                 r'(?:正方形|方块|方形|square)', lowered))
    blue_content = bool(re.search(r'(?:蓝|blue)\s*(?:色\s*)?(?:的\s*)?(?:大|小)?\s*'
                                  r'(?:圆形|圆圈|圆|circle)', lowered))
    positions_correct = ((not left or red_square) and (not right or blue_circle))
    return {'red_square': red_content, 'blue_circle': blue_content,
            'stated_positions_correct': positions_correct,
            'passed': red_content and blue_content and positions_correct}


def tool_names(definitions):
    return sorted(t.get('function', {}).get('name', t.get('name', '')) for t in definitions)


def serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def flattened(value):
    """只提取消息文本，用于比较实际 native 最后 user 的派生正文。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(flattened(item) for item in value)
    if isinstance(value, dict):
        return flattened(value.get('text', value.get('content', '')))
    return ''


def contains_derivative(text, derivative):
    """派生正文在附件 JSON 内；多行视觉回答会被合法转义，须解码后比较。"""
    marker = '本群已核验媒体资料：'
    if not derivative.get('text') or marker not in text:
        return False
    try:
        attachments, _ = json.JSONDecoder().raw_decode(text.split(marker, 1)[1].lstrip())
    except (ValueError, TypeError):
        return False
    return attachments == [derivative]


class CaptureFetcher:
    """只替换 QQ 下载：精确固定 URL，拒绝任何其他网络/文件输入。"""
    max_bytes = 10 * 1024 ** 2

    def __init__(self, png, records):
        self.png, self.records = png, records

    async def fetch(self, url):
        if url != IMAGE_URL:
            raise VerificationBlocked('non_synthetic_download_forbidden')
        self.records.append({'url': IMAGE_URL, 'bytes': len(self.png),
                             'sha256': hashlib.sha256(self.png).hexdigest(), 'network': False})
        return self.png, 'image/png'


def instrument_media(stack, adapter, state, root, canary, gateway_model, owner_home):
    """仅观察实际调用；每个成功路径调用原实现恰好一次。"""
    import httpx
    provider_module = importlib.import_module('hermes_napcat.media_provider')
    CodexMedia, ProviderManagedBudget = provider_module.CodexMedia, provider_module.ProviderManagedBudget
    MediaSandbox = importlib.import_module('hermes_napcat.media_io').MediaSandbox
    MediaService = importlib.import_module('hermes_napcat.media').MediaService
    ProcessOwner = importlib.import_module('hermes_napcat.process_owner').ProcessOwner
    from run_agent import AIAgent
    from agent import auxiliary_client
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    media = adapter.media
    if (not isinstance(media.sandbox, MediaSandbox) or type(media.provider) is not CodexMedia
            or type(media.provider.budget) is not ProviderManagedBudget):
        raise VerificationBlocked('real_media_provider_or_sandbox_missing')
    original_process = media.sandbox.process
    original_vision = media.provider.recognize
    original_recognize = MediaService.recognize
    original_worker = adapter._isolation_bridge.worker.request
    original_send = httpx.Client.send
    original_start, original_close = ProcessOwner.start, ProcessOwner.close
    original_init = ProcessOwner.__init__
    original_conversation = AIAgent.run_conversation
    original_resolver = auxiliary_client.resolve_provider_client
    process_records = {}
    wire_lock = threading.Lock()
    vision_call = ContextVar('synthetic_vision_call', default=False)

    def owner_vision_client(provider, model, **kwargs):
        # The main synthetic runtime holds an in-memory main-turn credential,
        # while its temporary profile intentionally has no auth store. Resolve
        # vision via the owner's existing pool with the host resolver, then
        # restore scope before sending bytes; never copy a token to test files.
        if provider != 'openai-codex' or model != gateway_model or kwargs != {'raw_codex': False}:
            raise VerificationBlocked('vision_auth_route_changed')
        token = set_hermes_home_override(str(owner_home.resolve(strict=True)))
        try:
            return original_resolver(provider, model, **kwargs)
        finally:
            reset_hermes_home_override(token)

    def conversation(self, *args, **kwargs):
        state['sdk_tools'].append(copy.deepcopy(self.tools))
        return original_conversation(self, *args, **kwargs)

    def process_init(self, command, **kwargs):
        record = {'command': list(command), 'kind': 'parser' if '/parser.py' in command else 'strict-worker'}
        state['processes'].append(record)
        process_records[id(self)] = record
        original_init(self, command, **kwargs)

    async def process_start(self):
        process = await original_start(self)
        process_records[id(self)]['pid'] = process.pid
        return process

    async def process_close(self):
        await original_close(self)
        process = self.launch.result()
        process_records[id(self)].update(returncode=process.returncode, joined=process.returncode is not None)

    async def sandbox(kind, data, suffix=''):
        record = {'kind': kind, 'input_sha256': hashlib.sha256(data).hexdigest(), 'completed': False}
        state['parsers'].append(record)
        try:
            result = await original_process(kind, data, suffix)
        except Exception as exc:
            record['error_type'] = type(exc).__name__
            if re.fullmatch(r'[a-z_]{1,80}', str(exc)):
                record['error_code'] = str(exc)
            raise
        normalized = base64.b64decode(result['data'], validate=True)
        record.update(completed=True, width=result.get('width'), height=result.get('height'),
                      source_mime=result.get('source_mime'), mime=result.get('mime'),
                      output_sha256=hashlib.sha256(normalized).hexdigest(), bytes=len(normalized))
        (root / 'normalized.png').write_bytes(normalized)
        return result

    async def vision(kind, data, **kwargs):
        if len(state['vision']) >= 2:
            raise VerificationBlocked('third_vision_call_forbidden')
        record = {'kind': kind, 'input_sha256': hashlib.sha256(data).hexdigest(),
                  'question': kwargs.get('question'), 'completed': False}
        state['vision'].append(record)
        token = vision_call.set(True)
        try:
            result = await original_vision(kind, data, **kwargs)
        except Exception as exc:
            record['error_type'] = type(exc).__name__
            if re.fullmatch(r'[a-z_]{1,80}', str(exc)):
                record['error_code'] = str(exc)
            raise
        finally:
            vision_call.reset(token)
        record.update(completed=True, result=copy.deepcopy(result))
        if result.get('model') != gateway_model:
            raise VerificationBlocked('vision_model_changed')
        return result

    async def recognize(service, ref, route, client, question):
        record = {'ref': asdict(ref), 'route': asdict(route), 'completed': False}
        state['recognitions'].append(record)
        # Preserve the task-local guarded copy of MediaService used by optional
        # media; rebinding to the original instance would bypass its I/O guards.
        result = await original_recognize(service, ref, route, client, question)
        record.update(completed=True, derivative=copy.deepcopy(result))
        return result

    async def worker(payload):
        record = {'payload': copy.deepcopy(payload), 'completed': False}
        state['worker_payloads'].append(record)
        try:
            result = await original_worker(payload)
        except Exception as exc:
            record['error_type'] = type(exc).__name__
            if re.fullmatch(r'[a-z_]{1,80}', str(exc)):
                record['error_code'] = str(exc)
            raise
        record.update(completed=True, response=copy.deepcopy(result))
        return result

    def main_send(self, request, **kwargs):
        # 捕获 SDK 序列化后的真实 HTTP body，而不只是 kwargs builder 的候选。
        if request.url.host == 'chatgpt.com' and request.url.path.rstrip('/').endswith('/responses'):
            with wire_lock:
                vision_request = vision_call.get()
                records = state['vision_wire'] if vision_request else state['main_wire']
                if len(records) >= (2 if vision_request else 1):
                    raise VerificationBlocked('unexpected_extra_model_call')
                if not vision_request and (not state['vision'] or not state['vision'][-1].get('completed')):
                    raise VerificationBlocked('main_inference_without_real_vision_forbidden')
                if request.method != 'POST' or not request.url.path.startswith('/backend-api/codex/'):
                    raise VerificationBlocked('main_endpoint_changed')
                payload = json.loads(request.content)
                if canary in serialized(payload) or payload.get('model') != gateway_model:
                    raise VerificationBlocked('private_context_or_model_changed')
                if vision_request:
                    parts = [p for item in payload.get('input', []) for p in item.get('content', [])
                             if isinstance(p, dict) and p.get('type') == 'input_image']
                    if (len(parts) != 1 or not parts[0].get('image_url', '').startswith('data:image/png;base64,')
                            or payload.get('store') is not False or payload.get('tools')):
                        raise VerificationBlocked('codex_vision_wire_invalid')
                record = {'method': request.method, 'endpoint': str(request.url), 'payload': payload,
                          'tools': tool_names(payload.get('tools') or [])}
                records.append(record)
            response = original_send(self, request, **kwargs)
            record['http_status'] = response.status_code
            return response
        return original_send(self, request, **kwargs)

    stack.enter_context(patch.object(ProcessOwner, '__init__', process_init))
    stack.enter_context(patch.object(AIAgent, 'run_conversation', conversation))
    stack.enter_context(patch.object(ProcessOwner, 'start', process_start))
    stack.enter_context(patch.object(ProcessOwner, 'close', process_close))
    stack.enter_context(patch.object(media.sandbox, 'process', sandbox))
    stack.enter_context(patch.object(media.provider, 'recognize', vision))
    stack.enter_context(patch.object(auxiliary_client, 'resolve_provider_client', owner_vision_client))
    stack.enter_context(patch.object(MediaService, 'recognize', recognize))
    stack.enter_context(patch.object(adapter._isolation_bridge.worker, 'request', worker))

    stack.enter_context(patch.object(httpx.Client, 'send', main_send))


def source_metadata(adapter, mid):
    """实际读取 metadata/provenance 索引，不手工添加 ref 或来源证明。"""
    Route = importlib.import_module('hermes_napcat.isolation').Route
    policy = adapter.settings.isolation
    route = Route(policy.domain_id, policy.social_profile, policy.revision, '10001', 'group:20001', 'group', mid)
    scope = adapter.media.scope(route, adapter.settings.self_id)
    rows = [{'scope': list(key[:5]), 'ref': asdict(ref)}
            for key, (_, ref) in adapter.media.index.rows.items() if key[:5] == scope and ref.origin == mid]
    return {'rows': rows, 'reference_source_matches': adapter.media.reference_sources.matches(
        adapter.settings.self_id, route, mid, route.user_id)}


def check_case(name, state, before, sink, send_before, metadata, route, ledger=None, *, followup=False, visual_context=None):
    trace = {key: value[before[key]:] for key, value in state.items()}
    sends = [c for c in sink.calls[send_before:] if c['action'].startswith('send_')]
    errors = []
    if not metadata['rows'] or not metadata['reference_source_matches']:
        errors.append('未登记精确当前群/作者的媒体索引及 reference provenance')
    if name == 'pure_image':
        if any(trace[k] for k in ('handlers', 'turns', 'agents', 'restores', 'requests',
                                  'main_wire')) or sends:
            errors.append('未点名纯图不应启动 native/主模型/发送')
        if (len(trace['vision']) != 1 or not trace['vision'][0].get('completed')
                or len(trace['vision_wire']) != 1 or trace['vision_wire'][0].get('http_status') != 200
                or len(trace['parsers']) != 1 or not trace['parsers'][0].get('completed')
                or len(trace['fetches']) != 1 or len(trace['recognitions']) != 1):
            errors.append('未点名纯图须经真实下载/解析/视觉且获得 HTTP 200')
        derivative = trace['recognitions'][0].get('derivative', {}) if trace['recognitions'] else {}
        if (not derivative.get('text') or derivative.get('origin') != PURE_ID
                or derivative.get('author') != '10001'
                or derivative.get('method') != route['model']['default']
                or not visual_context or not any(
                    r.get('message_id') == PURE_ID and derivative['text'][:30] in r.get('text', '')
                    and '图片内容未识别' not in r.get('text', '') for r in visual_context)):
            errors.append('未点名纯图的视觉派生未替换同群精确消息的未识别标记')
        if any(r['payload'].get('operation') == 'prepare' for r in trace['worker_payloads']):
            errors.append('纯图不应 worker prepare 正式会话')
        return {'name': name, 'passed': not errors, 'errors': errors, 'metadata': metadata,
                'native_count': len(trace['turns']), 'main_http_count': len(trace['main_wire']),
                'vision_http_count': len(trace['vision_wire']), 'send_count': len(sends),
                'visual_context': visual_context}

    agents, turns = trace['agents'], trace['turns']
    final = agents[-1].get('final_response') if agents else None
    actual = '\n'.join(''.join(s.get('data', {}).get('text', '') for s in c['payload']['message']
                              if s.get('type') == 'text') for c in sends)
    if len(agents) != 1 or not all(a['class'] == 'run_agent.AIAgent' and a.get('completed')
            and a['model'] == route['model']['default'] and a['provider'] == 'openai-codex'
            and a.get('api_calls') == 1 for a in agents):
        errors.append('须完成一次当前 Codex 正式 AIAgent/model 回合')
    if len(turns) != 1 or not all(t['executing'] and t['live_permit'] and t['ambient_valid']
            and t['message_id'] == QUESTION_ID and t['actor'] == '10001' and t['chat_id'] == 'group:20001'
            and t['reply_expected'] is False for t in turns):
        errors.append('正式 FIFO / 精确来源 ambient permit / 群作者证明失败')
    if not trace['restores'] or not all(r['exact_live_source'] and r['permit_active']
            and r['message_id'] == QUESTION_ID for r in trace['restores']):
        errors.append('持久化 source codec 未恢复仍有效的精确 source')
    if not trace['handlers'] or not any(h['executing'] for h in trace['handlers']):
        errors.append('正式 handler 未在 executing admission 中运行')
    if len(trace['vision']) != 1 or not trace['vision'][0].get('completed'):
        errors.append('须完成一次真实 CodexMedia.recognize')
    if len(trace['vision_wire']) != 1 or trace['vision_wire'][0].get('http_status') != 200:
        errors.append('须获得一次真实视觉 HTTP 200；不允许假结果')
    if len(trace['main_wire']) != 1 or trace['main_wire'][0].get('http_status') != 200:
        errors.append('须获得一次真实主模型 HTTP 200')
    if trace['main_wire'] and trace['main_wire'][0]['payload'].get('model') != route['model']['default']:
        errors.append('实际 HTTP 请求不是当前配置的主模型')
    if len(trace['parsers']) != 1 or not all(p.get('completed') and p['width'] == 512 and p['height'] == 512
            and p['mime'] == p['source_mime'] == 'image/png' for p in trace['parsers']):
        errors.append('真实 MediaSandbox 图片归一化失败或未执行')
    parser_processes = [p for p in trace['processes'] if p['kind'] == 'parser']
    worker_processes = [p for p in trace['processes'] if p['kind'] == 'strict-worker']
    if (not parser_processes or not worker_processes or not all(p.get('pid') and p.get('joined')
            and p.get('returncode') == 0 for p in [*parser_processes, *worker_processes])):
        errors.append('真实 namespace parser/worker 进程未成功退出并 join')
    if (trace['parsers'] and trace['vision'] and trace['parsers'][0].get('output_sha256')
            != trace['vision'][0]['input_sha256']):
        errors.append('视觉输入不是实际 parser 归一化结果')
    derivative = trace['recognitions'][0].get('derivative', {}) if trace['recognitions'] else {}
    media_origin, segment = (PURE_ID, 0) if followup else (QUESTION_ID, 1)
    if trace['recognitions']:
        recognition = trace['recognitions'][0]
        ref, media_route = recognition['ref'], recognition['route']
        if (ref.get('origin') != media_origin or ref.get('author') != '10001' or ref.get('index') != segment
                or ref.get('kind') != 'image' or ref.get('location') != IMAGE_URL
                or media_route.get('message_id') != QUESTION_ID or media_route.get('user_id') != '10001'
                or media_route.get('chat_id') != 'group:20001' or media_route.get('chat_type') != 'group'
                or media_route.get('domain_id') != route['worker_settings']['domain_id']
                or media_route.get('policy_revision') != route['worker_settings']['policy_revision']):
            errors.append('媒体 ref/route 不是本轮已准入的精确群/作者/消息/策略')
    if (len(trace['recognitions']) != 1 or derivative.get('origin') != media_origin
            or derivative.get('author') != '10001' or derivative.get('kind') != 'image'
            or not derivative.get('text') or derivative.get('method') != route['model']['default']):
        errors.append('视觉派生缺失或 origin/author/kind/model 不正确')
    if followup and (not trace['vision'] or trace['vision'][0].get('question') != FOLLOWUP_QUESTION):
        errors.append('视觉问题不是单独发送的原话，或混入了社交近期 JSON')
    prepare = [r for r in trace['worker_payloads'] if r['payload'].get('operation') == 'prepare']
    if (len(prepare) != 1 or not prepare[0].get('completed')
            or prepare[0]['payload'].get('attachments') != [derivative]
            or prepare[0]['payload']['route'].get('message_id') != QUESTION_ID):
        errors.append('真实 strict worker 未校验精确本轮视觉附件')
    requests = trace['requests']
    last_user = ''
    if requests:
        users = [m for m in requests[-1]['messages'] if m.get('role') == 'user']
        last_user = flattened(users[-1].get('content')) if users else ''
    wire_user = ''
    if trace['main_wire']:
        inputs = trace['main_wire'][-1]['payload'].get('input') or []
        users = [m for m in inputs if isinstance(m, dict) and m.get('role') == 'user']
        wire_user = flattened(users[-1].get('content')) if users else ''
    if not contains_derivative(last_user, derivative) or not contains_derivative(wire_user, derivative):
        errors.append('native 最后 user / 实际 HTTP body 只有占位符或丢失视觉派生')
    expected = route['expected_tools']
    if not agents or agents[0]['tools'] != tool_names(expected) or len(agents[0]['tools']) != EXPECTED_TOOL_COUNT:
        errors.append('原有 SDK 工具表面漂移；不得缩成 web-only')
    normalize = lambda tools: sorted(tools, key=lambda t: tool_names([t]))
    if len(trace['sdk_tools']) != 1 or serialized(normalize(trace['sdk_tools'][0])) != serialized(normalize(expected)):
        errors.append('实际 AIAgent SDK schema 与当前 registry 完整定义不符')
    if not trace['main_wire'] or len(trace['main_wire'][0]['payload'].get('tools') or []) != EXPECTED_TOOL_COUNT:
        errors.append('实际主模型 wire 未携带完整 20 个工具定义')
    if (not requests or not trace['main_wire'] or trace['main_wire'][0]['tools'] != requests[-1]['tools']
            or not {'terminal', 'read_file', 'tool_call', 'web_search', 'web_extract'}.issubset(requests[-1]['tools'])):
        errors.append('实际 wire 工具与正式 provider builder 不符或丢失既有工具')
    if not ledger or any(r['status'] != 'terminal' or r['outcome'] != 'completed' for r in ledger):
        errors.append('durable ledger 未 terminal/completed')
    evidence = answer_evidence(final, require_positions=not followup)
    if not evidence['passed']:
        errors.append('最终回答未正确配对红色方块与蓝色圆形，或陈述了错误位置')
    if not sends or not isinstance(final, str) or final.strip() not in actual:
        errors.append('正式最终回答未送达本地 capture sink')
    return {'name': name, 'passed': not errors, 'errors': errors,
            'input': FOLLOWUP_QUESTION if followup else QUESTION, 'metadata': metadata,
            'final_response': final, 'captured_text': actual, 'answer_evidence': evidence,
            'vision_derivative': derivative, 'native_last_user': last_user, 'wire_last_user': wire_user,
            'main_http_count': len(trace['main_wire']), 'vision_http_count': len(trace['vision_wire']),
            'send_count': len(sends), 'ledger': ledger}


async def verify(args, root, route, home, report, baseline):
    from gateway.run import GatewayRunner, _runtime_agent_kwargs
    import gateway.run as gateway_run
    from gateway.config import Platform, PlatformConfig, HomeChannel
    from gateway import platform_registry
    from gateway.platform_registry import PlatformRegistry
    from gateway.session_authority import initialize_session_authority
    from hermes_state_runtime import list_session_admissions
    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    from model_tools import get_tool_definitions

    plugin_root = (args.plugin_root or Path(__file__).resolve().parents[2] / 'plugins/hermes-napcat').resolve(strict=True)
    plugin, _ = baseline.load_plugin(plugin_root, 'hermes_napcat')
    social_root = (args.social_plugin_root or plugin_root.parent / 'hermes-social').resolve(strict=True)
    social, _ = baseline.load_plugin(social_root, 'hermes_social')
    sink, registry = baseline.CaptureOneBot(), PlatformRegistry()
    manager = PluginManager(scope_key=str(home))
    ctx = PluginContext(PluginManifest(name='hermes-napcat', kind='platform', source='user', path=str(plugin_root)), manager)
    social_ctx = PluginContext(PluginManifest(name='hermes-social', source='user', path=str(social_root)), manager)
    runner = adapter = None
    state = {key: [] for key in ('handlers', 'turns', 'agents', 'restores', 'requests', 'worker_requests',
                                 'parsers', 'vision', 'vision_wire', 'recognitions', 'worker_payloads',
                                 'main_wire', 'fetches', 'processes', 'sdk_tools')}
    report['trace'] = state
    with ExitStack() as stack:
        stack.enter_context(patch.object(gateway_run, '_resolve_runtime_agent_kwargs',
                                         lambda: _runtime_agent_kwargs(route['runtime'])))
        stack.enter_context(patch.object(platform_registry, 'platform_registry', registry))
        stack.enter_context(patch('hermes_cli.plugins.get_plugin_manager', return_value=manager))
        plugin.register(ctx)
        social.register(social_ctx)
        policy = route['worker_settings']
        cfg = PlatformConfig(enabled=True, extra={
            'self_id': '90001', 'owner_id': '10001', 'allowed_users': ['10001'],
            'allowed_groups': ['20001'], 'group_admission': 'all_members', 'require_mention': True,
            'group_trigger': {'mode': 'name_or_mention', 'name': '测试实例'},
            'isolation': {'mode': 'strict', 'private_profile': 'default', 'social_profile': 'social',
                          'domain_id': policy['domain_id'], 'policy_revision': policy['policy_revision'], 'guest_dm_users': []},
            'runtime': {'mode': 'social_agent', 'contract': 5, 'social_home': str(route['worker_home'])},
            'media': {'enabled': True, 'groups': ['20001'], 'types': ['image'], 'observe_metadata': True,
                      'references': True, 'download_hosts': ['example.invalid'], 'download_policy': 'allowlist',
                      'cloud': ['image'], 'allow_derived_text': True, 'budget_mode': 'provider'},
        })
        cfg.home_channel = HomeChannel(Platform('napcat'), 'group:20001', 'Synthetic media capture target')

        def forbidden_transport(*_args, **_kwargs):
            report['real_onebot_connection_attempts'] += 1
            raise VerificationBlocked('real_onebot_forbidden')

        stack.enter_context(patch('hermes_napcat.adapter.OneBotClient', forbidden_transport))
        try:
            runner = GatewayRunner()
            runner.config.multiplex_profiles = False
            runner.config.platforms[Platform('napcat')] = cfg
            authority = await initialize_session_authority(runner, profile_id='default', instance_id='synthetic-live-ambient-media')
            adapter = registry.create_adapter('napcat', cfg)
            if adapter is None:
                raise VerificationBlocked('real_adapter_factory_rejected_runtime')
            adapter.client = sink
            adapter._wire_plugin_handlers(None)
            if not adapter.participation.active or adapter.participation.policy.participation_groups != adapter.settings.groups:
                raise VerificationBlocked('independent_social_shared_allowlist_not_wired')
            adapter.gateway_runner = runner
            runner.adapters[Platform('napcat')] = adapter
            if not adapter.social_ready() or adapter._isolation_bridge.worker.readonly_home is not True:
                raise VerificationBlocked('strict_gateway_readiness_or_readonly_worker_failed')
            report.update(plugin_roots={'hermes-napcat': str(plugin_root), 'hermes-social': str(social_root)},
                          worker_home=str(route['worker_home']), worker_readonly_home=True,
                          media_budget_mode='provider', configured_media_model=route['model']['default'],
                          media_model_source='gateway default (not CLI session override)')
            # 从当前 SDK 实际 registry 求值，不手写工具字段，不实例化孤立主模型。
            expected = get_tool_definitions(enabled_toolsets=route['toolsets'], quiet_mode=True)
            route['expected_tools'] = copy.deepcopy(expected)
            report['expected_sdk_tools'] = copy.deepcopy(expected)
            if len(expected) != EXPECTED_TOOL_COUNT or any('mobile' in n.lower() for n in tool_names(expected)):
                raise VerificationBlocked('current_sdk_tool_surface_not_reviewed_20')
            baseline.instrument(stack, runner, adapter, state)
            png = synthetic_png()
            (root / 'synthetic.png').write_bytes(png)
            adapter.media.fetcher = CaptureFetcher(png, state['fetches'])
            instrument_media(stack, adapter, state, root, baseline.CANARY, route['model']['default'], args.owner_home)
            runner._wire_adapter_handlers(adapter)
            followup = args.scenario == 'followup'
            question = FOLLOWUP_QUESTION if followup else QUESTION
            report['scenario'] = args.scenario
            for name, mid, text in (('pure_image', PURE_ID, ''), ('unaddressed_image_question', QUESTION_ID, question)):
                before = {key: len(value) for key, value in state.items()}
                send_before = len(sink.calls)
                raw = (baseline.raw_message(text, int(mid)) if followup and name != 'pure_image'
                       else raw_image(baseline, text, mid))
                await adapter._receive(raw)
                await baseline.drain(adapter, authority, timeout=args.timeout)
                metadata = source_metadata(adapter, PURE_ID if followup else mid)
                ledger = None
                if name != 'pure_image':
                    source = adapter.build_source(chat_id='group:20001', chat_type='group', user_id='10001', message_id=mid)
                    entry = runner.session_store.get_or_create_session(source)
                    rows = list_session_admissions(authority.db, session_id=entry.session_id, pending_only=False)
                    ledger = [{'status': row['status'], 'outcome': row['outcome']} for row in rows]
                recent = adapter.participation.context('group:20001') if name == 'pure_image' else None
                case = check_case(name, state, before, sink, send_before, metadata, route, ledger,
                                  followup=followup, visual_context=recent)
                report['cases'].append(case)
                failures = [r for key in ('parsers', 'vision', 'worker_payloads') for r in state[key]
                            if r.get('error_type')]
                bad_http = [r for key in ('vision_wire', 'main_wire') for r in state[key]
                            if r.get('http_status') != 200]
                if failures or bad_http:
                    report['infrastructure_blocked'] = True
                # 失败纯图不能继续花钱；仍保留完整失败报告。
                if not case['passed']:
                    break
            # 捕获端也精确读回；不是 QQ 发送验收。
            readbacks = []
            for mid, message in list(sink.messages.items()):
                echoed = await sink.call('get_msg', {'message_id': mid})
                readbacks.append({'message_id': mid, 'matches': echoed == message})
            report['capture_readbacks'] = readbacks
            report['passed'] = (len(report['cases']) == 2 and all(c['passed'] for c in report['cases'])
                                and bool(readbacks) and all(r['matches'] for r in readbacks)
                                and report['real_onebot_connection_attempts'] == 0)
        finally:
            report['onebot_actions'] = sink.calls
            if adapter is not None:
                await adapter.disconnect()
            if runner is not None:
                for cached in runner._agent_cache.values():
                    agent = cached[0] if isinstance(cached, tuple) else cached
                    if callable(getattr(agent, 'close', None)):
                        agent.close()
                await runner._session_db.close()
            manager.unload()
            from gateway.status import flush_runtime_status
            flush_runtime_status(timeout=2)


def scan_credentials(root, secrets):
    """扫描最终报告、DB/WAL、logs 等全部自有文件；不输出匹配值。"""
    needles = set()
    for value in secrets:
        if isinstance(value, str) and value:
            needles.add(value.encode())
            needles.add(json.dumps(value, ensure_ascii=False)[1:-1].encode())
    leaks, unreadable, scanned = [], [], 0
    for path in root.rglob('*'):
        if path.is_file():
            try:
                data = path.read_bytes()
            except OSError:
                unreadable.append(str(path.relative_to(root)))
                continue
            scanned += 1
            if any(needle in data for needle in needles):
                leaks.append(str(path.relative_to(root)))
    return {'passed': bool(needles) and not leaks and not unreadable, 'files_scanned': scanned,
            'leaked_files': leaks, 'unreadable_files': unreadable, 'credential_values_reported': False,
            'scope': '全部本次 TMPDIR 生成物；未复制 owner auth/.env'}


def write_report(root, report, secrets):
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    # 意外 echo 的凭据也不允许由本验收器写入报告。
    for secret in secrets:
        if not isinstance(secret, str) or not secret:
            continue
        for spelling in (secret, json.dumps(secret, ensure_ascii=False)[1:-1]):
            if spelling in text:
                report['credential_redaction_required'] = True
                report['passed'] = False
                text = text.replace(spelling, '[REDACTED_CREDENTIAL]')
    if report.get('credential_redaction_required'):
        # text 已脱敏；同步该验收失败标记，避免报告自称通过。
        safe = json.loads(text)
        safe.update(credential_redaction_required=True, passed=False)
        text = json.dumps(safe, ensure_ascii=False, indent=2)
    path = root / 'report.json'
    path.write_text(text)
    os.chmod(path, 0o600)


def main():
    args = arguments()
    launch(args)
    tmp = Path(os.environ['TMPDIR']).resolve(strict=True)
    root = Path(tempfile.mkdtemp(prefix='napcat-ambient-media-' + re.sub(r'[^a-zA-Z0-9_-]', '_', args.label) + '-', dir=tmp))
    os.chmod(root, 0o700)
    report = {'label': args.label, 'passed': False, 'cases': [], 'artifact_root': str(root),
              'synthetic_content_only': True, 'real_onebot_connection_attempts': 0,
              'qq_download_verified': False, 'qq_send_verified': False, 'qq_client_acceptance': False,
              'capture_only': True, 'two_call_layers': ['OpenRouterMedia.recognize', 'GatewayRunner → run_agent.AIAgent'],
              'credential_mode': 'official readonly owner resolver + in-memory media key; no auth/env copy',
              'limitations': ['QQ 下载被精确 URL 捕获替换；QQ 发送仅本地捕获，未连接真实 OneBot',
                              '仅当前合成图片的未点名纯图与文字+图片；未覆盖其他模态/撤回/断线/引用回归']}
    print('ARTIFACT_ROOT=' + str(root), flush=True)
    secrets, code, baseline = [], 2, None
    try:
        baseline = load_baseline()
        home = baseline.isolate(root)
        (home / 'USER.md').write_text(baseline.CANARY)
        # 隔离后才导入 host config/provider；不会 import-time 发现主人插件。
        route = baseline.read_route(args)
        secrets.append(route['credential'])
        report.update(configured_model=route['model']['default'], configured_provider=route['model']['provider'],
                      configured_toolsets=route['toolsets'], credential_source=route['credential_source'])
        if route['model']['provider'] != 'openai-codex' or not route['model']['default'].startswith('gpt-'):
            raise VerificationBlocked('gateway_gpt_model_unavailable')
        # baseline.configure 只写本次临时 home，主模型仍为原 Codex readonly 登录。
        args.shared_allowlist = True
        baseline.configure(home, route, args)
        asyncio.run(verify(args, root, route, home, report, baseline))
        code = 2 if report.get('infrastructure_blocked') else 0 if report['passed'] else 1
    except Exception as exc:
        # 不输出异常值：API 异常可能带 headers/签名 URL。
        report['blocker_type'] = type(exc).__name__
        if isinstance(exc, VerificationBlocked):
            report['blocker_code'] = str(exc)
        report['blocker_location'] = [(f.filename, f.lineno, f.name) for f in traceback.extract_tb(exc.__traceback__)]
        report['passed'] = False
        print('BLOCKER_TYPE=' + type(exc).__name__, flush=True)
        code = 2
    finally:
        write_report(root, report, secrets)
        report['credential_scan'] = scan_credentials(root, secrets)
        report['no_credentials_written'] = report['credential_scan']['passed'] and not report.get('credential_redaction_required', False)
        if not report['no_credentials_written']:
            report['passed'] = False
            code = max(code, 1)
        if report.get('trace'):
            state = report['trace']
            report['actual_call_counts'] = {'vision_http': len(state['vision_wire']), 'main_http': len(state['main_wire']),
                                            'real_aiagent_turns': len(state['agents'])}
            inspected = [state['requests'], state['vision_wire'], state['main_wire'], state['agents'],
                         state['turns'], report.get('onebot_actions')]
            report['privacy_canary_absent_from_model_and_output'] = baseline is not None and baseline.CANARY not in serialized(inspected)
            if not report['privacy_canary_absent_from_model_and_output']:
                report['passed'] = False
                code = max(code, 1)
        write_report(root, report, secrets)
        print('REPORT=' + str(root / 'report.json'), flush=True)
        print('RESULT=' + ('PASS' if report['passed'] else 'BLOCKED' if code == 2 else 'FAIL'), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
