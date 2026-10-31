"""Local opportunities and model idle share durable budgets and actual feedback."""
from pathlib import Path
import tempfile
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import support
from support import FakeContext, FakePort, build, event, policy
from hermes_social.contacts import ContactLedger
from hermes_social.controller import SocialController, ChatState
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


class ContactTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.home=Path(self.temp.name)
        self.token=set_hermes_home_override(self.home)
        self.ctx,self.port=FakeContext(),FakePort(self.home)
        self.social=build(self.ctx,self.port,policy(initiative_mode='local'))
        self.idle()

    async def asyncTearDown(self):
        await self.social.stop()
        self.ctx.unload()
        reset_hermes_home_override(self.token)
        self.temp.cleanup()

    def idle(self):
        state=self.social.states['group:20001']
        state.last_activity=time.time()-4000
        state.last_check=0

    def offer(self):
        status=['planned']
        def claim(attempt): status[0]='attempting'
        def finish(result): status[0]=result
        self.port.offer=SimpleNamespace(reference='agreement-proof',recipient='77777',text='到约定时间了。',
            expires_at=time.time()+86400,current=lambda:status[0] in {'planned','attempting'},claim=claim,finish=finish)
        return status

    async def test_local_empty_or_due_never_calls_model(self):
        self.assertFalse(await self.social.tick('group:20001'))
        status=self.offer()
        self.idle()
        self.assertTrue(await self.social.tick('group:20001'))
        self.assertEqual(status,['notified'])
        self.assertFalse(self.ctx.calls)
        row=self.social.contacts.rows('group:20001')[0]
        self.assertEqual((row['kind'],row['status'],row['recipient']),('agreement','acknowledged','77777'))
        self.assertEqual(self.port.prepared[-1][2],[])

    async def test_unknown_is_consumed_across_restart_and_only_recipient_clears_it(self):
        self.offer()
        self.port.ambiguous=True
        self.assertFalse(await self.social.tick('group:20001'))
        self.assertEqual(self.social.contacts.rows('group:20001')[0]['status'],'unknown')
        replacement=SocialController(self.port,self.social.policy,self.social.binding)
        self.assertTrue(replacement.states['group:20001'].unanswered)
        self.social.observe(event('别人的消息',mid='2',user='88888'))
        self.assertTrue(self.social.states['group:20001'].unanswered)
        self.social.observe(event('本人的普通消息',mid='3',user='77777'))
        self.assertFalse(self.social.states['group:20001'].unanswered)
        self.assertEqual(self.port.feedback[-1][-1],'activity_observed')
        self.assertTrue(self.social.contacts.contains('agreement-proof'))

    async def test_real_reply_decline_and_control_cannot_be_forged_by_other_person(self):
        self.offer()
        await self.social.tick('group:20001')
        self.social.contact_control_feedback('group:20001','88888','2',[('agreement-proof','cancelled')])
        self.assertTrue(self.social.states['group:20001'].unanswered)
        reply=event('不用提醒了',mid='3',user='77777')
        reply.reply_to_message_id=self.port.last_receipt('group:20001')
        self.social.observe(reply)
        self.assertEqual(self.port.feedback[-1][-1],'declined')
        self.assertFalse(self.social.states['group:20001'].unanswered)

    async def test_local_opportunity_obeys_quiet_busy_and_shared_model_budget(self):
        self.offer()
        with patch.object(self.social.policy,'quiet',return_value=True):
            self.assertFalse(await self.social.tick('group:20001'))
        self.social.busy['group:20001']=1
        self.assertFalse(await self.social.tick('group:20001'))
        self.social.busy.clear()
        self.social.policy.daily_limit=1
        self.social.policy.initiative_mode='model'
        self.port.offer=None
        self.assertTrue(await self.social.tick('group:20001'))
        self.social.observe(event('实际活动',mid='4'))
        self.offer()
        self.idle()
        self.assertFalse(await self.social.tick('group:20001'))
        self.assertEqual(len(self.port.attempts),1)

    async def test_json_rollback_does_not_restore_budget_and_db_failure_pauses(self):
        self.offer()
        await self.social.tick('group:20001')
        self.social.path.unlink()
        replacement=SocialController(self.port,self.social.policy,self.social.binding)
        self.assertEqual(replacement.states['group:20001'].daily,1)
        with patch.object(self.social.contacts,'restore',side_effect=ValueError('corrupt ledger')):
            self.assertFalse(self.social.due('group:20001'))
        self.assertFalse(self.social.healthy)

    async def test_transaction_serializes_consumption_and_expiry_is_not_reply(self):
        ledger=self.social.contacts
        other=ContactLedger(ledger.path)
        kwargs=dict(kind='agreement',reference='one',recipient='77777',expires=time.time()-1,
                    day=self.social._day(time.time()),limit=1,interval=300)
        ledger.reserve('group:20001','first',state=ChatState(),**kwargs)
        with self.assertRaises(ValueError): other.reserve('group:20001','second',state=ChatState(),**kwargs)
        ledger.finish('first','900')
        ledger.expire('group:20001')
        self.assertEqual(ledger.rows('group:20001')[0]['status'],'no_response')
        states={'group:20001':ChatState()}
        other.restore(states)
        self.assertTrue(states['group:20001'].unanswered)

    async def test_receipt_storage_failure_preserves_actual_send_and_pauses(self):
        self.offer()
        with patch.object(self.social.contacts,'finish',side_effect=ValueError('unavailable')):
            self.assertTrue(await self.social.tick('group:20001'))
        self.assertFalse(self.social.healthy)
        self.assertEqual(self.social.counters['sent'],1)
        self.assertTrue(self.social.contacts.contains('agreement-proof'))
