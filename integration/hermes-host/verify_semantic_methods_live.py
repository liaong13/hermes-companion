#!/usr/bin/env python3
"""Synthetic error evidence, real method generation/validation/body, no QQ.

The two initial erroneous ACKs are authored fixtures, not model outputs. The
candidate, independent task results, later body and feedback classifier are real
model requests. A successful baseline is retained in the report, not weakened.
"""
import argparse
import asyncio
from contextlib import ExitStack
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import verify_napcat_social_live as base

CHAT = 'group:20001'


async def verify(args,root,route,home,report):
    from hermes_cli.plugins import PluginContext,PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    from agent.auxiliary_client import scoped_runtime_main
    from agent.plugin_llm import PluginLlm
    sys.path.insert(0,str(args.social_plugin_root/'tests'))
    support = importlib.import_module('support')
    module = importlib.import_module('hermes_social')
    manager = PluginManager(scope_key=str(home))
    ctx = PluginContext(PluginManifest(name='hermes-social',kind='standalone',source='user',
                                     path=str(args.social_plugin_root)),manager)
    port = support.FakePort(home)
    controller = module.create_controller(ctx,port,support.policy(targets=[CHAT],
        learning=dict(enabled=True,semantic=True,semantic_auto_trials=True,min_feedback=2,
                      daily_method_generations=1),activities=dict(enabled=False)))
    port.controller = controller
    original = PluginLlm.acomplete
    calls = []
    async def traced(self,messages,**kwargs):
        assert base.CANARY not in json.dumps(messages,ensure_ascii=False)
        assert 'tools' not in kwargs
        with scoped_runtime_main(route['runtime']):
            response = await original(self,messages=messages,**kwargs)
        calls.append(dict(purpose=kwargs.get('purpose'),provider=response.provider,
            model=response.model,text=response.text,tools=0))
        return response
    def observe(text,mid,user='10001',quoted=''):
        item = support.event(text,mid=mid,user=user,name='合成成员甲' if user=='10001' else '合成成员乙')
        item.bot_id,item.reply_expected = '90001',False
        if quoted:
            item.reply_to_message_id,item.verified_reply_author_id = quoted,'90001'
        controller.observe(item)
        return item
    async def drain():
        for _ in range(12):
            pending = [task for task in controller._tasks if not task.done()]
            if not pending:
                return
            await asyncio.wait_for(asyncio.gather(*pending),180)
        raise RuntimeError('method tasks did not settle')
    def case(name,passed,**details):
        row = dict(name=name,passed=bool(passed),**details)
        report['cases'].append(row)
        print(json.dumps(row,ensure_ascii=False),flush=True)
    try:
        with ExitStack() as stack:
            stack.enter_context(patch('agent.auxiliary_client._resolve_codex_credential_and_base',
                return_value=(route['runtime']['api_key'],route['runtime']['base_url'])))
            stack.enter_context(patch.object(PluginLlm,'acomplete',traced))
            # Entirely authored past-error fixtures. No real conversation is
            # copied, and no fake is used for the model requests below.
            for index,token in enumerate(('环形纸签','双层夹片')):
                observe('我为合成拼装建议使用'+token+'。','suggestion-'+str(index),user='10002')
                request = observe('合成成员乙建议使用'+token+'，我决定采用。请整理建议与决定。','request-'+str(index))
                controller.prepare_public_context(request)
                outbound = 'authored-wrong-'+str(index)
                controller.record_self(CHAT,'你提出使用'+token+'，我按你的提议整理。',outbound,
                                       reply_to_message_id=request.message_id)
                observe('这里把作者说错了：提议者是合成成员乙，我只是决定采用，请更正归属。',
                        'feedback-'+str(index),quoted=outbound)
                await drain()
            await controller.life.advance()
            await drain()
            methods = controller.life.enhanced.methods
            proposed = methods.list_methods(CHAT)
            active = [row for row in proposed if row['state'] in {'trial','retained'}]
            case('real_candidate_and_independent_validation',len(active)==1
                 and any(row['purpose']=='social_method_proposal' for row in calls)
                 and sum(row['purpose']=='social_method_validation' for row in calls)==4,
                 states=[row['state'] for row in proposed])
            if not active:
                return
            method = active[0]
            validations = methods.validations(CHAT,method['id'])
            improvement = any(not row['baseline_pass'] and row['method_pass'] for row in validations)
            case('honest_baseline_comparison',len(validations)==2
                 and all(row['method_pass'] for row in validations)
                 and (method['state']=='retained')==improvement,validations=validations)
            observe('这次合成拼装我提出采用格纹边框。','new-suggestion',user='10002')
            request = observe('我选择采用合成成员乙的格纹边框。请按本轮适用的方法区分作者和采用者。',
                              'new-request')
            view = controller.prepare_public_context(request)
            case('method_is_in_scoped_actual_body_context',method['id'] in view.text
                 and not controller.life.enhanced.method_context('group:20002'))
            response = await controller.life.enhanced._complete(CHAT,[
                {'role':'system','content':controller.prompt_for_chat(CHAT)+'\n'+view.text+
                 '\n请完成当前任务，只返回JSON {"proposer":"合成成员甲或合成成员乙","adopter":"合成成员甲或合成成员乙"}。'},
                {'role':'user','content':request.text}],purpose='social_method_live_body')
            answer = json.loads(response.text)
            case('actual_body_uses_method_on_new_task',answer==dict(proposer='合成成员乙',adopter='合成成员甲'),
                 actual=response.text)
            await controller.prepare_reply(request,response.text)
            controller.record_self(CHAT,response.text,'new-verified-ack',reply_to_message_id=request.message_id)
            uses = methods.uses_for_delivery(CHAT,'new-verified-ack')
            case('actual_ack_method_use_recorded',bool(uses))
            observe('我明确拒绝这轮所用的方法，请停止用这条方法处理后续聊天。',
                    'method-rejection',quoted='new-verified-ack')
            await drain()
            case('later_real_model_feedback_revokes_method',methods.method(CHAT,method['id'])['state']=='reverted'
                 and not controller.life.enhanced.method_context(CHAT))
            controller.forget_messages(CHAT,{'feedback-0'})
            row = methods.method(CHAT,method['id'])
            case('forgotten_method_summary_not_reused',not methods.context(CHAT)
                 and row['instruction']=='')
    finally:
        report['model_calls'] = calls
        report.update(real_onebot_connection_attempts=0,qq_client_acceptance=False,
                      authored_error_ack_fixtures=2)
        await controller.stop()
        manager.unload()
        await asyncio.sleep(0)
        if controller.binding.shutdown_task:
            await controller.binding.shutdown_task


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--host',type=Path,required=True)
    parser.add_argument('--owner-home',type=Path,required=True)
    parser.add_argument('--social-plugin-root',type=Path,required=True)
    parser.add_argument('--managed',action='store_true',help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.live:
        raise SystemExit('Explicit --live required; no QQ is ever connected')
    args.social_plugin_root = args.social_plugin_root.resolve(strict=True)
    if not args.managed:
        sys.path.insert(0,str(args.host.resolve(strict=True)))
        from hermes_cli._launchers import runtime_command
        code = 'import runpy;runpy.run_path('+repr(str(Path(__file__).resolve()))+",run_name='__main__')"
        return subprocess.call(runtime_command(args.host,args=[*sys.argv[1:],'--managed'],code=code))
    os.umask(0o077)
    route = base.read_route(args)
    root = Path(tempfile.mkdtemp(prefix='semantic-method-live-',dir=os.environ['TMPDIR']))
    home = base.isolate(root)
    args.shared_allowlist = False
    base.configure(home,route,args)
    report = dict(passed=False,cases=[],fixture_provenance='synthetic-authored-method-v1',
                  boundary='real models; authored prior-error ACKs and capture-only channel')
    result = 1
    try:
        asyncio.run(verify(args,root,route,home,report))
        secret = route['runtime']['api_key'].encode()
        report['no_credentials_written'] = all(secret not in path.read_bytes()
            for path in root.rglob('*') if path.is_file())
        report['passed'] = len(report['cases'])==7 and all(row['passed'] for row in report['cases'])
        result = 0 if report['passed'] and report['no_credentials_written'] else 1
    except Exception as exc:
        report['blocker_type'] = type(exc).__name__
        result = 2
    finally:
        path = root/'report.json'
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(dict(passed=report['passed'],report=str(path),blocker_type=report.get('blocker_type'))),flush=True)
    return result


if __name__=='__main__':
    raise SystemExit(main())
