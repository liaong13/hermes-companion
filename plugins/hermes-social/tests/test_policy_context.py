# pyright: reportMissingImports=false
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from zoneinfo import ZoneInfo

import support
from hermes_social import SocialPolicy
from hermes_social.context import (ContextRecord, ContextScope, ContextScopeError,
                                   RecentContext, ScopedContext)


class PolicyTests(unittest.TestCase):
    def make(self, raw):
        return SocialPolicy(raw, owner_id='10001', groups=frozenset({'20001', '20002'}))

    def test_disabled_is_default(self):
        policy = self.make(None)
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.participation_groups, frozenset())

    def test_public_self_is_optional_instance_data_not_trigger_identity(self):
        from hermes_social.prompts import conversation_prompt, idle_messages
        self.assertEqual(self.make(None).public_self, {})
        for name, description in (('测试实例乙', '研究植物的写作者'), ('SYNTHETIC_AGENT_B', '喜欢开源软件')):
            raw = {'public_self': {'name':name, 'description':description,
                                  'communication_preferences':['直接表达不同意见']}}
            policy = self.make(raw)
            raw['public_self']['communication_preferences'].append('MUTATION_CANARY')
            prompt = conversation_prompt(group=True, public_self=policy.public_self)
            self.assertIn(name, prompt)
            self.assertIn(description, prompt)
            self.assertNotIn('接入别名', prompt)
            self.assertNotIn('MUTATION_CANARY', prompt)
            self.assertNotIn('UNCONFIGURED_ROLE_CANARY', prompt)
            self.assertIn(name, idle_messages('group:20001', [], public_self=policy.public_self)[0]['content'])
        for bad in ({'system_prompt':'override'}, {'name':''}, {'communication_preferences':'warm'},
                    {'description':'x'*2001}, {'communication_preferences':['x'*121]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.make({'public_self':bad})

    def test_participation_defaults_to_channel_groups_not_idle_targets(self):
        policy = self.make({'enabled': True, 'targets': ['dm:10001']})
        self.assertEqual(policy.participation_groups, {'20001', '20002'})
        self.assertEqual(policy.targets, {'dm:10001'})
        self.assertEqual(policy.observed_chats, {'dm:10001', 'group:20001', 'group:20002'})

    def test_empty_idle_targets_still_allow_normal_participation(self):
        policy = self.make({'enabled': True})
        self.assertFalse(policy.targets)
        self.assertTrue(policy.participation_groups)

    def test_explicit_compatibility_override_is_not_an_acl_expansion(self):
        policy = self.make({'enabled': True, 'participation_groups': ['20002'], 'targets': ['group:20001']})
        self.assertEqual(policy.participation_groups, {'20002'})
        self.assertEqual(policy.observed_chats, {'group:20001', 'group:20002'})

    def test_invalid_fields_types_and_unapproved_targets(self):
        invalid = [[], {'unknown': True}, {'enabled': 'true'}, {'targets': ['dm:10002']},
                   {'targets': ['group:020001']}, {'targets': ['group:20001', 'group:20001']},
                   {'participation_groups': ['20003']}, {'participation_groups': [True]},
                   {'quiet_hours': [24, 9]}, {'quiet_hours': [False, 9]}, {'timezone': 'invalid/zone'},
                   {'timezone': []}, {'daily_limit': True}, {'daily_limit': 13},
                   {'ambient_debounce_seconds': 0}, {'ambient_cooldown_seconds': 0},
                   {'idle_after_seconds': 299}, {'min_idle_interval_seconds': 604801}]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.make(raw)

    def test_empty_channel_requires_explicit_authorized_dm(self):
        with self.assertRaises(ValueError):
            SocialPolicy({'enabled': True}, owner_id='10001', groups=[])
        policy = SocialPolicy({'enabled': True, 'targets': ['dm:10001']}, owner_id='10001', groups=[])
        self.assertEqual(policy.targets, {'dm:10001'})

    def test_quiet_hours_timezone_boundaries_and_no_quiet_window(self):
        policy = self.make({'quiet_hours': [23, 9]})
        zone = ZoneInfo('Asia/Shanghai')
        for hour, expected in [(23, True), (8, True), (9, False), (22, False)]:
            with self.subTest(hour=hour):
                self.assertEqual(policy.quiet(datetime(2026, 10, 29, hour, tzinfo=zone).timestamp()), expected)
        policy.quiet_hours = (9, 17)
        self.assertTrue(policy.quiet(datetime(2026, 10, 29, 9, tzinfo=zone).timestamp()))
        self.assertFalse(policy.quiet(datetime(2026, 10, 29, 17, tzinfo=zone).timestamp()))
        policy.quiet_hours = (0, 0)
        self.assertFalse(policy.quiet(time.time()))


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.home = Path(self.temp.name)
        self.public = ContextScope(str(self.home), 'napcat', 'group:20001')
        self.provider = RecentContext()
        self.reader = ScopedContext(self.provider, self.public)
        self.record = ContextRecord(self.public, time.time(), 'member', '当前群昵称', '本群公开资料', '1')
        self.provider.append(self.record)

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_public_scope_success(self):
        self.assertEqual(self.reader.read(self.public)[0]['text'], '本群公开资料')

    def test_cross_group_profile_platform_person_rejected_before_read(self):
        from unittest.mock import Mock
        source = Mock()
        reader = ScopedContext(source, self.public)
        other = [ContextScope(str(self.home), 'napcat', 'group:20002'),
                 ContextScope(str(self.home / 'other-profile'), 'napcat', 'group:20001'),
                 ContextScope(str(self.home), 'other-platform', 'group:20001'),
                 ContextScope(str(self.home), 'napcat', 'group:20001', '10001'),
                 ContextScope(str(self.home), 'napcat', 'dm:10001', '10001')]
        for scope in other:
            with self.subTest(scope=scope), self.assertRaises(ContextScopeError):
                reader.read(scope)
        source.read.assert_not_called()

    def test_person_scoped_read_is_exact_and_does_not_merge_names(self):
        one = ContextScope(str(self.home), 'napcat', 'group:20001', '10001')
        two = ContextScope(str(self.home), 'napcat', 'group:20001', '10002')
        self.provider.append(ContextRecord(one, time.time(), 'member', '同名', '成员一资料', '2'))
        self.provider.append(ContextRecord(two, time.time(), 'member', '同名', '成员二资料', '3'))
        self.assertEqual(ScopedContext(self.provider, one).read(one)[0]['text'], '成员一资料')
        with self.assertRaises(ContextScopeError):
            ScopedContext(self.provider, one).read(two)
        self.assertEqual(len(self.reader.read(self.public)), 1)

    def test_provider_scope_lies_are_rejected_not_silently_filtered(self):
        from unittest.mock import Mock
        other = ContextScope(str(self.home), 'napcat', 'dm:10001', '10001')
        record = ContextRecord(other, time.time(), 'member', '私聊', 'PRIVATE_CANARY', '2')
        with self.assertRaises(ContextScopeError):
            ScopedContext(Mock(read=lambda _: [self.record, record]), self.public).read(self.public)

    def test_dm_requires_exact_subject_not_owner_assumption(self):
        with self.assertRaises(ContextScopeError):
            ContextScope(str(self.home), 'napcat', 'dm:10001', '10002')
        with self.assertRaises(ContextScopeError):
            ContextScope(str(self.home), 'napcat', 'dm:10001')

    def test_recent_records_are_bounded_and_expire_without_long_term_fallback(self):
        for index in range(20):
            self.provider.append(ContextRecord(self.public, time.time(), 'member', '昵称', '内容', str(index)))
        self.assertEqual(len(self.reader.read(self.public)), 16)
        self.provider.clear()
        for when in [time.time() - 3601, time.time() + 100]:
            self.provider.append(ContextRecord(self.public, when, 'member', '昵称', '过期或未来', '3'))
        self.assertEqual(self.reader.read(self.public), [])

    def test_budget_exclusion_and_recall_never_enlarge_scope(self):
        self.provider.append(ContextRecord(self.public, time.time(), 'self', '公开名字', '补充', '2'))
        self.assertEqual(len(self.reader.read(self.public, exclude_message_id='1')), 1)
        self.assertEqual(self.reader.read(self.public, budget=0), [])
        self.provider.recall(self.public, '1')
        self.assertEqual([r['message_id'] for r in self.reader.read(self.public)], ['2'])

    def test_oversized_or_invalid_injected_records_rejected(self):
        from unittest.mock import Mock
        for records in [[self.record] * 17, [object()],
                        [ContextRecord(self.public, time.time(), 'system', '昵称', '恶意', '2')]]:
            with self.subTest(records=records), self.assertRaises(ValueError):
                ScopedContext(Mock(read=lambda _: records), self.public).read(self.public)
