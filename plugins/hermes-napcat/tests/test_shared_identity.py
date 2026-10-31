"""Synthetic profile/model/QQ fixtures; no active home, API or QQ traffic."""
import asyncio
import copy
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_plugin
import test_proactive_lifecycle as life
from hermes_napcat.adapter import Settings
from hermes_napcat.isolation import AdmissionDenied
from hermes_napcat.shared_identity import SharedIdentity, SharedIdentityPolicy


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home/'memories').mkdir()
        (self.home/'SOUL.md').write_text('我是合成人格甲，温柔而有主见。')
        self.private = ('我与主人一起讨论了盆栽。\n\n'
                        '不要告诉别人：主人藏着 RESTRICTED_CANARY。\n\n'
                        '主人资料 PRIVATE_RAW_CANARY')
        (self.home/'memories/MEMORY.md').write_text(self.private)
        (self.home/'memories/USER.md').write_text('个人资料 USER_RAW_CANARY')
        self.calls = []
        self.verdict = {'safe':True, 'grounded':True}
        self.candidates = [{'source':'memories/MEMORY.md', 'quote':'我与主人一起讨论了盆栽。',
                            'subject':'self', 'text':'我曾与主人一起讨论盆栽。'}]
        self.live = True
        async def complete(**kwargs):
            from gateway.session_context import get_session_env
            from hermes_constants import get_hermes_home
            self.calls.append((kwargs, get_session_env('HERMES_SESSION_CHAT_ID'),
                               get_session_env('HERMES_SESSION_ID'), Path(get_hermes_home())))
            return SimpleNamespace(text=json.dumps(self.verdict if kwargs['purpose'].endswith('review')
                                                   else {'memories':self.candidates}, ensure_ascii=False))
        self.binding = SimpleNamespace(home=self.home, validate_active=lambda: None,
            facade=lambda: SimpleNamespace(acomplete=complete))
        self.policy = SharedIdentityPolicy({'enabled':True, 'daily_memory':True})
        self.identity = SharedIdentity(self.binding, self.policy)

    async def prepare(self):
        return await self.identity.prepare(lambda: self.live)

    async def test_disabled_does_not_read_files_or_call_model(self):
        self.identity.policy = SharedIdentityPolicy()
        with patch('hermes_napcat.shared_identity._snapshot', side_effect=AssertionError('no read')):
            self.assertIsNone(await self.prepare())
        self.assertEqual(self.calls, [])

    async def test_soul_only_reads_same_profile_and_makes_no_memory_call(self):
        self.policy.daily_memory = False
        view = await self.prepare()
        self.assertIn((self.home/'SOUL.md').read_text(), view.text)
        self.assertNotIn('PRIVATE_RAW_CANARY', view.text)
        self.assertEqual(self.calls, [])

    async def test_reviewed_daily_memory_without_raw_private_material_or_session(self):
        from gateway.session_context import set_session_vars
        set_session_vars(chat_id='PRIVATE_PARENT_CHAT', session_id='PRIVATE_PARENT_SESSION')
        view = await self.prepare()
        self.assertIn('我曾与主人一起讨论盆栽。', view.text)
        for canary in ('RESTRICTED_CANARY', 'PRIVATE_RAW_CANARY', 'USER_RAW_CANARY'):
            self.assertNotIn(canary, view.text)
            self.assertIn(canary, self.calls[1][0]['messages'][1]['content'])
        self.assertEqual(len(self.calls), 2)
        for kwargs, chat, session, home in self.calls:
            self.assertEqual((chat, session, home), ('', '', self.home))
            self.assertNotIn('tools', kwargs)
        self.assertTrue(view.current())

    async def test_reviewer_rejects_unsafe_ungrounded_ill_typed_and_extra_fields(self):
        for verdict in ({'safe':False,'grounded':True}, {'safe':True,'grounded':False},
                        {'safe':1,'grounded':True}, {'safe':True,'grounded':True,'extra':'x'}, []):
            self.verdict = verdict
            with self.subTest(verdict=verdict):
                self.assertNotIn('我曾与主人一起讨论盆栽。', (await self.prepare()).text)

    async def test_secret_evidence_cannot_be_laundered_by_cropping_or_paraphrase(self):
        for quote, text in (('主人藏着 RESTRICTED_CANARY。','主人最近有个小收藏。'),
                            ('我与主人一起讨论了盆栽。','联系地址 a@example.invalid'),
                            ('没有发生的故事','我曾与主人一起旅行。')):
            self.calls.clear()
            self.candidates[0].update(quote=quote, text=text)
            view = await self.prepare()
            self.assertNotIn(text, view.text)
            self.assertEqual(len(self.calls), 1)

    async def test_model_failure_omits_memory_but_keeps_soul(self):
        self.binding.facade = lambda: SimpleNamespace(acomplete=AsyncMock(side_effect=TimeoutError))
        view = await self.prepare()
        self.assertIn('我是合成人格甲', view.text)
        self.assertNotIn('PRIVATE_RAW_CANARY', view.text)

    async def test_file_edit_delete_and_policy_revoke_invalidate_live_view(self):
        for name in ('SOUL.md','memories/MEMORY.md','memories/USER.md'):
            path = self.home/name
            before = path.read_text()
            view = await self.prepare()
            path.write_text(before+'\nCHANGED')
            self.assertFalse(view.current())
            path.write_text(before)
            self.assertFalse(view.current())
            view = await self.prepare()
            path.unlink()
            self.assertFalse(view.current())
            path.write_text(before)
        view = await self.prepare()
        self.policy.enabled = False
        self.assertFalse(view.current())

    async def test_source_expiry_during_review_cannot_publish_memory(self):
        original = self.binding.facade().acomplete
        async def complete(**kwargs):
            result = await original(**kwargs)
            if kwargs['purpose'].endswith('review'):
                self.live = False
            return result
        self.binding.facade = lambda: SimpleNamespace(acomplete=complete)
        with self.assertRaises(AdmissionDenied):
            await self.prepare()

    async def test_links_oversized_missing_and_writable_files_fail_closed(self):
        soul = self.home/'SOUL.md'
        soul.unlink()
        with self.assertRaises(AdmissionDenied):
            await self.prepare()
        other = self.home/'other'
        other.write_text('FOREIGN_SOUL_CANARY')
        soul.symlink_to(other)
        with self.assertRaises(AdmissionDenied):
            await self.prepare()
        soul.unlink()
        soul.write_text('x'*32769)
        with self.assertRaises(AdmissionDenied):
            await self.prepare()
        soul.write_text('role')
        soul.chmod(0o666)
        with self.assertRaises(AdmissionDenied):
            await self.prepare()

    async def test_memory_parent_symlink_is_not_followed(self):
        (self.home/'memories').rename(self.home/'elsewhere')
        (self.home/'memories').symlink_to(self.home/'elsewhere', target_is_directory=True)
        with self.assertRaises(AdmissionDenied):
            await self.prepare()


class SharedGroupTests(unittest.IsolatedAsyncioTestCase):
    parsed = life.Lifecycle.parsed
    issue_ingress = life.Lifecycle.issue_ingress
    start_group = life.Lifecycle.start_group
    asyncTearDown = life.Lifecycle.asyncTearDown

    async def asyncSetUp(self):
        await life.Lifecycle.asyncSetUp(self)
        self.adapter.settings.shared_identity.enabled = True
        (self.home/'SOUL.md').write_text('我是群聊与私聊共用的合成人格。')
        from gateway.run_turn_runner import TurnRunner
        self.contract = patch.object(TurnRunner, 'public_shared_identity_contract', 1, create=True)
        self.contract.start()
        self.addCleanup(self.contract.stop)
        local_contract = patch.object(TurnRunner, 'public_memory_context_contract', 1, create=True)
        local_contract.start()
        self.addCleanup(local_contract.stop)

    async def test_soul_reaches_exact_group_source_without_opening_private_tools(self):
        incoming = self.parsed('公开名字你好', mid=151)
        await self.start_group(incoming)
        self.assertIn('群聊与私聊共用的合成人格', self.adapter.conversation_prompt_for_source(incoming.source))
        self.assertEqual(self.adapter.public_memory_history_policy(incoming.source), {'mode':'local_events','contract':1})
        self.assertNotIn('群聊与私聊共用的合成人格', incoming.text)
        with self.assertRaises(AdmissionDenied):
            self.adapter.conversation_prompt_for_source(copy.deepcopy(incoming.source))
        await self.adapter.on_processing_complete(incoming, 'completed')

    async def test_edit_invalidates_group_prompt_tools_and_delivery(self):
        incoming = self.parsed('公开名字你好', mid=152)
        await self.start_group(incoming)
        authorize = self.adapter.tool_authority_for_source(incoming.source)
        (self.home/'SOUL.md').write_text('已修改的合成人格')
        self.assertFalse(self.adapter.admit_group_source(incoming.source))
        self.assertIsNotNone(authorize('web_search', {'query':'public'}))
        with self.assertRaises(AdmissionDenied):
            self.adapter.conversation_prompt_for_source(incoming.source)
        with self.assertRaises(AdmissionDenied):
            self.adapter._check_reply_target('group:20001', '152')
        await self.adapter.on_processing_complete(incoming, 'failed')

    async def test_disabled_social_still_keeps_addressed_shared_identity(self):
        from hermes_napcat.participation import NullParticipation
        self.adapter.participation = NullParticipation()
        incoming = self.parsed('公开名字你好', mid=153)
        await self.start_group(incoming)
        self.assertIn('群聊与私聊共用的合成人格', self.adapter.conversation_prompt_for_source(incoming.source))
        await self.adapter.on_processing_complete(incoming, 'completed')

    async def test_group_idle_uses_same_soul_and_rejects_late_reply_after_edit(self):
        messages = await self.social.messages('group:20001', 'a'*32)
        self.assertIn('群聊与私聊共用的合成人格', messages[0]['content'])
        grant = self.social.port.claim_idle('group:20001', 'a'*32)
        self.assertTrue(self.social.port._grant_valid(grant))
        (self.home/'SOUL.md').write_text('已修改的合成人格')
        self.assertFalse(self.social.port._grant_valid(grant))

    async def test_preparing_identity_does_not_publish_a_usable_turn_permit(self):
        incoming = self.parsed('公开名字你好', mid=154)
        await self.issue_ingress(incoming)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.adapter.shared_identity.prepare
        async def prepare(current):
            entered.set()
            await release.wait()
            return await original(current)
        async def start():
            await self.adapter.on_processing_start(incoming)
            try:
                return self.adapter.conversation_prompt_for_source(incoming.source)
            finally:
                await self.adapter.on_processing_complete(incoming, 'completed')
        with patch.object(self.adapter.shared_identity, 'prepare', side_effect=prepare):
            task = asyncio.create_task(start())
            self.jobs.append(task)
            await entered.wait()
            try:
                self.assertFalse(self.adapter.admit_group_source(incoming.source))
                with self.assertRaises(AdmissionDenied):
                    self.adapter.tool_authority_for_source(incoming.source)
            finally:
                release.set()
            self.assertIn('群聊与私聊共用的合成人格', await task)


class ConfigTests(unittest.TestCase):
    def test_shared_identity_is_opt_in_strict_and_cannot_select_paths(self):
        self.assertFalse(SharedIdentityPolicy().enabled)
        for raw in (True, {'enabled':1}, {'path':'/not-allowed'}, {'daily_memory':True}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                SharedIdentityPolicy(raw)
        with self.assertRaises(ValueError):
            Settings(test_plugin.config(shared_identity={'enabled':True}).extra)
