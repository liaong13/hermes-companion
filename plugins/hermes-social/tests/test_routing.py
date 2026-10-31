"""Offline multi-person routing; names and literal @ text are never authority."""
# pyright: reportMissingImports=false
import os
from pathlib import Path
import tempfile
import time
import unittest

import support
from hermes_social.context import ContextRecord, ContextScope, RecentContext
from hermes_social.routing import decide
from support import event


class MultiPersonRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.scope = ContextScope(self.tmp.name, 'napcat', 'group:20001')
        self.recent = RecentContext()
        self.now = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    def say(self, text, mid, sender, *, name='同名', age=10):
        row = ContextRecord(self.scope, self.now-age, 'member', name, text, mid,
                            sender_id=sender)
        self.recent.append(row)
        return row

    def ask(self, text, *, sender='10001', mid='99', targets=(), reply=None):
        item = event(text, mid=mid, user=sender)
        item.at_targets, item.bot_id = targets, '90001'
        item.reply_to_message_id = reply
        return decide(item, self.recent.routing_history(self.scope), now=self.now)

    def test_public_background_is_not_filtered_by_topic_words(self):
        self.say('露营计划和天气', '1', '10001')
        self.say('镜头报价', '2', '10002')
        answer = self.ask('周末露营怎么办？')
        self.assertEqual((answer.target, answer.anchor), ('group', 'public_turn'))
        self.assertEqual([row.message_id for row in answer.rows], ['1', '2'])

    def test_short_answers_sharing_jokes_and_corrections_reach_model(self):
        for text in ('选项乙', '合成拼图已经完成第一步', '好的', '不是这个意思', '我想试一下合成活动乙', '那哪个更好？'):
            with self.subTest(text=text):
                self.assertTrue(self.ask(text).participate)
        self.assertFalse(self.ask('😀').participate)
        self.assertFalse(self.ask('/social pause').participate)

    def test_at_and_verified_quote_are_transport_evidence_only(self):
        self.assertEqual(self.ask('大家怎么看？', targets=('10002',)).target, 'member')
        self.assertEqual(self.ask('意见呢', targets=('10002', '90001')).target, 'bot')
        self.assertEqual(self.ask('文字里的@90001').target, 'group')
        item = event('这个呢')
        item.bot_id, item.reply_to_message_id = '90001', '72'
        item.verified_reply_author_id = '90001'
        self.assertEqual(decide(item, ()).target, 'bot')
        item.verified_reply_author_id = '10002'
        self.assertEqual(decide(item, ()).target, 'member')
        del item.verified_reply_author_id
        self.assertEqual(decide(item, ()).target, 'unknown')

    def test_directed_public_background_does_not_become_a_request(self):
        row = ContextRecord(self.scope, self.now-5, 'member', '同名',
                            '其他成员的讨论', '1', sender_id='10002', at_targets=('10003',))
        self.recent.append(row)
        self.assertEqual(self.ask('一起聊聊').rows, (row,))
        self.assertFalse(self.ask('帮帮忙', targets=('10003',)).participate)

    def test_recipient_pairs_survive_other_members_exchange(self):
        original = self.say('拟定合成方案', '1', '10001', age=90)
        own_reply = ContextRecord(self.scope, self.now-60, 'self', '公开名字',
                                  '请选择方案，我再继续安排', '2', reply_to_message_id='1')
        self.recent.append(own_reply)
        other = self.say('讨论游戏', '3', '10002', age=20)
        self.recent.append(ContextRecord(self.scope, self.now-10, 'self', '公开名字',
                                        '你玩到哪一章？', '4', reply_to_message_id='3'))
        answer = self.ask('选项乙')
        self.assertEqual((answer.target, answer.anchor), ('group', 'public_turn'))
        self.assertEqual(answer.exchange, (original, own_reply))
        self.assertIn(original, answer.rows)
        self.assertIn(own_reply, answer.rows)
        self.assertIn(other, answer.rows)
        # Same display name does not create the pair for a different sender.
        self.assertEqual(self.ask('选项丙', sender='10003').anchor, 'public_turn')

    def test_old_or_unproven_pair_is_not_a_continuation_claim(self):
        self.say('拟定合成方案', '1', '10001', age=300)
        self.recent.append(ContextRecord(self.scope, self.now-250, 'self', '公开名字',
                                        '请选择一个合成方案', '2', reply_to_message_id='1'))
        self.assertEqual(self.ask('选项乙').anchor, 'public_turn')
        self.recent.append(ContextRecord(self.scope, self.now-10, 'self', '公开名字', '选择哪个方案', '3'))
        self.assertEqual(self.ask('选项乙').anchor, 'public_turn')

    def test_immediate_turn_taking_is_evidence_without_a_question_phrase(self):
        original = self.say('帮我查一下', 'a', '10001', age=40)
        reply = ContextRecord(self.scope, self.now-20, 'self', '实例',
                              '选一个合成方案，我再继续', 'b', reply_to_message_id='a')
        self.recent.append(reply)
        immediate = self.ask('选项乙')
        self.assertEqual(immediate.anchor, 'recent_exchange')
        self.assertEqual(immediate.exchange, (original, reply))
        self.say('换了一个讨论对象', 'c', '10002', age=10)
        interleaved = self.ask('选项乙')
        self.assertEqual(interleaved.anchor, 'public_turn')
        self.assertEqual(interleaved.exchange, (original, reply))

    def test_public_window_and_row_budget_are_bounded(self):
        self.say('过期', 'old', '10001', age=650)
        for index in range(20):
            self.say('公开资料', str(index), '10002', age=40-index)
        answer = self.ask('继续')
        self.assertEqual(len(answer.rows), 12)
        self.assertEqual([r.message_id for r in answer.rows], [str(i) for i in range(8,20)])

    def test_bound_media_is_transport_owned_without_keyword_test(self):
        request = event('看看我刚发的图')
        request._napcat_optional_media = True
        self.assertEqual(decide(request, ()).anchor, 'media_request')
        request.text = '[attachment:image]'
        del request._napcat_optional_media
        self.assertFalse(decide(request, ()).participate)


if __name__ == '__main__':
    unittest.main()
