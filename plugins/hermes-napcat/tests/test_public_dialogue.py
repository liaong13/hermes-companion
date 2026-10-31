"""Authored synthetic events and captured channel capabilities; no chat imports or QQ I/O."""
import copy
import unittest
from unittest.mock import patch

import test_proactive_lifecycle as base
from hermes_napcat.isolation import AdmissionDenied
from social_plugin_helpers import optional_valid


class PublicDialogue(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=base.Lifecycle.asyncSetUp
    asyncTearDown=base.Lifecycle.asyncTearDown
    issue_ingress=base.Lifecycle.issue_ingress
    start_group=base.Lifecycle.start_group
    drain_ambient=base.Lifecycle.drain_ambient

    async def optional(self):
        await self.adapter._receive(base.raw_text('示例活动甲的准备步骤完成了吗？',501))
        await self.drain_ambient()
        inbound=self.dispatched[-1]
        await self.start_group(inbound)
        return inbound

    async def test_supplement_during_generation_keeps_same_source_and_can_send(self):
        inbound=await self.optional()
        issued=self.adapter._optional_sources.grants[id(inbound.source)]
        await self.adapter._receive(base.raw_text('也需要准备材料乙',502,user=77777))
        self.assertTrue(optional_valid(self.adapter,inbound.source))
        updated=self.adapter._optional_sources.grants[id(inbound.source)]
        self.assertEqual((updated[2],updated[5],updated[6]),(issued[2],issued[5],issued[6]))
        self.assertIs(updated[0],inbound.source)
        self.assertFalse(optional_valid(self.adapter,copy.copy(inbound.source)))
        self.assertTrue(self.adapter.admit_group_source(inbound.source))
        result=await self.adapter.send('group:20001','先核对合成活动甲需要的两种材料。',reply_to='501')
        self.assertTrue(result.success)
        await self.adapter.on_processing_complete(inbound,'completed')

    async def test_explicit_control_cancels_optional_but_never_grants_new_target(self):
        inbound=await self.optional()
        await self.adapter._receive(base.raw_text('/social pause',503,user=77777))
        self.assertFalse(optional_valid(self.adapter,inbound.source))
        result=await self.adapter.send('group:20001','迟到的回复',reply_to='501')
        self.assertFalse(result.success)
        self.assertEqual(result.raw_response['error_code'],'group_reply_target_denied')
        self.assertFalse(self.sent)
        await self.adapter.on_processing_complete(inbound,'completed')

    async def test_supplement_has_bounded_count_and_never_renews_deadline(self):
        inbound=await self.optional()
        deadline=self.adapter._optional_sources.grants[id(inbound.source)][-1]
        for n in range(8):
            await self.adapter._receive(base.raw_text('也需要准备材料乙',510+n,user=77777))
            self.assertTrue(optional_valid(self.adapter,inbound.source))
        self.assertEqual(self.adapter._optional_sources.grants[id(inbound.source)][-1],deadline)
        await self.adapter._receive(base.raw_text('也需要准备材料乙',520,user=77777))
        self.assertFalse(optional_valid(self.adapter,inbound.source))
        await self.adapter.on_processing_complete(inbound,'completed')

    async def test_elapsed_supplement_does_not_refresh_a_conversation(self):
        inbound=await self.optional()
        grant=self.adapter._optional_sources.grants[id(inbound.source)]
        with patch('hermes_napcat.participation.time.monotonic',return_value=grant[-1]-279):
            await self.adapter._receive(base.raw_text('也需要准备材料乙',521,user=77777))
            self.assertFalse(optional_valid(self.adapter,inbound.source))
        await self.adapter.on_processing_complete(inbound,'completed')

    async def test_named_turn_gets_other_members_public_context_and_recall_revokes_it(self):
        await self.adapter._receive(base.raw_text('合成活动甲使用材料乙，材料已编号',530,user=77777))
        incoming=self.adapter.convert_event(base.raw_text('公开名字，请核对这份合成材料表',531))
        self.social.observe(incoming)
        self.assertEqual(incoming.message_id,'531')
        await self.start_group(incoming)
        prompt=self.adapter.conversation_prompt_for_source(incoming.source)
        self.assertIn('合成活动甲',prompt)
        self.assertIn('材料已编号',prompt)
        self.assertNotIn('请核对这份合成材料表',prompt)
        self.social.recall('group:20001','530')
        self.assertFalse(self.adapter.admit_group_source(incoming.source))
        with self.assertRaises(AdmissionDenied):
            self.adapter.conversation_prompt_for_source(incoming.source)
        await self.adapter.on_processing_complete(incoming,'completed')
