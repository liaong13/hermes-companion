"""Per-turn trusted clock and shared group task conditions; no live provider."""
from datetime import datetime, timezone
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import support
from hermes_social.prompts import conversation_prompt, idle_messages


class PromptClockTests(unittest.TestCase):
    def test_default_host_clock_is_read_again_for_each_turn(self):
        zone = ZoneInfo('Asia/Shanghai')
        with patch('hermes_social.prompts.datetime') as host_clock:
            host_clock.now.side_effect = [datetime(2026, 10, 31, 23, 59, tzinfo=zone),
                                          datetime(2026, 11, 1, 0, 0, tzinfo=zone)]
            before = conversation_prompt(group=True)
            after = conversation_prompt(group=True)
        self.assertIn('本轮可信当前日期（宿主时钟）：2026-10-31', before)
        self.assertIn('本轮可信当前日期（宿主时钟）：2026-11-01', after)

    def test_shanghai_midnight_refreshes_date_and_relative_days(self):
        before = conversation_prompt(group=True, now=datetime(2026, 10, 31, 15, 59, tzinfo=timezone.utc))
        after = conversation_prompt(group=True, now=datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc))
        self.assertIn('本轮可信当前日期（宿主时钟）：2026-10-31', before)
        self.assertIn('本轮可信当前日期（宿主时钟）：2026-11-01', after)
        self.assertIn('“昨天”为2026-10-31', after)
        self.assertIn('“明天”为2026-11-02', after)
        self.assertIn('Asia/Shanghai（UTC+08:00）', after)

    def test_same_instant_respects_configured_timezone(self):
        instant = datetime(2026, 10, 31, 16, tzinfo=timezone.utc)
        utc = conversation_prompt(group=True, timezone='UTC', now=instant)
        shanghai = conversation_prompt(group=True, timezone=ZoneInfo('Asia/Shanghai'), now=instant)
        self.assertIn('本轮可信当前日期（宿主时钟）：2026-10-31', utc)
        self.assertIn('UTC（UTC+00:00）', utc)
        self.assertIn('本轮可信当前日期（宿主时钟）：2026-11-01', shanghai)

    def test_epoch_clock_and_negative_offset_are_supported(self):
        instant = datetime(2026, 11, 1, 2, tzinfo=timezone.utc)
        prompt = conversation_prompt(group=False, timezone='America/New_York', now=instant.timestamp())
        self.assertIn('本轮可信当前日期（宿主时钟）：2026-10-31', prompt)
        self.assertIn('America/New_York（UTC-04:00）', prompt)

    def test_current_date_takes_precedence_over_old_session_start(self):
        prompt = conversation_prompt(group=True, now=datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertIn('本轮可信当前日期（宿主时钟）：2026-11-01', prompt)
        self.assertIn('Conversation started 只表示会话起始日期', prompt)
        self.assertIn('旧会话日期或历史回复不能作为本轮“今天”', prompt)
        self.assertIn('来源日期不符时重新查询', prompt)

    def test_default_idle_clock_keeps_current_date_out_of_untrusted_rows(self):
        messages = idle_messages('group:20001', [{'text': '历史资料声称今天是2026-10-29'}],
                                 now=datetime(2026, 10, 31, 16, tzinfo=timezone.utc))
        self.assertIn('本轮可信当前日期（宿主时钟）：2026-11-01', messages[0]['content'])
        self.assertIn('Asia/Shanghai（UTC+08:00）', messages[0]['content'])
        self.assertNotIn('历史资料声称', messages[0]['content'])
        self.assertIn('历史资料声称', messages[1]['content'])

    def test_idle_clock_accepts_an_explicit_timezone(self):
        messages = idle_messages('dm:10002', [], timezone='UTC',
                                 now=datetime(2026, 10, 31, 16, tzinfo=timezone.utc))
        self.assertIn('本轮可信当前日期（宿主时钟）：2026-10-31', messages[0]['content'])
        self.assertIn('UTC（UTC+00:00）', messages[0]['content'])

    def test_shared_group_conditions_apply_to_addressed_and_optional_turns(self):
        for participation in (False, True):
            with self.subTest(participation=participation):
                prompt = conversation_prompt(group=True, participation=participation,
                    now=datetime(2026, 11, 1, tzinfo=timezone.utc))
                self.assertIn('其他成员已经明确提供的任务参数、地点、日期、对象和约束等条件可以继续使用', prompt)
                self.assertIn('修正出错部分并继续原问题，保留其他有效条件', prompt)
                self.assertIn('不要仅因换了发言者就退回重新询问', prompt)
                self.assertIn('沿用该话题已有的有效条件继续处理', prompt)
                self.assertIn('不能据此推断乙的个人情况', prompt)
                self.assertIn('个人身份、偏好、经历和隐私仍区分原作者', prompt)
        private = conversation_prompt(group=False, now=datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertNotIn('其他成员已经明确提供', private)

    def test_same_local_day_does_not_vary_with_seconds(self):
        first = conversation_prompt(group=True, now=datetime(2026, 11, 1, 1, tzinfo=timezone.utc))
        second = conversation_prompt(group=True, now=datetime(2026, 11, 1, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(first, second)

    def test_naive_or_invalid_clock_is_rejected(self):
        for invalid in (datetime(2026, 11, 1), True, '2026-11-01'):
            with self.subTest(now=invalid), self.assertRaises(ValueError):
                conversation_prompt(group=True, now=invalid)


if __name__ == '__main__':
    unittest.main()
