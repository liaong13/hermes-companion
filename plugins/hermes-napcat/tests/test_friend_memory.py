"""Explicit friend memory through real adapter ingress; synthetic data/sink only."""
import asyncio
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_friend_dm as friend

ROOT = Path(__file__).resolve().parents[2]/'hermes-life-memory'
NAME = 'napcat_friend_life_memory_test'
if NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(NAME, ROOT/'__init__.py', submodule_search_locations=[str(ROOT)])
    life = importlib.util.module_from_spec(spec)
    sys.modules[NAME] = life
    spec.loader.exec_module(life)
else:
    life = sys.modules[NAME]


class FriendMemory(unittest.IsolatedAsyncioTestCase):
    raw = friend.FriendDmTests.raw
    feed = friend.FriendDmTests.feed
    drain = friend.FriendDmTests.drain
    sends = friend.FriendDmTests.sends

    async def asyncSetUp(self):
        await friend.FriendDmTests.asyncSetUp(self)
        self.config = dict(enabled=True, instance_id='friend-crossday', domain_id='friend-people', policy_revision=1,
            groups=['20001'], friend_dm=True, explicit_memory={'enabled': True, 'ttl_days': 30},
            conversation={'enabled': False}, candidates={'enabled': False}, episodes={'enabled': False},
            agreements={'enabled': True, 'proactive': False})
        self.service = self.new_service()
        self.mid = 100

    def new_service(self):
        service = life.service.LifeMemoryService(self.home, self.home/'memory-data', self.config, lambda: self.config,
            channel_domain=self.adapter.settings.isolation.domain_id,
            channel_revision=self.adapter.settings.isolation.revision,
            dm_allowed=self.adapter.public_dm_allowed)
        self.adapter.install_social_memory(service, registration_key=('hermes-life-memory', 'synthetic-friend'))
        return service

    async def asyncTearDown(self):
        self.service.close()
        await friend.FriendDmTests.asyncTearDown(self)

    def next_mid(self):
        self.mid += 1
        return self.mid

    def sent_text(self, start=0):
        return '\n'.join(segment['data']['text'] for payload in self.sends()[start:]
                         for segment in payload['message'] if segment['type'] == 'text')

    async def command(self, text, user=77777):
        before = len(self.sends())
        await self.feed(text, mid=self.next_mid(), user=user)
        return self.sent_text(before)

    async def remember(self, value='合成跨天偏好甲'):
        response = await self.command('/social-memory remember preference '+value)
        ident = re.search(r'review ([a-f0-9]{32})', response)[1]
        review = await self.command('/social-memory review '+ident)
        confirmation = review.split('确认：')[1].split('\n')[0]
        return ident, confirmation

    async def test_control_ingress_never_runs_body_or_enters_recent_chat(self):
        ident, confirmation = await self.remember()
        self.assertFalse(self.model_calls)
        self.assertIn('已确认', await self.command(confirmation))
        self.assertFalse(self.model_calls)
        self.assertEqual(self.social.context('dm:77777'), [])
        self.assertFalse(self.adapter._friend_history.get('dm:77777'))
        with self.service.store.connection() as db:
            rows = db.execute('SELECT user_text,assistant_text FROM sources').fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['user_text'], '合成跨天偏好甲')
            self.assertEqual(rows[0]['assistant_text'], '')

    async def test_pending_not_recalled_then_crossday_restart_fact_has_evidence_and_expiry(self):
        ident, confirmation = await self.remember()
        await self.feed('请按本次私聊提供的资料回答', mid=self.next_mid())
        self.assertNotIn('合成跨天偏好甲', json.dumps(self.model_calls[-1][0], ensure_ascii=False))
        await self.command(confirmation)
        self.service.close()
        self.adapter._friend_history.clear()
        self.service = self.new_service()
        # Age the stored evidence while preserving real short-lived transport
        # clocks: this is a cross-day memory check, not a fabricated old source.
        with self.service.store.connection(True) as db:
            db.execute('UPDATE sources SET created=?', (time.time()-3*86400,))
            db.execute('UPDATE memories SET created=?', (time.time()-3*86400,))
        await self.feed('请按已确认的本人偏好给一句建议', mid=self.next_mid())
        request = json.dumps(self.model_calls[-1][0], ensure_ascii=False)
        self.assertIn('合成跨天偏好甲', request)
        self.assertIn('expires_at', request)
        self.assertIn('observed_at', request)
        self.assertIn('message_id', request)
        self.assertNotIn('PRIVATE_REMARK_CANARY', request)

    async def test_another_friend_cannot_confirm_or_recall_same_name_peers_fact(self):
        ident, confirmation = await self.remember()
        self.assertIn('无法处理', await self.command(confirmation, user=88888))
        await self.command(confirmation)
        await self.feed('请只按我的私聊资料回应', mid=self.next_mid(), user=88888)
        self.assertNotIn('合成跨天偏好甲', json.dumps(self.model_calls[-1][0], ensure_ascii=False))

    async def test_forget_removes_confirmed_fact_and_ack_dependent_fallback(self):
        ident, confirmation = await self.remember()
        await self.command(confirmation)
        self.reply_text = '我按你确认的合成跨天偏好甲来接续。'
        await self.feed('请按我确认过的偏好回应', mid=self.next_mid())
        self.assertIn('合成跨天偏好甲', json.dumps(self.model_calls[-1][0], ensure_ascii=False))
        self.assertIn('已忘记', await self.command('/social-memory forget '+ident))
        self.reply_text = '现在没有已确认的旧偏好。'
        await self.feed('继续新的合成讨论', mid=self.next_mid())
        self.assertNotIn('合成跨天偏好甲', json.dumps(self.model_calls[-1][0], ensure_ascii=False))
        with self.service.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sources WHERE assistant_text!=''").fetchone()[0], 0)

    async def test_natural_explicit_rule_and_semantic_candidates_remain_reviewable(self):
        self.assertIn('已保存待核对', await self.command('请记住：我喜欢合成纸卡工坊'))
        self.assertFalse(self.model_calls)  # exact rule needs no model
        original = self.llm._async_caller
        semantic_calls = []
        async def provider(**kwargs):
            if '本轮本人明确请求保存' in kwargs['messages'][0]['content']:
                semantic_calls.append(kwargs)
                text = json.dumps({'field': 'preference', 'quote': '交流时偏好简短而清晰的回答'}, ensure_ascii=False)
                return 'offline', 'same-host-model', SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))], usage=None)
            return await original(**kwargs)
        self.llm._async_caller = provider
        response = await self.command('请记住：我交流时偏好简短而清晰的回答')
        self.assertIn('已保存待核对', response)
        self.assertEqual(len(semantic_calls), 1)
        self.assertNotIn('tools', semantic_calls[0])
        self.assertEqual(self.social.context('dm:77777'), [])

    async def test_ordinary_self_statement_does_not_collect_any_source(self):
        await self.feed('我喜欢合成普通话题', mid=self.next_mid())
        with self.service.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sources').fetchone()[0], 0)

    async def test_invalid_semantic_candidate_never_writes_a_source(self):
        for output in ('not JSON', '{"field":"preference","quote":"凭空合成片段"}',
                       '{"field":[],"quote":"交流时偏好简短而清晰的回答"}'):
            async def provider(**kwargs):
                return 'offline', 'same-host-model', SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=output))], usage=None)
            self.llm._async_caller = provider
            self.assertIn('不能安全提取', await self.command('请记住：我交流时偏好简短而清晰的回答'))
        with self.service.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sources').fetchone()[0], 0)

    async def test_metadata_failure_after_ack_clears_social_fallback_and_stops_next_turn(self):
        ident, confirmation = await self.remember()
        await self.command(confirmation)
        self.reply_text = '合成跨天偏好甲的派生回复'
        with patch.object(self.service, 'record_reference_delivery', side_effect=OSError('synthetic metadata outage')):
            await self.feed('按已确认资料回应', mid=self.next_mid())
        self.assertTrue(self.adapter._memory_context_fault)
        self.assertNotIn('派生回复', json.dumps(self.social.context('dm:77777'), ensure_ascii=False))
        before = len(self.model_calls)
        await self.feed('新的合成请求', mid=self.next_mid())
        self.assertEqual(len(self.model_calls), before)

    async def test_explicit_experience_recall_is_peer_scoped_and_forgettable(self):
        response = await self.command('/social-memory episode 我们在合成工坊核对过纸卡编号')
        ident = re.search(r'review ([a-f0-9]{32})', response)[1]
        review = await self.command('/social-memory review '+ident)
        confirmation = review.split('确认：')[1].split('\n')[0]
        self.assertIn('不代表其他参与者确认', review)
        self.assertIn('无法处理', await self.command(confirmation, user=88888))
        self.assertIn('已确认', await self.command(confirmation))
        self.service.close()
        self.service = self.new_service()
        await self.feed('接续已确认的合成经历', mid=self.next_mid())
        self.assertIn('subject_confirmed_statement', json.dumps(self.model_calls[-1][0], ensure_ascii=False))
        self.assertIn('合成工坊核对过纸卡编号', json.dumps(self.model_calls[-1][0], ensure_ascii=False))
        await self.feed('只看我的私聊资料', mid=self.next_mid(), user=88888)
        self.assertNotIn('合成工坊核对过纸卡编号', json.dumps(self.model_calls[-1][0], ensure_ascii=False))
        self.assertIn('已忘记', await self.command('/social-memory forget '+ident))
        await self.feed('继续新的合成讨论', mid=self.next_mid())
        self.assertNotIn('合成工坊核对过纸卡编号', json.dumps(self.model_calls[-1][0], ensure_ascii=False))

    async def test_friend_recall_revokes_fact_and_prior_derived_reply(self):
        ident, confirmation = await self.remember()
        original_mid = 101
        await self.command(confirmation)
        await self.adapter._receive(dict(post_type='notice', notice_type='friend_recall', self_id=90001,
            user_id=77777, message_id=original_mid))
        await self.feed('请看当前仍有效的资料', mid=self.next_mid())
        self.assertNotIn('合成跨天偏好甲', json.dumps(self.model_calls[-1][0], ensure_ascii=False))

    async def test_long_interval_friend_recall_does_not_require_a_live_admission_cache(self):
        ident, confirmation = await self.remember()
        await self.command(confirmation)
        self.reply_text = '合成跨天偏好甲的旧回复'
        await self.feed('按确认资料接续', mid=self.next_mid())
        self.adapter.settings.friend_dm.expires = 0
        self.assertFalse(self.adapter.public_dm_allowed('dm:77777'))
        await self.adapter._receive(dict(post_type='notice', notice_type='friend_recall', self_id=90001,
            user_id=77777, message_id=101))
        with self.service.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sources').fetchone()[0], 0)
        before = len(self.model_calls)
        await self.feed('请看当前仍有效的资料', mid=self.next_mid())
        self.assertEqual(len(self.model_calls), before+1, 'valid next input must not be cancelled by deferred withdrawal cleanup')
        self.assertNotIn('合成跨天偏好甲', json.dumps(self.model_calls[-1][0], ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
