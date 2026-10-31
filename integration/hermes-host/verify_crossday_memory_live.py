#!/usr/bin/env python3
"""真实模型跨天记忆捕获验收：合成好友/群/OneBot，从不连接真实 QQ。

显式 --live 才调用现有路由。owner-home 仅用于官方只读配置/OAuth解析；
不复制凭据、私人历史或身份。好友正文与分类走真实 ingress，群隔离探针只
验证 convert_event→真实服务 scope→同一真实无工具模型，非完整 native worker 验收。
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import copy
from datetime import datetime, timedelta, timezone
from importlib import import_module
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_napcat_social_live as shared
import verify_friend_dm_live as friend

sys.dont_write_bytecode = True
PREFERENCE = '合成琥珀纸卡工坊'
EXPERIENCE = '我们在合成工坊一起核对过银杉编号清单'
PLAN = '继续核对合成红枫纸卡清单'
EXPIRED = '合成青榆编织坊'


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='允许真实模型API；仍禁止真实QQ')
    parser.add_argument('--host', type=Path, required=True, help='当前Hermes源码')
    parser.add_argument('--owner-home', type=Path, required=True, help='仅只读模型配置/OAuth')
    parser.add_argument('--plugin-root', type=Path, help='待验收NapCat目录，默认当前源码')
    parser.add_argument('--social-plugin-root', type=Path)
    parser.add_argument('--life-plugin-root', type=Path)
    parser.add_argument('--managed', action='store_true', help=argparse.SUPPRESS)
    return parser.parse_args()


async def verify(args, root, home, route, report):
    from agent.plugin_llm import PluginLlm
    import agent.auxiliary_client as auxiliary
    from gateway.config import PlatformConfig
    from gateway import platform_registry
    from gateway.platform_registry import PlatformRegistry
    from gateway.session_context import get_session_env, set_session_vars
    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_manifest import PluginManifest

    plugin_root = (args.plugin_root or Path(__file__).resolve().parents[2]/'plugins/hermes-napcat').resolve(strict=True)
    social_root = (args.social_plugin_root or plugin_root.parent/'hermes-social').resolve(strict=True)
    life_root = (args.life_plugin_root or plugin_root.parent/'hermes-life-memory').resolve(strict=True)
    plugin, _ = shared.load_plugin(plugin_root, 'hermes_napcat_crossday_acceptance')
    social, _ = shared.load_plugin(social_root, 'hermes_social_crossday_acceptance')
    life, _ = shared.load_plugin(life_root, 'hermes_life_crossday_acceptance')
    hosted, transport = import_module(plugin.__name__+'.host_runtime'), import_module(plugin.__name__+'.transport')
    manager = PluginManager(scope_key=str(home))
    ctx = PluginContext(PluginManifest(name='hermes-napcat', kind='platform', source='user', path=str(plugin_root)), manager)
    social_ctx = PluginContext(PluginManifest(name='hermes-social', source='user', path=str(social_root)), manager)
    binding = hosted.HostBinding(ctx)
    async def deny_worker(payload):
        raise AssertionError('friend capture unexpectedly entered a native worker')
    bridge = hosted.HostIsolationBridge(binding, SimpleNamespace(request=deny_worker), gateway_tools=True)
    sink = friend.CaptureOneBot()
    trace = dict(facade=[], wire=[], sources=[], controls=[])
    report.update(trace=trace, source_roots=dict(napcat=str(plugin_root), social=str(social_root), life=str(life_root)))
    original_facade, original_wire = PluginLlm.acomplete, auxiliary._acreate_with_progress

    async def facade(self, messages, **kwargs):
        assert self._async_caller is None and self._sync_caller is None, 'model fixture replaced real SDK'
        visible = json.dumps(messages, ensure_ascii=False)
        assert shared.CANARY not in visible and friend.REMARK_CANARY not in visible
        row = dict(purpose=kwargs.get('purpose'), messages=copy.deepcopy(messages), completed=False,
            chat_id=get_session_env('HERMES_SESSION_CHAT_ID'), user_id=get_session_env('HERMES_SESSION_USER_ID'),
            message_id=get_session_env('HERMES_SESSION_MESSAGE_ID'))
        trace['facade'].append(row)
        started = time.monotonic()
        with auxiliary.scoped_runtime_main(route['runtime']):
            result = await original_facade(self, messages=messages, **kwargs)
        row.update(completed=True, response=result.text, provider=result.provider, model=result.model,
                   seconds=round(time.monotonic()-started, 3))
        return result

    async def wire(client, kwargs, *args, **options):
        assert not kwargs.get('tools'), 'memory response exposed tools'
        assert shared.CANARY not in json.dumps(kwargs, ensure_ascii=False, default=str)
        trace['wire'].append(dict(model=kwargs.get('model'), tools=kwargs.get('tools') or [],
                                 messages=copy.deepcopy(kwargs.get('messages', []))))
        return await original_wire(client, kwargs, *args, **options)

    async def deny_connection(*args, **kwargs):
        report['real_onebot_connection_attempts'] += 1
        raise AssertionError('real QQ connections prohibited')

    service, adapter = None, None
    config = dict(enabled=True, instance_id='crossday-capture', domain_id='crossday-people', policy_revision=1,
        groups=['20001'], friend_dm=True, explicit_memory={'enabled': True, 'ttl_days': 30},
        conversation={'enabled': False}, candidates={'enabled': False}, episodes={'enabled': False},
        agreements={'enabled': True, 'proactive': False})
    with ExitStack() as stack:
        stack.enter_context(patch.object(platform_registry, 'platform_registry', PlatformRegistry()))
        stack.enter_context(patch('hermes_cli.plugins.get_plugin_manager', return_value=manager))
        stack.enter_context(patch.object(transport.OneBotClient, 'start', deny_connection))
        stack.enter_context(patch.object(PluginLlm, 'acomplete', facade))
        stack.enter_context(patch.object(auxiliary, '_acreate_with_progress', wire))
        stack.enter_context(patch.object(auxiliary, '_resolve_codex_credential_and_base',
            return_value=(route['runtime']['api_key'], route['runtime']['base_url'])))
        plugin.register(ctx)
        cfg = PlatformConfig(enabled=True, extra=dict(self_id='90001', owner_id='10001', allowed_users=['10001'],
            allowed_groups=['20001'], require_mention=True, group_admission='all_members', group_trigger={'mode':'mention'},
            friend_dm={'enabled':True, 'admission':'explicit', 'users':['77777','88888']},
            isolation={'mode':'strict','private_profile':'default','social_profile':'social','domain_id':'crossday-public',
                       'policy_revision':1,'guest_dm_users':[]},
            runtime={'mode':'social_agent','contract':5,'social_home':str(root/'unused-worker')}))
        adapter = hosted.HostedNapCatAdapter(cfg, binding, bridge)
        adapter.client = sink
        controller = social.create_controller(social_ctx, adapter.participation_port(), dict(enabled=True, targets=[],
            quiet_hours=[0,0], public_self={'name':'合成跨天实例'}, dialogue={'enabled':True,'proactive':False}))
        adapter.install_participation(controller)
        original_queue = adapter._queue_friend_dm
        def queue(event, raw):
            source = adapter.settings.isolation.resolve(event.source)
            trace['sources'].append(dict(message_id=event.message_id, source_message_id=event.source.message_id,
                chat_id=source.chat_id, user_id=source.user_id, source_actor=event.source.user_id))
            return original_queue(event, raw)
        stack.enter_context(patch.object(adapter, '_queue_friend_dm', queue))
        async def no_native(event):
            raise AssertionError('friend entered owner/native agent')
        stack.enter_context(patch.object(adapter, 'handle_message', no_native))

        def install_memory():
            current = life.service.LifeMemoryService(home, home/'crossday-memory', config, lambda: config,
                channel_domain=adapter.settings.isolation.domain_id, channel_revision=adapter.settings.isolation.revision,
                dm_allowed=adapter.public_dm_allowed)
            adapter.install_social_memory(current, registration_key=('hermes-life-memory','crossday-capture'))
            report['source_roots']['scoped_memory'] = str(Path(life.core.memory_api().__file__).resolve().parent)
            return current

        mid = 100
        async def feed(text, user=77777):
            nonlocal mid
            mid += 1
            before = len(sink.calls)
            await adapter._receive(friend.raw_message(text, mid, user))
            async with asyncio.timeout(180):
                while adapter._friend_jobs:
                    await asyncio.gather(*tuple(adapter._friend_jobs))
            sends = [row['payload'] for row in sink.calls[before:] if row['action']=='send_private_msg']
            return '\n'.join(shared.plain_text(row['message']) for row in sends), sends

        async def authorize(command):
            before = len(trace['facade'])
            response, _ = await feed(command)
            match = re.search(r'review ([a-f0-9]{32})', response)
            assert match is not None, 'explicit control failed to propose candidate'
            ident = match[1]
            review, _ = await feed('/social-memory review '+ident)
            confirmation = review.split('确认：')[1].split('\n')[0]
            confirmed, _ = await feed(confirmation)
            assert '已确认' in confirmed and len(trace['facade']) == before, 'control invoked model or failed confirmation'
            trace['controls'].append(dict(command=command, candidate_id=ident, review=review,
                                          confirmed=confirmed, extraction='local exact rule or explicit command; no LLM'))
            return ident

        def save_case(name, text, output, before, sends, *, absent=(), required=(), kind='friend_ingress'):
            delta = trace['facade'][before[0]:]
            body = [row for row in delta if row['purpose'] in {'napcat_friend_dm','napcat_crossday_group_probe'}]
            errors = []
            visible = json.dumps([row['messages'] for row in delta], ensure_ascii=False)
            if len(body)!=1 or not body[0]['completed'] or len(output.strip())<3:
                errors.append('real body missing or did not complete')
            for marker in absent:
                if marker in visible or marker in output:
                    errors.append('out-of-scope or revoked marker appeared: '+marker)
            for marker in required:
                if marker not in visible or marker not in output:
                    errors.append('confirmed scoped memory not used: '+marker)
            if any(row.get('provider') != route['model']['provider'] or row.get('model') != route['model']['default'] for row in delta):
                errors.append('model route mismatch')
            if len(trace['wire']) <= before[1]:
                errors.append('real provider wire not observed')
            if kind=='friend_ingress':
                sources = trace['sources'][before[2]:]
                if len(sends)!=1 or len(sources)!=1 or sources[0]['source_message_id']!=str(mid):
                    errors.append('exact friend ingress/captured ACK missing')
                if body and sources and body[0]['chat_id'] != sources[0]['chat_id']:
                    errors.append('body route did not match authenticated source')
            case = dict(name=name, boundary=kind, input=text, output=output, sends=len(sends),
                body_calls=len(body), classifier_calls=sum(row['purpose']=='social_participation' for row in delta),
                actual_model_api_calls=len(trace['wire'])-before[1], passed=not errors, errors=errors)
            report['cases'].append(case)
            friend.write_report(root, report)
            print(json.dumps(case, ensure_ascii=False), flush=True)

        try:
            service = install_memory()
            interest = await authorize('请记住：我喜欢'+PREFERENCE)
            experience = await authorize('/social-memory episode '+EXPERIENCE)
            due = (datetime.now(timezone.utc)+timedelta(days=3)).replace(microsecond=0).isoformat()
            plan = await authorize('/social-memory plan '+due+' '+PLAN)
            with service.store.connection(True) as db:
                db.execute('UPDATE sources SET created=created-?',(3*86400,))
                db.execute('UPDATE memories SET created=created-?',(3*86400,))
                db.execute('UPDATE life_confirmations SET confirmed_at=confirmed_at-?',(3*86400,))
            service.close()
            adapter._friend_history.clear()
            service = install_memory()
            report.update(service_reconstructed=True, synthetic_evidence_age_days=3,
                          friend_ram_rows_after_reset=sum(map(len, adapter._friend_history.values())))
            group_raw = dict(post_type='message', self_id=90001, user_id=77777, message_id=8001,
                message_type='group', group_id=20001, sender={'user_id':77777,'card':'合成同名好友'},
                message=[{'type':'at','data':{'qq':'90001'}},{'type':'text','data':{'text':'只根据这个群里本人确认的资料，我有什么偏好、经历或下次约定？没有就直说。'}}])
            group_event = adapter.convert_event(group_raw)
            assert group_event is not None
            group_route = adapter.settings.isolation.resolve(group_event.source)
            group_view = service.recall(group_route, include_conversation=False)
            assert group_view.facts == () and group_view.relations == ()
            for name, user, text, required, absent in (
                ('a_confirmed_preference_after_reconstruction',77777,'我确认过的兴趣是什么？只用现有资料简短回答。',(PREFERENCE,),()),
                ('a_confirmed_experience_and_plan',77777,'我确认过哪段共同经历，下次约定做什么？请复述资料中的具体事项，不要代他人确认。',('银杉编号清单','红枫纸卡清单'),()),
                ('b_same_name_isolation',88888,'只根据这个私聊中我本人确认的资料，我有什么偏好、经历或下次约定？没有就直说。',(),(PREFERENCE,EXPERIENCE,PLAN)),
            ):
                before=(len(trace['facade']),len(trace['wire']),len(trace['sources']))
                output,sends=await feed(text,user)
                save_case(name,text,output,before,sends,required=required,absent=absent)
            before=(len(trace['facade']),len(trace['wire']),len(trace['sources']))
            set_session_vars(platform='napcat',source='napcat',chat_id=group_route.chat_id,chat_type='group',
                user_id=group_route.user_id,message_id=group_route.message_id,async_delivery=False,
                session_history_delivery='',cwd='')
            group_prompt = ('这是同一公开成员在群聊中的资料隔离探针。只使用本群本人确认资料；无资料时明确说明，不能借私聊、别人的资料或猜测。'
                            +json.dumps(dict(facts=group_view.facts,relations=group_view.relations),ensure_ascii=False))
            answer=await ctx.llm.acomplete(messages=[{'role':'system','content':group_prompt},
                {'role':'user','content':group_event.text}],max_tokens=256,timeout=45,purpose='napcat_crossday_group_probe')
            save_case('same_member_group_isolation',group_event.text,answer.text,before,[],
                      absent=(PREFERENCE,EXPERIENCE,PLAN),kind='convert_event_service_real_llm_probe')
            for ident in (interest,experience,plan):
                output,_=await feed('/social-memory forget '+ident)
                assert '已忘记' in output
            before=(len(trace['facade']),len(trace['wire']),len(trace['sources']))
            text='已忘记的旧资料请勿再用。现在还记得我确认过什么兴趣、经历或约定吗？没有有效资料就直说，不要猜。'
            output,sends=await feed(text)
            save_case('a_forget_removes_old_bot_fallback',text,output,before,sends,absent=(PREFERENCE,EXPERIENCE,PLAN))
            expired=await authorize('请记住：我喜欢'+EXPIRED)
            with service.store.connection(True) as db:
                db.execute('UPDATE memories SET expires=? WHERE id=?',(time.time()-1,expired))
            before=(len(trace['facade']),len(trace['wire']),len(trace['sources']))
            text='我的旧兴趣资料现在还有效吗？只说仍有效的确认资料，没有就直说。'
            output,sends=await feed(text)
            save_case('a_expired_fact_not_recalled',text,output,before,sends,absent=(PREFERENCE,EXPIRED))
            with service.store.connection() as db:
                report['retained_plaintext_sources_after_forget_and_expiry']=db.execute('SELECT COUNT(*) FROM sources').fetchone()[0]
                report['reply_plaintext_source_count']=db.execute("SELECT COUNT(*) FROM sources WHERE assistant_text!=''").fetchone()[0]
            report['actual_model_api_calls']=len(trace['wire'])
            report['passed']=all(case['passed'] for case in report['cases']) and report['reply_plaintext_source_count']==0
            report['no_credentials_written']=all(route['runtime']['api_key'].encode() not in path.read_bytes()
                for path in root.rglob('*') if path.is_file())
            assert report['no_credentials_written']
        finally:
            report['onebot_actions']=sink.calls
            report['admission_counts']=dict(adapter.admission_counts)
            if service is not None: service.close()
            await adapter.disconnect()
            binding.close()
            manager.unload()
            from gateway.status import flush_runtime_status
            flush_runtime_status(timeout=2)


def main():
    args=arguments()
    if not args.live:
        raise SystemExit('必须显式 --live；始终禁止连接真实QQ。')
    args.host,args.owner_home=args.host.resolve(strict=True),args.owner_home.resolve(strict=True)
    for field in ('plugin_root','social_plugin_root','life_plugin_root'):
        if getattr(args,field) is not None: setattr(args,field,getattr(args,field).resolve(strict=True))
    if not args.managed:
        sys.path.insert(0,str(args.host))
        from hermes_cli._launchers import runtime_command
        code='import runpy; runpy.run_path('+repr(str(Path(__file__).resolve()))+",run_name='__main__')"
        raise SystemExit(subprocess.call(runtime_command(args.host,args=[*sys.argv[1:],'--managed'],code=code)))
    os.umask(0o077)
    root=Path(tempfile.mkdtemp(prefix='napcat-crossday-live-',dir=os.environ.get('TMPDIR','/tmp')))
    report=dict(passed=False,cases=[],artifact_root=str(root),host_root=str(args.host),real_model=True,
        capture_transport_only=True,real_onebot_connection_attempts=0,qq_client_acceptance=False,
        group_native_worker_acceptance=False,raw_conversation_collection_enabled=False,
        extraction='exact local natural rule/manual commands; no candidate model calls',
        credential_mode='official readonly resolver; scoped_runtime_main and auxiliary credential seam in memory only')
    print('ARTIFACT_ROOT='+str(root),flush=True)
    try:
        home=shared.isolate(root)
        route=shared.read_route(args)
        report.update(configured_provider=route['model']['provider'],configured_model=route['model']['default'],
                      credential_source=route['credential_source'])
        friend.configure(home,route)
        asyncio.run(verify(args,root,home,route,report))
        status=0 if report['passed'] else 1
    except Exception as exc:
        report['blocker_type']=type(exc).__name__
        report['blocker_location']=[(frame.filename,frame.lineno,frame.name)
                                  for frame in __import__('traceback').extract_tb(exc.__traceback__)]
        print('BLOCKER_TYPE='+type(exc).__name__,flush=True)
        status=2
    finally:
        friend.write_report(root,report)
        print('REPORT='+str(root/'report.json'),flush=True)
    return status


if __name__=='__main__':
    raise SystemExit(main())
