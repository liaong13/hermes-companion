"""Real controller/life/store wiring with authored fixtures and capture-only ports."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from support import FakeContext, FakePort, build, event, policy
from hermes_social.activities import PublishedDocument
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


CHAT = 'group:20001'
OTHER = 'group:20002'
DM = 'dm:10002'
URL = 'https://example.org/one'
SECOND_URL = 'https://example.org/two'
DM_URL = 'https://example.org/dm'


class CapturedActivityPort(FakePort):
    """Shared capture-only port with a fixture's two explicitly admitted friends."""

    def dm_target_allowed(self, chat):
        return chat in {'dm:10001', DM}

    def dm_allowed(self, chat):
        return self.dm_target_allowed(chat)


class LifeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.home = Path(self.temp.name)
        self.token = set_hermes_home_override(self.home)
        self.instances = []
        self.config = policy(targets=[CHAT, OTHER, 'dm:10001', DM], initiative_mode='local',
            activities={'enabled': True, 'sources': {CHAT: [URL]}, 'proactive': True,
                        'read_interval_seconds': 300, 'daily_read_limit': 3},
            learning={'enabled': True, 'auto_trials': True, 'min_feedback': 2, 'trial_seconds': 600})
        self.social, self.ctx, self.port = self.create()

    def create(self):
        ctx, port = FakeContext(), CapturedActivityPort(self.home)
        social = build(ctx, port, self.config)
        social.policy.ambient_debounce_seconds = 0
        self.instances.append((social, ctx))
        return social, ctx, port

    async def asyncTearDown(self):
        for social, ctx in reversed(self.instances):
            ctx.release.set()
            await social.stop()
            ctx.unload()
            await asyncio.sleep(0)
            if social.binding.shutdown_task:
                await social.binding.shutdown_task
        reset_hermes_home_override(self.token)
        self.temp.cleanup()

    def observe(self, text, mid, *, chat=CHAT, user='10001', reply_to=None, verified=False):
        item = event(text, mid=mid, chat=chat, user=user)
        item.reply_expected = False
        item.bot_id = '90001'
        if reply_to:
            item.reply_to_message_id = reply_to
        if verified:
            item.verified_reply_author_id = '90001'
        self.social.observe(item)
        return item

    async def drain_models(self):
        for _ in range(8):
            tasks = [task for task in self.ctx.tasks if not task.done() and task is not asyncio.current_task()]
            if not tasks:
                return
            await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.fail('life tasks did not settle')

    def make_idle(self, social=None):
        social = self.social if social is None else social
        for state in social.states.values():
            state.last_activity = time.time()-4000

    async def read_once(self, text='PUBLIC_READING_CANARY：合成模型可以采用对称折法。'):
        async def reader(url):
            return PublishedDocument(url, url, text, '合成阅读记录', time.time())
        self.social.life.reader = AsyncMock(side_effect=reader)
        await self.social.life.advance()
        notes = self.social.life.activities.notes(CHAT)
        self.assertEqual(len(notes), 1)
        return notes[0], self.social.life.reader

    def actual_reply(self, mid, *, request='request', chat=CHAT):
        item = self.observe('一条需要回应的合成讨论', request, chat=chat)
        self.social.prepare_public_context(item)
        self.social.record_self(chat, '这是一条通道已核验发出的合成回复。', mid, reply_to_message_id=request)

    def feedback_command(self, mid, status, *, command_id, chat=CHAT, user='10002'):
        item = self.observe('/social feedback '+mid+' '+status, command_id, chat=chat, user=user)
        return self.social.control(item)

    def two_command_feedbacks(self, status):
        for index in range(2):
            mid = 'bot-'+str(index)
            self.actual_reply(mid, request='request-'+str(index))
            result = self.feedback_command(mid, status, command_id='feedback-'+str(index))
            self.assertIn('已记录', result)
        return self.social.life.strategy(CHAT)

    async def test_configured_reading_creates_offer_and_tick_uses_shared_contact_budget_once(self):
        note, reader = await self.read_once()
        offer = self.social.life.next_opportunity(CHAT, frozenset())
        self.assertIsNotNone(offer)
        self.assertEqual(offer.source_id, note['id'])
        self.make_idle()
        self.assertTrue(await self.social.tick(CHAT))
        self.assertEqual(reader.await_count, 1)
        self.assertEqual(len(self.port.attempts), 1)
        ledger = self.social.contacts.rows(CHAT)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]['kind'], 'activity')
        self.assertEqual(ledger[0]['reference'], offer.reference)
        self.assertEqual(ledger[0]['status'], 'acknowledged')
        self.assertIsNone(self.social.life.next_opportunity(CHAT, self.social.contacts.references(CHAT)))
        self.assertFalse(await self.social.tick(CHAT))
        self.assertFalse(self.ctx.calls)

    async def test_unknown_activity_delivery_does_not_replay_after_restart_and_content_restore(self):
        await self.read_once()
        content = self.social.life.activities.path
        backup = self.home/'content-before-send.sqlite3'
        shutil.copyfile(content, backup)
        self.port.ambiguous = True
        self.make_idle()
        self.assertFalse(await self.social.tick(CHAT))
        self.assertEqual(len(self.port.attempts), 1)
        self.assertEqual(self.social.contacts.rows(CHAT)[0]['status'], 'unknown')
        await self.social.stop()
        shutil.copyfile(backup, content)
        restored, _, restored_port = self.create()
        self.make_idle(restored)
        self.assertIsNone(restored.life.next_opportunity(CHAT, restored.contacts.references(CHAT)))
        self.assertFalse(await restored.tick(CHAT))
        self.assertFalse(restored_port.attempts)

    async def test_public_note_enters_snapshot_and_forget_revokes_view_and_derived_reply(self):
        note, _ = await self.read_once()
        request = self.observe('请接着谈谈这个合成模型', 'question')
        view = self.social.prepare_public_context(request)
        self.assertIn('PUBLIC_READING_CANARY', view.text)
        self.assertIn(URL, view.text)
        self.social.record_self(CHAT, '这条合成回答依赖刚才的公开阅读记录。', 'derived', reply_to_message_id=request.message_id)
        command = self.observe('/social forget-source '+note['id'], 'forget-source', user='10001')
        self.assertIn('已忘记', self.social.control(command))
        self.assertFalse(view.current())
        self.assertNotIn('derived', [row.message_id for row in self.social.recent.routing_history(self.social._scope(CHAT))])
        with self.assertRaises(PermissionError):
            self.social.life.activities.exchange_for_delivery(CHAT, 'derived')
        later = self.observe('继续讨论', 'later')
        self.assertNotIn('PUBLIC_READING_CANARY', self.social.prepare_public_context(later).text)

    async def test_reading_failure_does_not_create_a_share_or_completed_note(self):
        self.social.life.reader = AsyncMock(side_effect=TimeoutError('SYNTHETIC_TIMEOUT'))
        self.social.policy.activities['daily_read_limit'] = 1
        await self.social.life.advance()
        await self.social.life.advance()
        self.assertEqual(self.social.life.reader.await_count, 1)
        self.assertEqual(self.social.life.material(CHAT), [])
        self.assertIsNone(self.social.life.next_opportunity(CHAT, frozenset()))
        self.assertFalse(self.port.attempts)

    async def test_daily_read_budget_survives_restart_and_restored_content_store(self):
        self.config['activities']['daily_read_limit'] = 1
        self.social.policy.activities['daily_read_limit'] = 1
        content = self.social.life.activities.path
        backup = self.home/'content-before-read.sqlite3'
        shutil.copyfile(content, backup)
        await self.read_once()
        day = self.social._day(time.time())
        self.assertEqual(self.social.life.activities.read_budget_used(CHAT, day), 1)
        await self.social.stop()
        shutil.copyfile(backup, content)
        restored, _, _ = self.create()
        restored.life.reader = AsyncMock(side_effect=AssertionError('must not fetch after restored budget'))
        await restored.life.advance()
        self.assertEqual(restored.life.reader.await_count, 0)
        self.assertEqual(restored.life.activities.read_budget_used(CHAT, day), 1)
        self.assertEqual(restored.life.material(CHAT), [])

    async def test_explicit_feedback_commands_start_real_reply_length_trial(self):
        strategy = self.two_command_feedbacks('correction')
        self.assertEqual(strategy['state'], 'trial')
        self.assertEqual(self.social.reply_char_limit(CHAT), 280)
        self.assertIn('280', self.social.prompt_for_chat(CHAT))
        trial = self.social.life.activities.trial(CHAT, strategy['trial_id'])
        self.assertLessEqual(trial['expires']-trial['started'], 601)
        self.assertIsNone(self.social.reply_char_limit(OTHER))

    async def test_verified_quote_feedback_is_classified_and_refines_the_same_event(self):
        self.ctx.response = {'feedback': 'correction'}
        for index in range(2):
            mid = 'quoted-bot-'+str(index)
            self.actual_reply(mid, request='quoted-request-'+str(index))
            self.observe('这里有一处需要纠正。', 'quoted-feedback-'+str(index), user='10002',
                         reply_to=mid, verified=True)
            await self.drain_models()
        self.assertEqual(self.social.reply_char_limit(CHAT), 280)
        self.assertEqual(len(self.social.life.activities.feedback_evidence(CHAT)), 2)
        self.assertEqual(len(self.ctx.calls), 2)

    async def test_negative_feedback_temporarily_stops_sharing_and_revert_restores_it(self):
        await self.read_once()
        self.assertIsNotNone(self.social.life.next_opportunity(CHAT, frozenset()))
        strategy = self.two_command_feedbacks('negative')
        self.assertFalse(strategy['sharing_enabled'])
        self.assertIsNone(self.social.life.next_opportunity(CHAT, frozenset()))
        command = self.observe('/social revert '+strategy['trial_id'], 'revert', user='10001')
        self.assertIn('已撤销', self.social.control(command))
        self.assertTrue(self.social.life.strategy(CHAT)['sharing_enabled'])
        self.assertIsNotNone(self.social.life.next_opportunity(CHAT, frozenset()))

    async def test_trial_changes_ambient_delay_and_expiry_restores_baseline(self):
        strategy = self.two_command_feedbacks('correction')
        store = self.social.life.activities
        store.revert_trial(CHAT, strategy['trial_id'])
        trial = store.propose_trial(CHAT, {'cooldown_multiplier': 2}, store.feedback_evidence(CHAT))
        store.start_trial(CHAT, trial, duration_seconds=600)
        state = self.social.states[CHAT]
        state.last_reply_at = 0
        state.ambient_at = 100
        self.assertEqual(self.social.ambient_delay(CHAT, now=101), 39)
        expires = store.trial(CHAT, trial)['expires']
        store.clock = lambda: expires+1
        self.assertEqual(self.social.ambient_delay(CHAT, now=101), 19)
        self.assertIsNone(self.social.reply_char_limit(CHAT))

    async def test_later_explicit_counterexample_rolls_back_active_trial(self):
        strategy = self.two_command_feedbacks('correction')
        self.actual_reply('later-bot', request='later-request')
        self.feedback_command('later-bot', 'negative', command_id='later-feedback')
        self.assertEqual(self.social.life.activities.trial(CHAT, strategy['trial_id'])['state'], 'reverted')
        self.assertIsNone(self.social.reply_char_limit(CHAT))

    async def test_group_and_dm_reading_and_feedback_remain_exact_scope(self):
        await self.social.stop()
        self.config['activities']['sources'] = {CHAT: [URL], OTHER: [SECOND_URL], DM: [DM_URL]}
        self.social, self.ctx, self.port = self.create()
        texts = {URL: 'GROUP_ONE_CANARY', SECOND_URL: 'GROUP_TWO_CANARY', DM_URL: 'DM_ONLY_CANARY'}
        async def reader(url):
            return PublishedDocument(url, url, texts[url], '合成资料', time.time())
        self.social.life.reader = AsyncMock(side_effect=reader)
        await self.social.life.advance()
        for chat, positive, negatives in (
            (CHAT, 'GROUP_ONE_CANARY', ('GROUP_TWO_CANARY', 'DM_ONLY_CANARY')),
            (OTHER, 'GROUP_TWO_CANARY', ('GROUP_ONE_CANARY', 'DM_ONLY_CANARY'))):
            item = self.observe('看看本群的资料', 'request-'+chat, chat=chat)
            text = self.social.prepare_public_context(item).text
            self.assertIn(positive, text)
            for negative in negatives:
                self.assertNotIn(negative, text)
        dm = json.dumps(self.social.life.material(DM), ensure_ascii=False)
        self.assertIn('DM_ONLY_CANARY', dm)
        self.assertNotIn('GROUP_ONE_CANARY', dm)
        self.assertNotIn('GROUP_TWO_CANARY', dm)
        self.actual_reply('group-bot', request='group-question')
        cross = self.feedback_command('group-bot', 'correction', command_id='cross-feedback', chat=OTHER)
        self.assertIn('操作未执行', cross)
        self.assertEqual(self.social.life.activities.feedback_evidence(OTHER), [])


if __name__ == '__main__':
    unittest.main()
