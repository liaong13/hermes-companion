"""Actual channel grants and local agreements, with captured QQ transport."""
import asyncio
from datetime import datetime, timezone
import re
import time
import unittest
from unittest.mock import patch

import test_local_memory as local
from gateway.run_turn_runner import TurnRunner


@unittest.skipUnless(getattr(TurnRunner,'public_memory_context_contract',None)==1,
                     'requires isolated local-memory host patch')
class LocalContacts(unittest.IsolatedAsyncioTestCase):
    raw=local.LocalMemoryIngress.raw
    command=local.LocalMemoryIngress.command
    gateway_input=local.LocalMemoryIngress.gateway_input

    async def asyncSetUp(self):
        await local.LocalMemoryIngress.asyncSetUp(self)
        self.adapter.uninstall_social_memory(self.service)
        self.config.update(episodes={'enabled':True},agreements={'enabled':True,'proactive':True})
        local.shared.life.register(self.context)
        self.service=self.factories[-1](None,self.adapter)
        self.social.policy.initiative_mode='local'
        original=self.adapter.client.call
        async def members(action,payload):
            if action=='get_group_member_info':
                self.sent.append((action,payload))
                return {'user_id':payload['user_id'],'group_id':payload['group_id']}
            return await original(action,payload)
        self.adapter.client.call=members

    async def asyncTearDown(self):
        await local.LocalMemoryIngress.asyncTearDown(self)

    async def due_plan(self):
        due=datetime.fromtimestamp(time.time()+600,timezone.utc).isoformat()
        reply=await self.command('/social-memory plan '+due+' 继续聊恒星')
        ident=re.search(r'review ([a-f0-9]{32})',reply)[1]
        review=await self.command('/social-memory review '+ident)
        await self.command(review.split('确认：')[1].split('\n')[0])
        self.social.states['group:20001'].last_activity=time.time()-4000
        self.social.states['group:20001'].last_check=0
        self.sent.clear()
        return ident,time.time()+601

    async def test_due_contact_checks_membership_mentions_exact_member_and_records_ack(self):
        ident,now=await self.due_plan()
        with patch('time.time',return_value=now):
            self.assertTrue(await self.social.tick('group:20001'))
            self.assertIsNone(self.service.contact_opportunity('group:20001'))
        self.assertEqual([a for a,p in self.sent],['get_group_member_info','send_group_msg','get_msg'])
        segments=self.sent[1][1]['message']
        self.assertEqual(segments[0],{'type':'at','data':{'qq':'77777'}})
        self.assertFalse(self.calls)
        row=self.social.contacts.rows('group:20001')[0]
        self.assertEqual(row['status'],'acknowledged')
        self.assertEqual(row['message_id'],str(self.echo['message_id']))
        scope=self.service.scope(self.adapter.settings.isolation.resolve(self.adapter.convert_event(
            self.raw('公开名字，继续')).source))
        with self.service.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM life_events WHERE kind='contact' AND source_id IS NOT NULL").fetchone()[0],1)
        self.assertFalse(self.social.states['group:20001'].pending)

    async def test_cancel_while_membership_check_is_pending_prevents_send_and_consumption(self):
        ident,now=await self.due_plan()
        original=self.adapter.client.call
        async def cancel(action,payload):
            if action=='get_group_member_info':
                raw=self.raw('/social-memory cancel '+ident)
                route=self.adapter.settings.isolation.resolve(self.adapter.convert_event(raw).source)
                self.service.command(route,'/social-memory cancel '+ident)
            return await original(action,payload)
        with patch('time.time',return_value=now),patch.object(self.adapter.client,'call',side_effect=cancel):
            self.assertFalse(await self.social.tick('group:20001'))
        self.assertFalse([a for a,p in self.sent if a.startswith('send_')])
        self.assertEqual(self.social.contacts.rows('group:20001'),[])

    async def test_wrong_member_response_prevents_contact(self):
        _,now=await self.due_plan()
        async def wrong(action,payload): return {'user_id':88888,'group_id':20001}
        with patch('time.time',return_value=now),patch.object(self.adapter.client,'call',side_effect=wrong):
            self.assertFalse(await self.social.tick('group:20001'))
        self.assertFalse(self.social.contacts.rows('group:20001'))
        self.assertFalse(self.calls)

    async def test_unknown_send_is_one_shot_and_does_not_claim_delivery(self):
        _,now=await self.due_plan()
        self.fail_send=True
        with patch('time.time',return_value=now):
            self.assertFalse(await self.social.tick('group:20001'))
            self.assertIsNone(self.service.contact_opportunity('group:20001'))
        self.assertEqual(self.social.contacts.rows('group:20001')[0]['status'],'unknown')
        self.assertFalse(self.calls)

    async def test_late_unload_ack_is_truthful_and_stores_only_a_tombstone(self):
        _,now=await self.due_plan()
        original=self.adapter.client.call
        async def unload(action,payload):
            result=await original(action,payload)
            if action=='send_group_msg': self.adapter.uninstall_social_memory(self.service)
            return result
        with patch('time.time',return_value=now),patch.object(self.adapter.client,'call',side_effect=unload):
            self.assertTrue(await self.social.tick('group:20001'))
        mid=str(self.echo['message_id'])
        self.assertIn(mid,self.service.suppressed_messages('group:20001'))
        self.assertEqual(self.social.contacts.rows('group:20001')[0]['status'],'acknowledged')
        with self.service.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sources WHERE assistant_text LIKE '之前约好%'").fetchone()[0],0)

    async def test_cancel_command_updates_shared_journal_and_retires_sent_derivative(self):
        ident,now=await self.due_plan()
        with patch('time.time',return_value=now):
            self.assertTrue(await self.social.tick('group:20001'))
        mid=str(self.echo['message_id'])
        self.assertIn('已结束约定',await self.command('/social-memory cancel '+ident))
        self.assertEqual(self.social.contacts.rows('group:20001')[0]['status'],'cancelled')
        self.assertFalse(self.social.states['group:20001'].unanswered)
        self.assertIn(mid,self.service.suppressed_messages('group:20001'))

    async def test_wrong_at_readback_is_unknown_and_never_replayed(self):
        _,now=await self.due_plan()
        original=self.adapter.client.call
        async def changed(action,payload):
            result=await original(action,payload)
            if action=='get_msg':
                result={**result,'message':[{'type':'at','data':{'qq':'88888'}},result['message'][-1]]}
            return result
        with patch('time.time',return_value=now),patch.object(self.adapter.client,'call',side_effect=changed):
            self.assertFalse(await self.social.tick('group:20001'))
            self.assertIsNone(self.service.contact_opportunity('group:20001'))
        self.assertEqual(self.social.contacts.rows('group:20001')[0]['status'],'unknown')
        self.assertIn(str(self.echo['message_id']),self.service.suppressed_messages('group:20001'))
