"""Normal participation shares allowed_groups; idle send scope stays explicit."""
import copy
import time
import unittest

import test_proactive as base
import test_proactive_lifecycle as life
from hermes_napcat.adapter import NapCatAdapter
from hermes_napcat.isolation import AdmissionDenied
from social_plugin_helpers import install_social, optional_valid, raw_policy


def shared_policy(**overrides):
    # Owner idle initiative stays opted in; neither group is an idle target.
    return raw_policy(shared=True, targets=['dm:10001'], **overrides)


def shared_adapter(cfg=None, policy=None):
    return base.social_adapter(cfg, shared_policy() if policy is None else policy)


class SharedPolicy(unittest.TestCase):
    def test_every_allowed_group_accepts_an_ordinary_member_without_mention(self):
        adapter = shared_adapter()
        self.assertNotIn('proactive', adapter.config.extra)
        self.assertEqual(adapter.participation.policy.participation_groups, {'20001', '20002'})
        for group in (20001, 20002):
            with self.subTest(group=group):
                incoming = adapter.convert_event(life.raw_text('大家怎么看这个问题？', group, group=group, user=77777))
                self.assertIsNotNone(incoming)
                self.assertFalse(incoming.reply_expected)
                self.assertEqual(incoming.source.user_id, '77777')
                self.assertFalse(incoming.internal)

    def test_allowlist_addition_and_removal_require_no_second_group_list(self):
        cfg = base.social_config()
        cfg.extra['allowed_groups'].append('20003')
        cfg.extra['allowed_groups'].remove('20002')
        adapter = shared_adapter(cfg)
        self.assertEqual(adapter.participation.policy.participation_groups, {'20001', '20003'})
        self.assertIsNotNone(adapter.convert_event(life.raw_text('新群正常聊天', 10, group=20003)))
        self.assertIsNone(adapter.convert_event(life.raw_text('已移出白名单', 11, group=20002)))
        self.assertIsNone(adapter.convert_event(life.raw_text('未授权场所', 12, group=99999)))

    def test_existing_explicit_test_override_is_backward_compatible(self):
        adapter = shared_adapter(policy=shared_policy(participation_groups=['20001']))
        self.assertIsNotNone(adapter.convert_event(life.raw_text('旧测试范围', 13)))
        self.assertIsNone(adapter.convert_event(life.raw_text('测试覆盖未启用此群', 14, group=20002)))
        adapter = shared_adapter(policy=shared_policy(participation_groups=[]))
        self.assertIsNone(adapter.convert_event(life.raw_text('明确关闭测试参与', 15)))

    def test_shared_participation_needs_no_idle_targets(self):
        adapter = shared_adapter(policy=raw_policy(shared=True, targets=[]))
        self.assertEqual(adapter.participation.policy.targets, set())
        self.assertEqual(adapter.participation.policy.observed_chats, {'group:20001', 'group:20002'})
        self.assertIsNotNone(adapter.convert_event(life.raw_text('仅接话，不主动唤醒', 16, group=20002)))

    def test_global_disable_keeps_addressed_messages_and_does_not_open_dms(self):
        adapter = shared_adapter(policy=shared_policy(enabled=False))
        self.assertIsNone(adapter.convert_event(life.raw_text('未点名', 17, group=20002)))
        self.assertTrue(adapter.convert_event(life.raw_text('公开名字你好', 18, group=20002)).reply_expected)
        self.assertIsNone(adapter.convert_event(life.raw_text('非主人私聊', 19, private=True, user=77777)))

    def test_identity_and_private_owner_admission_still_apply(self):
        adapter = shared_adapter()
        forged = life.raw_text('群成员不能伪造身份', 20, group=20002, user=77777)
        forged['sender']['user_id'] = 10001
        self.assertIsNone(adapter.convert_event(forged))
        self.assertIsNone(adapter.convert_event(life.raw_text('非主人私聊', 21, private=True, user=77777)))

    def test_invalid_explicit_overrides_are_not_normalized(self):
        for groups in (['99999'], ['020001'], ['20001', '20001'], '20001', None):
            with self.subTest(groups=groups), self.assertRaises((ValueError, TypeError)):
                shared_adapter(policy=shared_policy(participation_groups=groups))


class SharedLifecycle(unittest.IsolatedAsyncioTestCase):
    parsed = life.Lifecycle.parsed
    issue_ingress = life.Lifecycle.issue_ingress
    start_group = life.Lifecycle.start_group
    drain_ambient = life.Lifecycle.drain_ambient
    asyncTearDown = life.Lifecycle.asyncTearDown

    async def asyncSetUp(self):
        await life.Lifecycle.asyncSetUp(self)
        client = self.adapter.client
        await self.adapter.disconnect()
        self.adapter = base.HostedNapCatAdapter(base.social_config(), self.binding, self.bridge)
        self.adapter.client = client
        self.adapter._queue_dispatch = self.dispatched.append
        self.social = install_social(self.adapter, shared_policy(), llm=self.llm)
        self.social.policy.ambient_debounce_seconds = 0
        self.social.policy.ambient_cooldown_seconds = 0

    async def test_new_group_has_observation_ambient_and_exact_source_public_style(self):
        self.social.observe(self.parsed('PRIVATE_OWNER_CANARY', mid=22, private=True))
        await self.adapter._receive(life.raw_text('大家觉得周末露营下雨要怎么安排？', 23, group=20002, user=77777))
        await self.drain_ambient()
        self.assertEqual(len(self.dispatched), 1)
        incoming = self.dispatched[0]
        self.assertEqual(incoming.source.chat_id, 'group:20002')
        self.assertEqual(incoming.source.user_id, '77777')
        self.assertNotIn('PRIVATE_OWNER_CANARY', incoming.text)
        await self.start_group(incoming)
        try:
            self.assertTrue(self.adapter.conversation_prompt_for_source(incoming.source))
            self.assertTrue(optional_valid(self.adapter, incoming.source))
            with self.assertRaises(AdmissionDenied):
                self.adapter.conversation_prompt_for_source(copy.deepcopy(incoming.source))
        finally:
            await self.adapter.on_processing_complete(incoming, 'completed')

    async def test_participation_only_group_cannot_generate_or_send_idle_initiative(self):
        chat = 'group:20002'
        state = self.social.states[chat]
        state.last_activity = time.time()-4000
        self.assertFalse(self.social.due(chat))
        self.assertFalse(self.social.send_allowed(chat, state.generation, self.adapter._connection_epoch))
        self.assertFalse(await self.social.tick(chat))
        with self.assertRaises(AdmissionDenied):
            self.social.port.claim_idle(chat, 'a'*32)
        self.assertFalse(self.calls)
        self.assertFalse(self.sent)
        self.social.states['dm:10001'].last_activity = time.time()-4000
        self.assertTrue(self.social.due('dm:10001'))

    async def test_pause_status_and_owner_resume_work_for_shared_group(self):
        chat = 'group:20002'
        await self.adapter._receive(life.raw_text('/napcat-social pause', 24, group=20002, user=77777))
        self.assertTrue(self.social.states[chat].paused)
        before = len(self.dispatched)
        await self.adapter._receive(life.raw_text('暂停后不自主接话', 25, group=20002, user=77777))
        await self.drain_ambient()
        self.assertEqual(len(self.dispatched), before)
        self.assertTrue(self.adapter.convert_event(life.raw_text('公开名字仍可点名', 26, group=20002)).reply_expected)
        denied = self.adapter.convert_event(life.raw_text('/napcat-social resume', 27, group=20002, user=77777))
        self.assertIn('只有', self.social.control(denied))
        resumed = self.adapter.convert_event(life.raw_text('/napcat-social resume', 28, group=20002))
        self.assertIn('启用', self.social.control(resumed))
        self.assertFalse(self.social.states[chat].paused)
        self.assertFalse(self.social.states['group:20001'].paused)


if __name__ == '__main__':
    unittest.main()
