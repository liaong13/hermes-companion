"""Real parent wiring with scripted model products and synthetic channel ACKs.

No network/model/QQ is used. Only controller.record_self stands for a trusted
channel's confirmed-delivery callback; drafts and unknown sends never call it.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from support import FakeContext, FakePort, build, event, policy
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from hermes_social.method_validation import CASES


CHAT = 'group:20001'
OTHER = 'group:20002'
CONDITION = '多人共同决定合成活动方案时'
INSTRUCTION = '先把每项建议的原始提出者与实际采用者分别列明，再继续安排。'


class EnhancedMethodsRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.home = Path(self.temp.name)
        self.token = set_hermes_home_override(self.home)
        self.instances = []
        self.proposal_calls = 0
        self.validation_calls = 0
        self.coverage_calls = 0
        self.coverage_payloads = []
        self.pause_proposal = False
        self.pause_feedback = False
        self.pause_coverage = False
        self.proposal_entered,self.proposal_release = asyncio.Event(),asyncio.Event()
        self.feedback_entered,self.feedback_release = asyncio.Event(),asyncio.Event()
        self.coverage_entered,self.coverage_release = asyncio.Event(),asyncio.Event()
        self.settings = policy(targets=[CHAT,OTHER],initiative_mode='local',
            dialogue={'enabled':True,'orchestration':False},
            learning={'enabled':True,'auto_trials':False,'min_feedback':2,
                      'semantic':True,'semantic_auto_trials':True})
        self.social,self.ctx,self.port = self.create()

    def create(self):
        ctx,port = FakeContext(),FakePort(self.home)
        original = ctx.llm._async_caller
        async def provider(**kwargs):
            messages = kwargs['messages']
            last = messages[-1]['content']
            try:
                payload = json.loads(last)
            except (ValueError,TypeError):
                payload = None
            result = ctx.response
            if isinstance(payload,dict) and payload.get('kind') == 'social_method_proposal':
                self.proposal_calls += 1
                self.proposal_entered.set()
                if self.pause_proposal:
                    await self.proposal_release.wait()
                result = dict(category='authorship',condition=CONDITION,instruction=INSTRUCTION,
                    evidence_ids=[row['feedback_id'] for row in payload['samples']]) if self.proposal_calls == 1 else None
            elif isinstance(payload,dict) and payload.get('kind') == 'social_reply_coverage':
                self.coverage_calls += 1
                self.coverage_payloads.append(payload)
                self.coverage_entered.set()
                if self.pause_coverage:
                    await self.coverage_release.wait()
                result = dict(completed=[],waiting=[],methods=[row['id'] for row in payload['methods']])
            elif isinstance(payload,dict) and 'quoted_reply_feedback' in payload:
                self.feedback_entered.set()
                if self.pause_feedback:
                    await self.feedback_release.wait()
                result = dict(feedback='correction')
            else:
                case = next((row for row in CASES if row.scenario == last),None)
                if case is not None:
                    self.validation_calls += 1
                    is_method = INSTRUCTION in messages[0]['content']
                    # The fixtures exercise the comparison/promotion wiring.
                    # They are not evidence of natural-language model quality.
                    result = json.loads(case.expected_json) if is_method or ':assembly-marker:' in case.case_id else dict(
                        proposer='甲',adopter='乙',topic='叶脉分类')
            old = ctx.response
            ctx.response = result
            try:
                return await original(**kwargs)
            finally:
                ctx.response = old
        ctx.llm._async_caller = provider
        social = build(ctx,port,self.settings)
        self.instances.append((social,ctx))
        return social,ctx,port

    async def asyncTearDown(self):
        self.proposal_release.set()
        self.feedback_release.set()
        self.coverage_release.set()
        for social,ctx in reversed(self.instances):
            ctx.release.set()
            await social.stop()
            ctx.unload()
            await asyncio.sleep(0)
            if social.binding.shutdown_task:
                await social.binding.shutdown_task
        reset_hermes_home_override(self.token)
        self.temp.cleanup()

    def observe(self,text,mid,*,user='10001',chat=CHAT,quote=None,name='合成成员'):
        item = event(text,mid=mid,user=user,chat=chat,name=name)
        item.reply_expected = False
        item.bot_id = '90001'
        if quote is not None:
            item.reply_to_message_id = quote
            item.verified_reply_author_id = item.bot_id
        self.social.observe(item)
        return item

    async def drain(self):
        for _ in range(12):
            tasks = [task for task in self.ctx.tasks if not task.done() and task is not asyncio.current_task()]
            if not tasks:
                return
            await asyncio.wait_for(asyncio.gather(*tasks),5)
        self.fail('semantic method tasks did not settle')

    def origin_feedback(self,index):
        request = self.observe('合成标签分类需要核对谁提出哪项建议。','origin-request-'+str(index))
        self.social.prepare_public_context(request)
        mid = 'origin-answer-'+str(index)
        self.social.record_self(CHAT,'这份合成答复遗漏了建议来源的区分。',mid,
                                reply_to_message_id=request.message_id)
        feedback = self.observe('/social feedback '+mid+' correction','origin-feedback-'+str(index),user='10002')
        result = self.social.control(feedback)
        self.assertIn('已记录',result)
        return request,mid,feedback

    async def learned_method(self):
        self.origin_feedback(0)
        self.assertEqual(self.social.life.enhanced.methods.list_methods(CHAT),[])
        self.origin_feedback(1)
        await self.drain()
        rows = self.social.life.enhanced.methods.list_methods(CHAT)
        self.assertEqual(len(rows),1,self.social.life.last_error)
        self.assertEqual(rows[0]['state'],'retained',self.social.life.last_error)
        self.assertEqual(self.proposal_calls,1)
        self.assertEqual(self.validation_calls,4)
        return rows[0]['id']

    @staticmethod
    def payload(view):
        return json.loads(view.text[view.text.index('{'):])

    async def reviewed_draft(self,ident,*,mid='new-request'):
        request = self.observe('新的合成活动中请分别确认建议提出者与采用者。',mid)
        self.social.processing_start(request)
        view = self.social.prepare_public_context(request)
        self.assertIn(ident,[row['id'] for row in self.payload(view)['methods']])
        self.assertIn(INSTRUCTION,view.text)
        draft = '合成成员乙提出标签分类建议，合成成员甲决定采用。'
        await self.social.prepare_reply(request,draft)
        self.assertGreater(self.coverage_calls,0)
        return request,view,draft

    async def test_verified_feedback_to_heldout_method_body_and_actual_ack_use(self):
        ident = await self.learned_method()
        request,view,draft = await self.reviewed_draft(ident)
        store = self.social.life.enhanced.methods
        self.assertTrue(view.current())
        self.assertEqual(store.uses(CHAT,ident),[])
        # Only the channel-verified ACK callback records application of advice.
        self.social.record_self(CHAT,draft,'method-ack',reply_to_message_id=request.message_id)
        uses = store.uses_for_delivery(CHAT,'method-ack')
        self.assertEqual(len(uses),1)
        self.assertEqual(uses[0]['method_id'],ident)
        self.assertEqual(uses[0]['request_id'],request.message_id)
        self.assertEqual(uses[0]['reference'],self.social.life.activities.exchange_for_delivery(CHAT,'method-ack'))
        self.social.processing_complete(request)

    async def test_draft_or_unknown_delivery_does_not_record_method_use(self):
        ident = await self.learned_method()
        request,_,_ = await self.reviewed_draft(ident)
        self.social.processing_complete(request)
        self.assertEqual(self.social.life.enhanced.methods.uses(CHAT,ident),[])
        with self.assertRaises(PermissionError):
            self.social.life.activities.exchange_for_delivery(CHAT,'unknown-no-ack')

    async def test_method_only_coverage_has_frozen_author_names_and_current_recipient(self):
        ident = await self.learned_method()
        source = self.observe('我为合成拼装提出标签阵列的建议。','rendered-source',
                              user='10002',name='合成建议者乙')
        current = self.observe('我采用这项拼装建议，请分别说明提出者和采用者。','named-recipient',
                               user='10001',name='合成采用者甲')
        self.social.processing_start(current)
        view = self.social.prepare_public_context(current)
        enhanced = self.social.life.enhanced
        self.assertIsNone(enhanced.requests)
        self.assertNotIn((CHAT,current.message_id),enhanced.plans)
        self.assertIn(ident,[row['id'] for row in self.payload(view)['methods']])
        # Later observations must not replace the exact rows/name rendering
        # actually supplied to the body generator for this source.
        self.observe('另一份后来到达的合成资料。','after-snapshot',user='10002',name='后来使用的群昵称')
        self.assertTrue(view.current())
        await self.social.prepare_reply(current,'合成建议者乙提出标签阵列建议，合成采用者甲决定采用。')
        payload = self.coverage_payloads[-1]
        self.assertEqual(payload['requests'],[])
        self.assertEqual(payload['current_name'],'合成采用者甲')
        evidence = {row['id']:row for row in payload['method_evidence']}
        self.assertEqual(evidence[source.message_id]['name'],'合成建议者乙')
        self.assertNotEqual(evidence[source.message_id]['author'],payload['current_author'])
        self.assertEqual(evidence[current.message_id]['name'],payload['current_name'])
        self.assertEqual(evidence[current.message_id]['author'],payload['current_author'])
        self.assertNotIn('after-snapshot',evidence)
        self.assertTrue(all(row['author'] not in {'10001','10002','10003'} for row in evidence.values()))
        rendered = {row['message_id']:row for row in self.payload(view)['messages']}
        self.assertEqual(evidence[source.message_id]['text'],rendered[source.message_id]['text'])
        self.assertTrue(any(row['role']=='self' and row['author']=='self' for row in evidence.values()))
        self.social.processing_complete(current)
        key = (CHAT,current.message_id)
        self.assertNotIn(key,enhanced.evidence)
        self.assertNotIn(key,enhanced.validities)

    async def test_method_only_source_recall_blocks_pre_and_post_coverage_and_late_use(self):
        ident = await self.learned_method()
        enhanced = self.social.life.enhanced
        store = enhanced.methods
        self.assertIsNone(enhanced.requests)
        for stage in ('before','during'):
            with self.subTest(stage=stage):
                source = self.observe('当前合成任务的可见建议资料。','visible-'+stage,
                                      user='10002',name='合成资料提供者')
                current = self.observe('请依据当前建议区分资料来源。','recipient-'+stage,name='合成当前成员')
                self.social.processing_start(current)
                view = self.social.prepare_public_context(current)
                key = (CHAT,current.message_id)
                self.assertIn(ident,[row['id'] for row in self.payload(view)['methods']])
                self.assertNotIn(key,enhanced.plans)
                self.assertNotIn(source.message_id,store.dependencies(CHAT,ident))
                before = self.coverage_calls
                draft = '这项合成建议来自资料提供者，当前成员选择采用。'
                if stage == 'during':
                    self.pause_coverage = True
                    task = asyncio.create_task(self.social.prepare_reply(current,draft))
                    await asyncio.wait_for(self.coverage_entered.wait(),2)
                self.social.recall(CHAT,source.message_id)
                self.assertFalse(view.current())
                self.assertTrue(store.current(CHAT,[ident]))  # Isolate the snapshot guard.
                if stage == 'before':
                    await self.social.prepare_reply(current,draft)
                    self.assertEqual(self.coverage_calls,before)
                else:
                    self.coverage_release.set()
                    await asyncio.wait_for(task,2)
                    self.assertEqual(self.coverage_calls,before+1)
                self.assertEqual(enhanced.coverage[key]['methods'],())
                # A receipt already travelling back from the network must not
                # turn stale visible context into a recorded method use.
                late_id = 'late-ack-'+stage
                self.social.record_self(CHAT,draft,late_id,reply_to_message_id=current.message_id)
                self.assertEqual(store.uses_for_delivery(CHAT,late_id),[])
                with self.assertRaises(PermissionError):
                    self.social.life.activities.exchange_for_delivery(CHAT,late_id)
                self.social.processing_complete(current)

    async def test_old_coverage_cannot_attach_to_a_rebound_same_message_snapshot(self):
        ident = await self.learned_method()
        current = self.observe('核对这份合成建议的来源。','same-message-rebound')
        self.social.processing_start(current)
        first = self.social.prepare_public_context(current)
        enhanced = self.social.life.enhanced
        key = (CHAT,current.message_id)
        original_validity = enhanced.validities[key]
        self.pause_coverage = True
        draft = '合成建议由提出者提供，当前成员决定采用。'
        task = asyncio.create_task(self.social.prepare_reply(current,draft))
        await asyncio.wait_for(self.coverage_entered.wait(),2)
        # Rebinding creates a new valid view under the same chat/message key.
        # The older in-flight assessment must not borrow its replacement's
        # authority even though both view predicates still return True.
        replacement = self.social.prepare_public_context(current)
        self.assertIsNot(enhanced.validities[key],original_validity)
        self.assertTrue(first.current())
        self.assertTrue(replacement.current())
        self.assertTrue(original_validity())
        self.coverage_release.set()
        await asyncio.wait_for(task,2)
        self.assertEqual(enhanced.coverage.get(key,{}).get('methods',()),())
        self.assertEqual(self.coverage_calls,1)
        self.social.record_self(CHAT,draft,'old-view-ack',reply_to_message_id=current.message_id)
        self.assertEqual(enhanced.methods.uses_for_delivery(CHAT,'old-view-ack'),[])
        self.assertEqual(enhanced.methods.method(CHAT,ident)['state'],'retained')
        self.social.processing_complete(current)

    async def test_overlong_single_draft_clears_coverage_without_hiding_the_ack(self):
        ident = await self.learned_method()
        current,view,_ = await self.reviewed_draft(ident,mid='overlong-single')
        enhanced = self.social.life.enhanced
        key = (CHAT,current.message_id)
        self.assertEqual(enhanced.coverage[key]['methods'],(ident,))
        before = self.coverage_calls
        draft = '合成正文继续说明。'*1100
        self.assertGreater(len(draft),8000)
        await self.social.prepare_reply(current,draft)
        self.assertEqual(self.coverage_calls,before)
        self.assertEqual(self.social.life.last_error,'reply_coverage_too_long')
        self.assertEqual(enhanced.coverage[key],dict(completed=(),waiting=(),methods=()))
        self.assertTrue(view.current())
        # Coverage uncertainty does not erase a channel's real delivery fact.
        self.social.record_self(CHAT,draft,'overlong-single-ack',reply_to_message_id=current.message_id)
        self.assertTrue(self.social.life.activities.exchange_for_delivery(CHAT,'overlong-single-ack'))
        self.assertEqual(enhanced.methods.uses_for_delivery(CHAT,'overlong-single-ack'),[])
        self.social.processing_complete(current)

    async def test_acknowledged_prefix_plus_tail_cannot_reuse_truncated_coverage(self):
        ident = await self.learned_method()
        current = self.observe('合成回复需要分段表达，请保留资料来源。','overlong-accumulated')
        self.social.processing_start(current)
        self.social.prepare_public_context(current)
        enhanced = self.social.life.enhanced
        key = (CHAT,current.message_id)
        prefix = '合成成员乙提出建议，合成成员甲采用。'+'合成补充。'*1000
        tail = '合成后段说明。'*450
        self.assertLessEqual(len(prefix),8000)
        self.assertLessEqual(len(tail),8000)
        self.assertGreater(len(prefix+tail),8000)
        await self.social.prepare_reply(current,prefix)
        original_invalidate = self.port.invalidate_optional
        def preserve(chat=None,*,keep_pure_image=False,preserve_source=None):
            original_invalidate(chat,keep_pure_image=keep_pure_image)
        # The transport's exact-source preservation is tested separately by
        # NapCat. Here the FakePort accepts that keyword so the real controller
        # keeps this ACK's per-turn bindings for the following fragment.
        with patch.object(self.port,'invalidate_optional',side_effect=preserve):
            self.social.record_self(CHAT,prefix,'prefix-ack',reply_to_message_id=current.message_id,source=current.source)
        self.assertEqual(enhanced.sent_parts[key],[prefix])
        self.assertEqual(len(enhanced.methods.uses_for_delivery(CHAT,'prefix-ack')),1)
        before = self.coverage_calls
        await self.social.prepare_reply(current,tail)
        self.assertEqual(self.coverage_calls,before)
        self.assertEqual(self.social.life.last_error,'reply_coverage_too_long')
        self.assertEqual(enhanced.coverage[key]['methods'],())
        self.social.record_self(CHAT,tail,'tail-ack',reply_to_message_id=current.message_id)
        self.assertTrue(self.social.life.activities.exchange_for_delivery(CHAT,'tail-ack'))
        self.assertEqual(enhanced.methods.uses_for_delivery(CHAT,'tail-ack'),[])
        self.assertEqual(len(enhanced.methods.uses(CHAT,ident)),1)
        self.social.processing_complete(current)

    async def test_later_explicit_correction_revokes_used_method_and_current_view(self):
        ident = await self.learned_method()
        request,view,draft = await self.reviewed_draft(ident)
        self.social.record_self(CHAT,draft,'method-ack',reply_to_message_id=request.message_id)
        feedback = self.observe('/social feedback method-ack correction','method-correction',user='10002')
        self.assertIn('已记录',self.social.control(feedback))
        await self.drain()
        store = self.social.life.enhanced.methods
        self.assertEqual(store.method(CHAT,ident)['state'],'reverted')
        self.assertEqual(store.method(CHAT,ident)['reason'],'contradictory_feedback')
        self.assertFalse(view.current())
        self.assertEqual(store.context(CHAT),[])
        # A contact-activity reflect may already be draining an older evidence
        # key; the regular life tick must see the newly added correction.
        await self.social.life.advance()
        await self.drain()
        self.assertEqual(self.proposal_calls,2)  # The subsequent proposal safely abstained.

    async def test_natural_quote_refinement_replied_to_corrected_revokes_same_use(self):
        ident = await self.learned_method()
        request,_,draft = await self.reviewed_draft(ident)
        self.social.record_self(CHAT,draft,'method-ack',reply_to_message_id=request.message_id)
        self.pause_feedback = True
        quoted = self.observe('这次合成答复仍需纠正建议来源的归属。','natural-correction',
                              user='10002',quote='method-ack')
        await asyncio.wait_for(self.feedback_entered.wait(),2)
        # Exercise the real race: a periodic reflect sees the quote's automatic
        # replied status before the semantic feedback classifier completes.
        self.social.life.enhanced.feedback(CHAT)
        store = self.social.life.enhanced.methods
        with store.connection() as db:
            prior = db.execute('SELECT * FROM outcomes WHERE method_id=?', (ident,)).fetchone()
        self.assertIsNotNone(prior)
        self.assertEqual(prior['status'],'replied')
        feedback_id = prior['evidence_id']
        self.feedback_release.set()
        await self.drain()
        with store.connection() as db:
            later = db.execute('SELECT * FROM outcomes WHERE method_id=? AND evidence_id=?',(ident,feedback_id)).fetchone()
        self.assertEqual(later['message_id'],quoted.message_id)
        self.assertEqual(later['status'],'corrected')
        self.assertEqual(store.method(CHAT,ident)['state'],'reverted')

    async def test_forgotten_old_method_source_revokes_derived_ack_and_backup_restore(self):
        ident = await self.learned_method()
        for index in range(70):
            self.observe('无关合成背景编号'+str(index),'background-'+str(index),user='10003')
        request,view,draft = await self.reviewed_draft(ident)
        payload = self.payload(view)
        visible = {row['message_id'] for row in payload['messages']}
        self.assertNotIn('origin-request-0',visible)
        # The source must survive as a dependency even after its raw public row
        # was evicted; otherwise later source deletion misses this new answer.
        bound = self.social.life.reply_dependencies[(CHAT,request.message_id)]
        self.assertIn('origin-request-0',bound)
        self.assertIn(ident,bound)
        self.social.record_self(CHAT,draft,'derived-method-ack',reply_to_message_id=request.message_id)
        store = self.social.life.enhanced.methods
        backup = self.home/'before-forget-methods.sqlite3'
        shutil.copyfile(store.path,backup)
        self.social.forget_messages(CHAT,{'origin-request-0'})
        self.assertFalse(view.current())
        for recent in (self.social.recent,self.social.life.recent):
            self.assertNotIn('derived-method-ack',{row.message_id for row in recent.routing_history(self.social._scope(CHAT))})
        self.assertEqual(store.method(CHAT,ident)['instruction'],'')
        await self.social.stop()
        shutil.copyfile(backup,store.path)
        self.social,self.ctx,self.port = self.create()
        restored = self.social.life.enhanced.methods
        self.assertEqual(restored.method(CHAT,ident)['state'],'reverted')
        self.assertEqual(restored.method(CHAT,ident)['instruction'],'')
        self.assertEqual(restored.context(CHAT),[])
        later = self.observe('换一个合成话题。','after-restart')
        self.assertNotIn(INSTRUCTION,self.social.prepare_public_context(later).text)

    async def test_same_scope_only_and_generation_retry_is_consumed(self):
        ident = await self.learned_method()
        other = self.observe('另一群的新合成话题。','other-request',chat=OTHER)
        view = self.social.prepare_public_context(other)
        self.assertNotIn(INSTRUCTION,view.text)
        self.assertEqual(self.social.life.enhanced.methods.context(OTHER),[])
        for _ in range(3):
            self.social.life.reflect(CHAT)
            await self.drain()
        self.assertEqual(self.proposal_calls,1)
        self.assertEqual(self.social.life.enhanced.methods.method(CHAT,ident)['state'],'retained')

    async def test_forget_during_proposal_discards_late_summary_before_storage(self):
        self.pause_proposal = True
        self.origin_feedback(0)
        self.origin_feedback(1)
        await asyncio.wait_for(self.proposal_entered.wait(),2)
        self.social.forget_messages(CHAT,{'origin-request-0'})
        self.proposal_release.set()
        await self.drain()
        self.assertEqual(self.social.life.enhanced.methods.list_methods(CHAT),[])
        self.assertEqual(self.validation_calls,0)


if __name__ == '__main__':
    unittest.main()
