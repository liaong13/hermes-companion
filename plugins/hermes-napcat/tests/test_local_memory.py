"""Matched local-history host, authenticated ingress and captured OneBot ACKs."""
import unittest
from unittest.mock import patch

import test_life_memory as shared
from gateway.run_turn_runner import TurnRunner
from hermes_napcat.adapter import Settings
from hermes_napcat.isolation import AdmissionDenied


@unittest.skipUnless(getattr(TurnRunner, 'public_memory_context_contract', None) == 1,
                     'requires napcat-local-memory-context.patch in an isolated host')
class LocalMemoryIngress(unittest.IsolatedAsyncioTestCase):
    raw = shared.PersonIngress.raw
    command = shared.PersonIngress.command
    remember = shared.PersonIngress.remember
    gateway_input = shared.PersonIngress.gateway_input

    async def asyncSetUp(self):
        await shared.PersonIngress.asyncSetUp(self)
        self.adapter.uninstall_social_memory(self.service)
        config = shared.base.social_config()
        config.extra['local_conversation_context'] = True
        self.adapter.settings = Settings(config.extra)
        self.config.update(conversation={'enabled':True}, candidates={'enabled':True})
        shared.life.register(self.context)
        self.service = self.factories[-1](None, self.adapter)
        call = self.adapter.client.call
        self.outgoing = 1000
        async def acknowledged(action, payload):
            result = await call(action, payload)
            if action.startswith('send_'):
                self.outgoing += 1
                result = {'message_id':self.outgoing}
                self.echo.update(message_id=self.outgoing, sender={'user_id':90001})
            return result
        self.adapter.client.call = acknowledged

    async def asyncTearDown(self):
        await shared.PersonIngress.asyncTearDown(self)

    async def test_original_natural_statement_is_pending_and_other_people_cannot_confirm(self):
        event = await self.gateway_input('公开名字，我喜欢天文学')
        pending = self.service.people.list(self.service.scope(self.adapter.settings.isolation.resolve(event.source)), pending=True)
        self.assertEqual(len(pending), 1)
        self.assertNotIn('兴趣：天文学', event.test_person_prompt)
        review = await self.command('/social-memory review '+pending[0]['id'])
        confirmation = review.split('确认：')[1].split('\n')[0]
        self.assertIn('无法处理', await self.command(confirmation, user=88888))
        self.assertIn('已确认', await self.command(confirmation))
        self.assertIn('兴趣：天文学', (await self.gateway_input()).test_person_prompt)
        self.assertFalse(self.calls)

    async def test_forget_removes_derived_recent_reply_and_blocks_bot_quote(self):
        ident, confirmation = await self.remember('天文学')
        await self.command(confirmation)
        event = await self.gateway_input('公开名字，天文学里恒星是什么？', process=True)
        sent = await self.adapter.send('group:20001', '你喜欢天文学，我们聊聊恒星。', reply_to=event.message_id)
        self.assertTrue(sent.success)
        quote = dict(self.echo)
        self.assertTrue(self.social.context('group:20001'))
        await self.command('/social-memory forget '+ident)
        self.assertIn(sent.message_id, self.service.suppressed_messages('group:20001'))
        self.assertFalse(self.social.context('group:20001'))
        with self.assertRaises(AdmissionDenied): self.adapter._check_reply_target('group:20001')
        await self.adapter.on_processing_complete(event, None)
        raw = self.raw('公开名字，继续')
        raw['message'].insert(0, {'type':'reply','data':{'id':sent.message_id}})
        async def get_quote(action, payload): return quote
        with patch.object(self.adapter.client, 'call', side_effect=get_quote):
            self.assertFalse(await self.adapter._verified_self_reply(raw))

    async def test_unload_during_transport_ack_stores_no_plaintext_and_keeps_history_policy(self):
        event = await self.gateway_input('公开名字，恒星是什么？', process=True)
        async def late_ack(action, payload):
            self.adapter.uninstall_social_memory(self.service)
            return {'message_id':2000}
        with patch.object(self.adapter.client, 'call', side_effect=late_ack):
            sent = await self.adapter.send('group:20001', 'UNLOADED_REPLY_CANARY', reply_to=event.message_id)
        self.assertTrue(sent.success)  # External ACK cannot be undone.
        self.assertIn('2000', self.service.suppressed_messages('group:20001'))
        with self.service.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sources WHERE assistant_text='UNLOADED_REPLY_CANARY'").fetchone()[0], 0)
        await self.adapter.on_processing_complete(event, None)
        fresh = await self.gateway_input(process=True)
        self.assertEqual(self.adapter.public_memory_history_policy(fresh.source), {'mode':'local_events','contract':1})
        await self.adapter.on_processing_complete(fresh, None)

    async def test_local_context_requires_matching_host_and_channel_opt_in(self):
        with patch.object(TurnRunner, 'public_memory_context_contract', None):
            self.assertFalse(self.adapter._public_group_ready())
            with self.assertRaises(AdmissionDenied):
                self.adapter.install_social_memory(self.service, registration_key=('test','test'))
        self.adapter.settings.local_conversation_context = False
        with self.assertRaises(AdmissionDenied):
            self.adapter.install_social_memory(self.service, registration_key=('test','test'))

    async def test_control_quotes_are_excluded_and_revocation_replay_is_idempotent(self):
        await self.remember('天文学')
        mid = str(self.echo['message_id'])
        raw = self.raw('公开名字，看看这条')
        raw['message'].insert(0, {'type':'reply', 'data':{'id':mid}})
        self.assertFalse(await self.adapter._verified_self_reply(raw))
        state = self.social.states['group:20001']
        generation = state.generation
        self.adapter._apply_memory_revocations(self.service, 'group:20001')
        self.adapter._apply_memory_revocations(self.service, 'group:20001')
        self.assertEqual(state.generation, generation)

    async def test_storage_failure_after_ack_is_not_an_unknown_send_or_replayed_context(self):
        event = await self.gateway_input('公开名字，恒星是什么？', process=True)
        with patch.object(self.service, 'record_reply', side_effect=OSError('synthetic storage failure')):
            result = await self.adapter.send('group:20001', 'ACKED_BUT_UNRECORDED', reply_to=event.message_id)
        self.assertTrue(result.success)
        self.assertFalse(self.adapter._public_group_ready())
        self.assertFalse(self.social.context('group:20001'))
        with self.assertRaises(AdmissionDenied): self.adapter.public_memory_history_policy(event.source)
        with self.assertRaises(AdmissionDenied): self.adapter._check_reply_target('group:20001')
        await self.adapter.on_processing_complete(event, None)
