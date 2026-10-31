"""Synthetic orchestration protocol: no LLM, network or real conversations."""
import copy
import json
import unittest

import support
from support import event
from hermes_social.context import ContextRecord, ContextScope
from hermes_social.participation import ParticipationFrame
from hermes_social.requests import ConditionSnapshot, RequestSnapshot


def relation(action='new',target='c0',conditions=(),supersedes=()):
    return dict(action=action,target=target,conditions=list(conditions),supersedes=list(supersedes))


def assessment(candidate='c0',*,request=None,topic=None,action='respond',reason='question'):
    return dict(candidate=candidate,audience='group',action=action,reason=reason,
                topic=topic or candidate,topic_action='continue',
                request=relation(target=candidate) if request is None else request)


def verdict(*rows,selected='c0',covers=('c0',),mode='single',defer=()):
    return dict(selected=selected,assessments=list(rows or [assessment()]),
                reply_plan=dict(covers=list(covers),mode=mode,defer=list(defer)))


class RequestProtocolTests(unittest.TestCase):
    def setUp(self):
        self.scope=ContextScope('/tmp/synthetic-request-protocol','napcat','group:20001')
        self.now=10000.0
        self.a=event('合成任务甲需要分配三组纸卡。',mid='new-a',user='10001',name='同名成员')
        self.b=event('我的独立合成任务乙需要分配两组木片。',mid='new-b',user='10002',name='同名成员')

    def row(self,mid,actor='10001',text='一条独立编写的合成任务资料',*,scope=None,at=None):
        return ContextRecord(scope or self.scope,self.now-20 if at is None else at,
                             'member','同名成员',text,mid,sender_id=actor)

    def request(self,root='root-a',owner='10001',conditions=()):
        return RequestSnapshot(root,owner,root,'pending',1,(root,*[item.source_id for item in conditions]),
                               tuple(conditions),self.now+1000)

    def frame(self,events=None,rows=(),requests=(),*,orchestration=True):
        return ParticipationFrame(events or [self.a],rows,self.scope,now=self.now,
                                  requests=requests,orchestration=orchestration)

    @staticmethod
    def decode(frame,value):
        return frame.parse(json.dumps(value,ensure_ascii=False))

    def test_two_new_requests_preserve_distinct_real_owners_and_combined_roots(self):
        frame=self.frame([self.a,self.b])
        value=verdict(assessment(),assessment('c1'),selected='c1',covers=['c0','c1'],mode='combined')
        choice,items=self.decode(frame,value)
        self.assertIs(choice.event,self.b)
        self.assertEqual(choice.reply_plan.covers,('new-a','new-b'))
        self.assertEqual(choice.reply_plan.mode,'combined')
        self.assertNotEqual(frame.payload['candidates'][0]['speaker'],frame.payload['candidates'][1]['speaker'])
        self.assertEqual([(item.candidate,item.request.target) for item in items],[('c0','c0'),('c1','c1')])

    def test_contributor_supplement_and_owner_adoption_keep_source_metadata(self):
        condition=ConditionSnapshot('condition-b','10002','supplement')
        existing=self.request(conditions=[condition])
        rows=[self.row('root-a'),self.row('condition-b','10002')]
        frame=self.frame([self.a],rows,[existing])
        root=next(ref for ref,mid in frame.ids.items() if mid=='root-a')
        source=next(ref for ref,mid in frame.ids.items() if mid=='condition-b')
        payload=frame.payload['requests'][0]
        self.assertNotEqual(payload['requester'],payload['conditions'][0]['provider'])
        value=verdict(assessment(request=relation('adopt',root,[source]),topic=root),covers=[root])
        choice,items=self.decode(frame,value)
        self.assertEqual(items[0].request.conditions,(source,))
        self.assertEqual(choice.reply_plan.covers,('root-a',))
        supplement=self.frame([self.b],rows,[existing])
        root=next(ref for ref,mid in supplement.ids.items() if mid=='root-a')
        choice,items=self.decode(supplement,verdict(assessment(request=relation('supplement',root),topic=root),covers=[root]))
        self.assertEqual(items[0].request.action,'supplement')
        self.assertIs(choice.event,self.b)

    def test_new_request_can_explicitly_reference_an_earlier_foreign_condition(self):
        rows=[self.row('condition-b','10002')]
        frame=self.frame(rows=rows)
        source=next(ref for ref,mid in frame.ids.items() if mid=='condition-b')
        _,items=self.decode(frame,verdict(assessment(request=relation('new','c0',[source]))))
        self.assertEqual(items[0].request.conditions,(source,))

    def test_new_request_cannot_choose_a_previous_member_as_its_owner(self):
        frame=self.frame(rows=[self.row('old-b','10002')])
        old=next(ref for ref,mid in frame.ids.items() if mid=='old-b')
        with self.assertRaises(ValueError):
            self.decode(frame,verdict(assessment(request=relation('new',old))))

    def test_unbound_and_other_scope_conditions_are_rejected(self):
        other=ContextScope(self.scope.profile_home,'napcat','group:20002')
        frame=self.frame(rows=[self.row('cross-group','10002',scope=other)])
        self.assertNotIn('cross-group',frame.ids.values())
        for ref in ('cross-group','missing','h999'):
            with self.subTest(ref=ref),self.assertRaises(ValueError):
                self.decode(frame,verdict(assessment(request=relation('new','c0',[ref]))))

    def test_condition_and_target_cannot_point_into_future_candidate(self):
        frame=self.frame([self.a,self.b])
        for request in (relation('new','c0',['c1']),relation('supplement','c1')):
            value=verdict(assessment(request=request),assessment('c1'),selected='c1',covers=['c1'])
            with self.subTest(request=request),self.assertRaises(ValueError):
                self.decode(frame,value)

    def test_a_historical_message_is_not_automatically_a_request_target(self):
        frame=self.frame(rows=[self.row('observation-b','10002')])
        ref=next(ref for ref,mid in frame.ids.items() if mid=='observation-b')
        with self.assertRaises(ValueError):
            self.decode(frame,verdict(assessment(request=relation('continue',ref)),covers=[ref]))

    def test_plan_cannot_cover_a_non_request_or_duplicate_roots(self):
        frame=self.frame(rows=[self.row('background','10002')])
        background=next(ref for ref,mid in frame.ids.items() if mid=='background')
        for covers in ([background],['c0','c0'],['unknown']):
            with self.subTest(covers=covers),self.assertRaises(ValueError):
                self.decode(frame,verdict(covers=covers))

    def test_no_selected_source_cannot_plan_a_reply(self):
        frame=self.frame()
        with self.assertRaises(ValueError):
            self.decode(frame,verdict(assessment(action='wait'),selected=None,covers=['c0']))
        choice,items=self.decode(frame,verdict(assessment(action='wait'),selected=None,covers=[],defer=['c0']))
        self.assertIsNone(choice)
        self.assertEqual(items[0].action,'wait')

    def test_covers_and_defer_cannot_overlap(self):
        frame=self.frame()
        with self.assertRaises(ValueError):
            self.decode(frame,verdict(covers=['c0'],defer=['c0']))

    def test_selected_earlier_source_cannot_cover_a_later_new_request(self):
        frame=self.frame([self.a,self.b])
        with self.assertRaises(ValueError):
            self.decode(frame,verdict(assessment(),assessment('c1'),selected='c0',covers=['c0','c1'],mode='combined'))

    def test_order_of_assessment_rows_does_not_change_earlier_source_relationships(self):
        frame=self.frame([self.a,self.b])
        later=assessment('c1',request=relation('supplement','c0'),topic='c0')
        earlier=assessment()
        choice,items=self.decode(frame,verdict(later,earlier,selected='c1',covers=['c0']))
        self.assertEqual(choice.reply_plan.covers,('new-a',))
        self.assertEqual({item.candidate for item in items},{'c0','c1'})

    def test_request_fields_and_reply_plan_required_only_when_enabled(self):
        frame=self.frame()
        value=verdict()
        value['assessments'][0].pop('request')
        with self.assertRaises(ValueError):
            self.decode(frame,value)
        value=verdict()
        value.pop('reply_plan')
        with self.assertRaises(ValueError):
            self.decode(frame,value)
        legacy=self.frame(orchestration=False)
        old=dict(selected='c0',assessments=[{key:value for key,value in assessment().items() if key!='request'}])
        choice,items=self.decode(legacy,old)
        self.assertIs(choice.event,self.a)
        self.assertIsNone(choice.reply_plan)
        self.assertIsNone(items[0].request)
        with self.assertRaises(ValueError):
            self.decode(legacy,verdict())

    def test_plan_ref_mapping_does_not_expose_message_ids_as_model_authority(self):
        existing=self.request()
        frame=self.frame(rows=[self.row('root-a')],requests=[existing])
        root=next(ref for ref,mid in frame.ids.items() if mid=='root-a')
        self.assertEqual(frame.payload['requests'][0]['ref'],root)
        self.assertNotEqual(root,'root-a')
        value=verdict(assessment(request=relation('continue',root),topic=root),covers=[root],mode='clarify')
        choice,_=self.decode(frame,value)
        self.assertEqual(choice.reply_plan.covers,('root-a',))
        clone=copy.copy(self.a)
        clone.source=copy.copy(self.a.source)
        self.assertFalse(choice.matches(clone))


if __name__=='__main__':
    unittest.main()
