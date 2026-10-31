#!/usr/bin/env python3
"""Real model + public HTTPS reading + social state; synthetic capture only.

This never connects QQ. The capture port is the same authored offline contract
fixture used by the component tests. Model responses and the public document are
real; no private profile content or credential files enter the synthetic home.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import importlib
import json
import os
from pathlib import Path
import sys
import time
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_napcat_social_live as base


URL = 'https://arxiv.org/abs/2501.00383'
CHAT = 'group:20001'


async def verify(args, root, route, home, report):
    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    from agent.auxiliary_client import scoped_runtime_main
    from agent.plugin_llm import PluginLlm
    plugin = args.social_plugin_root.resolve(strict=True)
    sys.path.insert(0, str(plugin/'tests'))
    support = importlib.import_module('support')
    module = importlib.import_module('hermes_social')
    manager = PluginManager(scope_key=str(home))
    ctx = PluginContext(PluginManifest(name='hermes-social', kind='standalone', source='user',
                                      path=str(plugin)), manager)
    port = support.FakePort(home)
    controller = module.create_controller(ctx, port, support.policy(targets=[CHAT],
        initiative_mode='model', dialogue={'enabled': True, 'proactive': True},
        activities={'enabled': True, 'sources': {CHAT: [URL]}, 'proactive': True},
        learning={'enabled': True, 'auto_trials': True, 'min_feedback': 2, 'trial_seconds': 600}))
    port.controller = controller
    original = PluginLlm.acomplete
    calls = []
    async def traced(self, messages, **kwargs):
        wire = json.dumps(messages, ensure_ascii=False)
        assert base.CANARY not in wire
        assert 'tools' not in kwargs
        with scoped_runtime_main(route['runtime']):
            response = await original(self, messages=messages, **kwargs)
        calls.append(dict(purpose=kwargs.get('purpose'), provider=response.provider,
                          model=response.model, response=response.text, tools=0))
        report['model_calls'] = calls
        return response
    def observe(text, mid):
        item = support.event(text, mid=mid)
        item.bot_id, item.reply_expected = '90001', False
        controller.observe(item)
        return item
    def record_case(name, passed, **details):
        case = dict(name=name, passed=bool(passed), **details)
        report['cases'].append(case)
        print(json.dumps(case, ensure_ascii=False), flush=True)
    async def select(item):
        history = controller.recent.routing_history(controller._scope(CHAT))
        choice = await controller._select_participation(CHAT,
            [(item, SimpleNamespace(anchor='public_turn'))], history)
        controller.states[CHAT].participation_choice = choice
        return choice
    try:
        with ExitStack() as stack:
            stack.enter_context(patch('agent.auxiliary_client._resolve_codex_credential_and_base',
                return_value=(route['runtime']['api_key'], route['runtime']['base_url'])))
            stack.enter_context(patch.object(PluginLlm, 'acomplete', traced))
            pending = observe('合成计分题还没写完：步骤甲得分暂缺。请先等我下一条补全分值，再回答总分。', 'pending')
            waiting = await select(pending)
            first = json.loads(calls[-1]['response'])['assessments'][0]
            record_case('defer_incomplete_question', waiting is None and first['action'] in {'wait', 'observe'},
                        actual_assessment=first)
            continued = observe('补全刚才合成计分题：步骤甲三分，步骤乙五分。现在请告诉我两步总分。', 'continued')
            choice = await select(continued)
            view = controller.prepare_public_context(continued)
            record_case('fresh_event_continues_prior_topic', choice is not None
                and choice.event is continued and choice.topic_message_id == 'pending'
                and '步骤甲得分暂缺' in view.text,
                selected_message=choice.event.message_id if choice else None,
                topic_message=choice.topic_message_id if choice else None)
            controller.record_self(CHAT, '合成回执：按你提供的三分与五分，总分为八分。', 'capture-1',
                                   reply_to_message_id=continued.message_id)
            closed = observe('这道合成计分题已经结束，到这里就好，不用再回答。', 'closed')
            closing = await select(closed)
            closed_state = controller.life.dialogue.context(controller._scope(CHAT))
            record_case('close_topic', closing is None and any(t.status == 'closed'
                and 'pending' in t.message_ids for t in closed_state),
                actual_assessment=json.loads(calls[-1]['response'])['assessments'][0])
            reopened = observe('重新打开刚才那道合成计分题：如果步骤乙改成四分，总分应是多少？', 'reopened')
            reopened_choice = await select(reopened)
            record_case('reopen_closed_topic', reopened_choice is not None and any(
                t.status == 'active' and 'pending' in t.message_ids and 'reopened' in t.message_ids
                for t in controller.life.dialogue.context(controller._scope(CHAT))),
                actual_assessment=json.loads(calls[-1]['response'])['assessments'][0])
            correction = observe('纠正刚才我写的数字：步骤乙是六分，四分是笔误；请按新数值修正总分。', 'correction')
            corrected_choice = await select(correction)
            actual = json.loads(calls[-1]['response'])['assessments'][0]
            record_case('correct_same_topic', corrected_choice is not None and actual.get('topic_action') == 'correct'
                        and any('pending' in t.message_ids and 'correction' in t.message_ids
                                for t in controller.life.dialogue.context(controller._scope(CHAT))),
                        actual_assessment=actual)
            await controller.life.advance()
            notes = controller.life.activities.notes(CHAT)
            source = notes[0] if notes else None
            record_case('actual_public_reading', source and source['url'] == URL
                        and len(source['note']) > 100 and 'Inner Thoughts' in source['title'],
                        source_url=source['url'] if source else None,
                        title=source['title'] if source else None,
                        excerpt_chars=len(source['note']) if source else 0)
            controller.states[CHAT].last_activity = time.time()-4000
            controller.states[CHAT].pending = None
            sent = await controller.tick(CHAT)
            if not sent:
                record_case('irrelevant_share_can_stay_silent', len(port.attempts) == 0
                            and json.loads(calls[-1]['response'])['speak'] is False)
                observe('本组合成交流最近在研究智能体何时主动插话，欢迎分享与主动对话机制有关、带原文出处的简短发现。', 'reading-interest')
                controller.states[CHAT].last_activity = time.time()-4000
                controller.states[CHAT].last_check = 0
                sent = await controller.tick(CHAT)
            record_case('model_reviewed_source_share', sent and len(port.attempts) == 1
                        and URL in port.attempts[0][1] and any(word in port.attempts[0][1]
                            for word in ('主动', '对话', 'Inner Thoughts', '插话')),
                        capture=port.attempts[0][1] if port.attempts else None)
            for index in range(2):
                request = observe('合成反馈的原请求'+str(index), 'request-'+str(index))
                controller.prepare_public_context(request)
                outbound = 'verified-'+str(index)
                controller.record_self(CHAT, '合成的已核验回复', outbound, reply_to_message_id=request.message_id)
                command = observe('/social feedback '+outbound+' correction', 'feedback-'+str(index))
                controller.control(command)
            strategy = controller.life.strategy(CHAT)
            record_case('feedback_trial_and_rollback', strategy['state'] == 'trial'
                        and controller.reply_char_limit(CHAT) is not None, strategy=strategy)
            if strategy['state'] == 'trial':
                controller.life.activities.revert_trial(CHAT, strategy['trial_id'])
            controller.life.activities.forget_source(CHAT, source['id'])
            record_case('source_forget_and_strategy_revert', not controller.life.material(CHAT)
                        and controller.reply_char_limit(CHAT) is None)
            report.update(model_calls=calls, real_onebot_connection_attempts=0,
                          capture_sends=len(port.attempts), qq_client_acceptance=False)
    finally:
        await controller.stop()
        manager.unload()
        await asyncio.sleep(0)
        if controller.binding.shutdown_task:
            await controller.binding.shutdown_task


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--live', action='store_true')
    p.add_argument('--host', type=Path, required=True)
    p.add_argument('--owner-home', type=Path, required=True)
    p.add_argument('--social-plugin-root', type=Path, required=True)
    p.add_argument('--managed', action='store_true', help=argparse.SUPPRESS)
    args = p.parse_args()
    if not args.live:
        raise SystemExit('Refusing live inference without --live; QQ is never connected.')
    if not args.managed:
        sys.path.insert(0, str(args.host.resolve(strict=True)))
        from hermes_cli._launchers import runtime_command
        code = f"import runpy; runpy.run_path({str(Path(__file__).resolve())!r}, run_name='__main__')"
        raise SystemExit(subprocess.call(runtime_command(args.host, args=[*sys.argv[1:], '--managed'], code=code)))
    os.umask(0o077)
    route = base.read_route(args)
    import tempfile
    root = Path(tempfile.mkdtemp(prefix='social-life-live-', dir=os.environ['TMPDIR']))
    home = base.isolate(root)
    args.shared_allowlist = False
    base.configure(home, route, args)
    report = dict(passed=False, cases=[], scope='synthetic capture; real model and public HTTPS only',
                  fixture_provenance='synthetic-authored-life-v1', qq_client_acceptance=False)
    code = 1
    try:
        asyncio.run(verify(args, root, route, home, report))
        report['passed'] = all(case['passed'] for case in report['cases'])
        code = 0 if report['passed'] else 1
    except Exception as exc:
        report['blocker_type'] = type(exc).__name__
        code = 2
    finally:
        output = root/'report.json'
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(dict(passed=report['passed'], report=str(output),
                              blocker_type=report.get('blocker_type')), ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
