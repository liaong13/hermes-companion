"""Integration regressions for actual send, forget and feedback lifecycles."""
import asyncio
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from support import FakeContext, FakePort, build, event, policy
from hermes_social.activities import PublishedDocument
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


CHAT = 'group:20001'
URL = 'https://example.org/review'


class LifeReviewBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.home = Path(self.temp.name)
        self.token = set_hermes_home_override(self.home)
        self.ctx, self.port = FakeContext(), FakePort(self.home)
        self.social = build(self.ctx, self.port, policy(targets=[CHAT], initiative_mode='local',
            activities={'enabled': True, 'sources': {CHAT: [URL]}, 'proactive': True},
            learning={'enabled': True, 'auto_trials': True, 'min_feedback': 2, 'trial_seconds': 600}))

    async def asyncTearDown(self):
        self.ctx.release.set()
        await self.social.stop()
        self.ctx.unload()
        await asyncio.sleep(0)
        if self.social.binding.shutdown_task:
            await self.social.binding.shutdown_task
        reset_hermes_home_override(self.token)
        self.temp.cleanup()

    def observe(self, text, mid, user='10001'):
        item = event(text, mid=mid, user=user)
        item.reply_expected = False
        item.bot_id = '90001'
        self.social.observe(item)
        return item

    async def read_source(self):
        async def reader(url):
            return PublishedDocument(url, url, 'FORGOTTEN_SOURCE_CANARY', '合成公开资料', time.time())
        self.social.life.reader = AsyncMock(side_effect=reader)
        await self.social.life.advance()
        return self.social.life.activities.notes(CHAT)[0]

    def add_feedback(self, index, status='correction'):
        request = self.observe('需要回答的合成问题', 'request-'+str(index))
        mid = 'bot-'+str(index)
        self.social.record_self(CHAT, '合成的真实已核验出站回复', mid, reply_to_message_id=request.message_id)
        feedback = self.observe('/social feedback '+mid+' '+status, 'feedback-'+str(index), user='10002')
        self.assertIn('已记录', self.social.control(feedback))

    async def test_forget_source_revokes_derived_reply_even_without_feedback_learning(self):
        self.social.policy.learning['enabled'] = False
        note = await self.read_source()
        request = self.observe('请根据刚才的公开资料继续说', 'source-request')
        view = self.social.prepare_public_context(request)
        self.assertIn('FORGOTTEN_SOURCE_CANARY', view.text)
        self.social.record_self(CHAT, 'FORGOTTEN_SOURCE_CANARY 的派生回答', 'derived',
                                reply_to_message_id=request.message_id)
        command = self.observe('/social forget-source '+note['id'], 'forget-source')
        self.assertIn('已忘记', self.social.control(command))
        self.assertFalse(view.current())
        later = self.observe('现在换一个话题', 'later-request')
        self.assertNotIn('FORGOTTEN_SOURCE_CANARY', self.social.prepare_public_context(later).text)

    async def test_forgotten_background_revokes_replies_quoted_to_other_questions_and_their_derivatives(self):
        self.observe('FORGOTTEN_BACKGROUND_CANARY', 'background-a')
        for index in range(10):
            self.observe('无关的合成背景内容', 'filler-'+str(index))
        first = self.observe('问题 R1', 'question-r1')
        self.assertIn('FORGOTTEN_BACKGROUND_CANARY', self.social.prepare_public_context(first).text)
        self.social.record_self(CHAT, 'FORGOTTEN_BACKGROUND_CANARY 的第一层回答', 'derived-b',
                                reply_to_message_id=first.message_id)
        second = self.observe('问题 R2', 'question-r2')
        view = self.social.prepare_public_context(second)
        self.assertNotIn('"message_id": "background-a"', view.text)
        self.assertIn('FORGOTTEN_BACKGROUND_CANARY', view.text)
        self.social.record_self(CHAT, 'FORGOTTEN_BACKGROUND_CANARY 的第二层回答', 'derived-c',
                                reply_to_message_id=second.message_id)
        self.social.forget_messages(CHAT, ['background-a'])
        self.assertFalse(view.current())
        for recent in (self.social.recent, self.social.life.recent):
            ids = {row.message_id for row in recent.routing_history(self.social._scope(CHAT))}
            self.assertFalse({'background-a', 'derived-b', 'derived-c'}.intersection(ids))
        later = self.observe('换一个话题', 'later-question')
        self.assertNotIn('FORGOTTEN_BACKGROUND_CANARY', self.social.prepare_public_context(later).text)

    async def test_expired_verified_activity_becomes_real_no_response_feedback(self):
        await self.read_source()
        offer = self.social.life.next_opportunity(CHAT, frozenset())
        self.social.states[CHAT].last_activity = time.time()-4000
        self.assertTrue(await self.social.tick(CHAT))
        store = self.social.life.activities
        after_expiry = offer.expires_at+1
        store.clock = lambda: after_expiry
        with patch('hermes_social.contacts.time.time', return_value=after_expiry):
            self.social.due(CHAT)
        self.assertEqual(self.social.contacts.rows(CHAT)[0]['status'], 'no_response')
        self.assertEqual(len(store.feedback_evidence(CHAT)), 1)

    async def test_each_inflight_fragment_keeps_background_dependencies_until_completion(self):
        self.observe('FRAGMENT_BACKGROUND_CANARY','background')
        request = self.observe('请分段回答这份合成资料。','fragment-request')
        self.social.processing_start(request)
        view = self.social.prepare_public_context(request)
        with patch.object(self.port,'invalidate_optional') as invalidate:
            for index in range(2):
                self.social.record_self(CHAT,'合成回答的第'+str(index)+'段。','fragment-'+str(index),
                    reply_to_message_id=request.message_id,source=request.source)
            invalidate.assert_called_with(CHAT,preserve_source=request.source)
        key = (CHAT,request.message_id)
        self.assertIn('background',self.social.life.reply_dependencies[key])
        self.social.processing_complete(request)
        self.assertNotIn(key,self.social.life.reply_dependencies)
        self.social.forget_messages(CHAT,['background'])
        self.assertFalse(view.current())
        ids = {row.message_id for row in self.social.recent.routing_history(self.social._scope(CHAT))}
        self.assertFalse({'fragment-0','fragment-1'}.intersection(ids))

    async def test_forget_between_fragments_prevents_late_body_reentering_context(self):
        self.observe('FORGOTTEN_FRAGMENT_CANARY','background')
        request = self.observe('一份合成分段任务。','fragment-request')
        self.social.processing_start(request)
        self.social.prepare_public_context(request)
        with patch.object(self.port,'invalidate_optional'):
            self.social.record_self(CHAT,'已核验的第一段。','fragment-first',
                reply_to_message_id=request.message_id,source=request.source)
            self.social.forget_messages(CHAT,['background'])
            self.social.record_self(CHAT,'已经在网络中的第二段。','fragment-late',
                reply_to_message_id=request.message_id,source=request.source)
            self.social.record_self(CHAT,'另一个迟到片段。','fragment-later',
                reply_to_message_id=request.message_id,source=request.source)
        ids = {row.message_id for row in self.social.recent.routing_history(self.social._scope(CHAT))}
        self.assertFalse({'fragment-first','fragment-late','fragment-later'}.intersection(ids))
        self.social.processing_complete(request)
        self.assertNotIn((CHAT,request.message_id),self.social.life.revoked_replies)

    async def test_recalled_feedback_revokes_its_strategy_evidence_and_trial(self):
        self.add_feedback(0)
        self.add_feedback(1)
        strategy = self.social.life.strategy(CHAT)
        self.assertEqual(strategy['state'], 'trial')
        self.social.recall(CHAT, 'feedback-0')
        store = self.social.life.activities
        self.assertEqual(store.trial(CHAT, strategy['trial_id'])['state'], 'reverted')
        self.assertEqual(len(store.feedback_evidence(CHAT)), 1)
        self.assertIsNone(self.social.reply_char_limit(CHAT))

    async def test_new_explicit_counterexample_reverts_a_retained_strategy(self):
        self.add_feedback(0)
        self.add_feedback(1)
        trial_id = self.social.life.strategy(CHAT)['trial_id']
        self.add_feedback(2, 'positive')
        self.add_feedback(3, 'positive')
        self.assertEqual(self.social.life.strategy(CHAT)['state'], 'retained')
        self.add_feedback(4, 'negative')
        self.assertEqual(self.social.life.activities.trial(CHAT, trial_id)['state'], 'reverted')
        self.assertIsNone(self.social.reply_char_limit(CHAT))

    async def test_proactive_unfinished_topic_records_one_valid_unquoted_outbound(self):
        self.social.policy.dialogue['proactive'] = True
        dialogue = self.social.life.dialogue
        dialogue.clock = lambda: time.time()-2000
        question = self.observe('还有个合成问题没有回答', 'awaiting')
        dialogue.clock = time.time
        scope = self.social._scope(CHAT)
        dialogue.assess(scope, question.message_id, action='wait', reason='question', audience='group')
        self.social.states[CHAT].last_activity = time.time()-4000
        self.assertTrue(await self.social.tick(CHAT))
        self.assertEqual(len(self.port.attempts), 1)
        self.assertEqual(self.social.contacts.rows(CHAT)[0]['kind'], 'dialogue')
        self.assertEqual(self.social.contacts.rows(CHAT)[0]['status'], 'acknowledged')
        self.assertFalse(any(question.message_id in topic.awaiting_reply for topic in dialogue.context(scope)))
        outbound = self.port.last_receipt(CHAT)
        metadata = dialogue.reference_metadata(scope, (outbound,))
        self.assertEqual(len(metadata), 1)
        self.assertEqual(metadata[0]['reply_to'], '')
        self.assertFalse(await self.social.tick(CHAT))

    async def test_model_share_uses_exact_source_current_chat_and_fixed_origin(self):
        await self.read_source()
        self.observe('THIS_CHAT_CANARY', 'same-chat')
        other = event('OTHER_CHAT_CANARY', mid='different-chat', chat='group:20002')
        other.reply_expected = False
        self.social.observe(other)
        self.social.policy.initiative_mode = 'model'
        self.ctx.response = {'speak': True, 'text': '这份公开摘录提出了一个具体想法，大家觉得怎样？'}
        self.social.states[CHAT].last_activity = time.time()-4000
        self.assertTrue(await self.social.tick(CHAT))
        self.assertEqual(len(self.ctx.calls), 2)
        kwargs = self.ctx.calls[0][0]
        prompt = kwargs['messages'][-1]['content']
        self.assertIn('FORGOTTEN_SOURCE_CANARY', prompt)
        self.assertIn('THIS_CHAT_CANARY', prompt)
        self.assertNotIn('OTHER_CHAT_CANARY', prompt)
        self.assertNotIn('tools', kwargs)
        self.assertIn('social_source_grounding', self.ctx.calls[1][0]['messages'][-1]['content'])
        self.assertNotIn('THIS_CHAT_CANARY', self.ctx.calls[1][0]['messages'][-1]['content'])
        self.assertEqual(self.port.attempts[0][1], self.ctx.response['text']+'\n来源：'+URL)

    async def test_unrelated_draft_cannot_borrow_the_reading_source(self):
        await self.read_source()
        self.social.policy.initiative_mode = 'model'
        self.ctx.response = {'speak': True, 'text': '旧题的答案是九分。'}
        self.ctx.grounding_response = {'supported': False, 'about_source': False}
        self.social.states[CHAT].last_activity = time.time()-4000
        self.assertFalse(await self.social.tick(CHAT))
        self.assertEqual(len(self.ctx.calls), 2)
        self.assertFalse(self.port.attempts)
        self.assertEqual(self.social.contacts.rows(CHAT), [])

    async def test_model_can_silence_a_share_without_spending_contact_budget(self):
        await self.read_source()
        self.social.policy.initiative_mode = 'model'
        self.ctx.response = {'speak': False, 'text': ''}
        self.social.states[CHAT].last_activity = time.time()-4000
        self.assertFalse(await self.social.tick(CHAT))
        self.assertEqual(len(self.ctx.calls), 1)
        self.assertEqual(self.social.contacts.rows(CHAT), [])
        self.assertFalse(self.port.attempts)
        self.assertIsNotNone(self.social.life.next_opportunity(CHAT, frozenset()))

    async def test_model_cannot_replace_the_source_link(self):
        await self.read_source()
        self.social.policy.initiative_mode = 'model'
        self.ctx.response = {'speak': True, 'text': '请访问 https://attacker.example/other'}
        self.social.states[CHAT].last_activity = time.time()-4000
        with self.assertRaises(ValueError):
            await self.social.tick(CHAT)
        self.assertEqual(self.social.contacts.rows(CHAT), [])
        self.assertFalse(self.port.attempts)

    async def test_forget_source_during_model_review_discards_the_candidate(self):
        note = await self.read_source()
        self.social.policy.initiative_mode = 'model'
        self.ctx.delayed = True
        self.ctx.response = {'speak': True, 'text': '这是基于刚才公开摘录的分享。'}
        self.social.states[CHAT].last_activity = time.time()-4000
        task = asyncio.create_task(self.social.tick(CHAT))
        await asyncio.wait_for(self.ctx.entered.wait(), 2)
        command = self.observe('/social forget-source '+note['id'], 'forget-during-model')
        self.assertIn('已忘记', self.social.control(command))
        self.ctx.release.set()
        self.assertFalse(await asyncio.wait_for(task, 2))
        self.assertEqual(self.social.contacts.rows(CHAT), [])
        self.assertFalse(self.port.attempts)

    async def test_forget_source_during_transmission_discards_late_verified_body(self):
        note = await self.read_source()
        async def late_readback(grant, text, consume, valid):
            self.assertTrue(valid())
            consume()
            self.port.attempts.append((CHAT, text))
            command = self.observe('/social forget-source '+note['id'], 'forget-before-readback')
            self.assertIn('已忘记', self.social.control(command))
            self.port.receipts[CHAT] = 'late-delivery'
            self.social.record_self(CHAT, text, 'late-delivery')
            return True
        self.port.send_idle = late_readback
        self.social.states[CHAT].last_activity = time.time()-4000
        self.assertFalse(await self.social.tick(CHAT))
        self.assertEqual(len(self.port.attempts), 1)
        # The real forget command is ordinary incoming activity, never a reply
        # or a verified delivery acknowledgement for the in-flight send.
        contact = self.social.contacts.rows(CHAT)[0]
        self.assertIn(contact['status'], {'unknown', 'activity_observed'})
        self.assertEqual(contact['message_id'], '')
        for recent in (self.social.recent, self.social.life.recent):
            self.assertNotIn('late-delivery', [row.message_id for row in recent.routing_history(self.social._scope(CHAT))])
        later = self.observe('换一个话题', 'after-late-readback')
        self.assertNotIn('FORGOTTEN_SOURCE_CANARY', self.social.prepare_public_context(later).text)


if __name__ == '__main__':
    unittest.main()
