"""Authored synthetic scenarios for context, turn-taking and bursts; no chat imports."""
import unittest

import support
from support import event
import test_controller as base
from hermes_social.routing import decide


class PublicDialogue(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.ControllerTests.asyncSetUp
    asyncTearDown = base.ControllerTests.asyncTearDown
    idle = base.ControllerTests.idle
    drain = base.ControllerTests.drain

    def observe(self,text,mid,user='10001',chat='group:20001'):
        item=event(text,mid=mid,user=user,chat=chat)
        item.reply_expected=False
        item.bot_id='90001'
        item.at_targets=()
        self.social.observe(item)
        return item

    async def test_shared_activity_context_reaches_named_turn_without_private_or_other_group(self):
        self.observe('PRIVATE_CANARY','dm',chat='dm:10001')
        self.observe('OTHER_GROUP_CANARY','other',chat='group:20002')
        self.observe('合成活动甲需要准备红色和蓝色纸片','1',user='10002')
        self.observe('合成活动甲可以先裁剪再折叠','2',user='10003')
        current=self.observe('请整理一下合成活动甲的下一步','3')
        current.reply_expected=True
        view=self.social.prepare_public_context(current)
        self.assertIn('合成活动甲',view.text)
        self.assertIn('裁剪',view.text)
        self.assertNotIn('下一步',view.text)
        self.assertNotIn('PRIVATE_CANARY',view.text)
        self.assertNotIn('OTHER_GROUP_CANARY',view.text)
        self.assertTrue(view.current())
        self.observe('也需要准备材料乙','4',user='10003')
        self.assertTrue(view.current())
        self.social.recall('group:20001','1')
        self.assertFalse(view.current())

    async def test_imperative_question_survives_other_members_exchange(self):
        original=self.observe('请继续合成活动的安排','1')
        self.social.record_self('group:20001','请选择方案甲或方案乙，我再继续安排。','900',reply_to_message_id='1')
        self.observe('我正在整理合成材料乙','2',user='10002')
        self.social.record_self('group:20001','材料准备到第几步？','901',reply_to_message_id='2')
        answer=self.observe('选项乙','3')
        result=decide(answer,self.social.recent.routing_history(self.social._scope('group:20001')))
        self.assertEqual(result.exchange[0].message_id,original.message_id)
        self.assertIn(original.message_id,[r.message_id for r in result.rows])
        self.assertIn('900',[r.message_id for r in result.rows])
        stranger=self.observe('选项丙','4',user='10003')
        self.assertEqual(decide(stranger,self.social.recent.routing_history(self.social._scope('group:20001'))).anchor,'public_turn')

    async def test_snapshot_survives_eviction_but_explicit_forgetting_revokes_it(self):
        self.observe('需要稍后忘记的公开资料','old')
        current=self.observe('继续这个话题','current')
        view=self.social.prepare_public_context(current)
        for index in range(70):
            self.observe('新的公开发言'+str(index),'later'+str(index))
        self.assertTrue(view.current())
        self.social.forget_messages('group:20001', ['old'])
        self.assertFalse(view.current())

    async def test_utf8_context_budget_includes_exchange_evidence(self):
        original=self.observe('长资料'*300,'1')
        self.social.record_self('group:20001','回复资料'*240,'2',reply_to_message_id=original.message_id)
        for index in range(10):
            self.observe('背景资料'*240,'background'+str(index),user='10002')
        view=self.social.prepare_public_context(self.observe('继续','last'))
        self.assertLessEqual(len(view.text.encode()),5000)

    async def test_burst_keeps_question_as_background_for_latest_turn(self):
        self.social.busy['group:20001']=1
        question=self.observe('大家想先折哪一种合成纸模型？','1')
        self.social.queue_ambient(question)
        for idx,text in enumerate(('材料甲已经裁好了','材料乙需要折成对称图形','也要给材料丙贴上编号'),2):
            self.social.queue_ambient(self.observe(text,str(idx),user='10002'))
        self.social.busy.clear()
        await self.drain()
        self.assertEqual(len(self.port.dispatched),1)
        self.assertEqual(self.port.dispatched[0].message_id,'4')
        self.assertEqual(question.text,'大家想先折哪一种合成纸模型？')
        prompt=self.social.prepare_public_context(self.port.dispatched[0]).text
        self.assertIn('对称图形',prompt)
        self.assertIn('合成纸模型',prompt)
        self.assertFalse(self.ctx.calls)

    async def test_sharing_has_opportunity_but_other_member_address_does_not(self):
        sharing=self.observe('我完成了合成纸模型的第一张示意图','1')
        self.social.queue_ambient(sharing)
        await self.drain()
        self.assertEqual(self.port.dispatched,[sharing])
        directed=self.observe('这次还请你帮我安排一下','2')
        directed.at_targets=('10002',)
        self.social.queue_ambient(directed)
        await self.drain()
        self.assertEqual(self.port.dispatched,[sharing])
