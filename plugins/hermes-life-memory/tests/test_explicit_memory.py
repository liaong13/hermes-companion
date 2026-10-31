"""Synthetic explicit facts survive days; ordinary conversation stays uncollected."""
import importlib.util
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
NAME = 'life_explicit_test'
if NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(NAME, ROOT/'__init__.py', submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[NAME] = module
    spec.loader.exec_module(module)
from life_explicit_test.service import LifeMemoryService
from life_explicit_test.rules import explicit_candidate, explicit_request


class ExplicitMemory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='life-explicit-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.allowed = {'dm:77777', 'dm:88888'}
        self.config = dict(enabled=True, instance_id='synthetic-life', domain_id='synthetic-people', policy_revision=1,
            groups=['20001'], friend_dm=True, explicit_memory={'enabled': True, 'ttl_days': 30},
            conversation={'enabled': False}, episodes={'enabled': False}, agreements={'enabled': True})
        self.service = self.new_service()

    def new_service(self):
        service = LifeMemoryService(self.home, self.home/'data', self.config, lambda: self.config,
            channel_domain='public-test', channel_revision=1, dm_allowed=lambda chat: chat in self.allowed)
        self.addCleanup(service.close)
        return service

    def route(self, mid='1', user='77777', kind='dm'):
        return SimpleNamespace(platform='napcat', chat_type=kind, chat_id='dm:'+user if kind == 'dm' else 'group:20001',
            user_id=user, message_id=mid, domain_id='public-test', policy_revision=1, thread_id='')

    def remember(self, value='合成偏好甲', r=None):
        r = r or self.route()
        response = self.service.command(r, '/social-memory remember preference '+value)
        ident = re.search(r'review ([a-f0-9]{32})', response)[1]
        scope = self.service.scope(r)
        card = self.service.people.review(scope, ident)
        return ident, card['digest']

    def confirm(self, ident, digest, r=None):
        return self.service.command(r or self.route('2'), '/social-memory confirm '+ident+' '+digest)

    def test_explicit_mode_never_collects_ordinary_chat_or_enables_provider(self):
        self.assertFalse(self.service.observe(self.route(), '我喜欢合成普通话题', can_extract=True))
        with self.service.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sources').fetchone()[0], 0)
        self.assertFalse(self.service.config['conversation']['enabled'])
        self.assertFalse(self.service.config['episodes']['enabled'])
        self.assertFalse((self.home/'mem0.json').exists())

    def test_pending_is_not_recalled_and_confirmation_is_exact_person_and_origin(self):
        ident, digest = self.remember()
        self.assertEqual(self.service.recall(self.route('3')).facts, ())
        self.assertIn('无法处理', self.confirm(ident, digest, self.route('4', user='88888')))
        self.assertIn('无法处理', self.confirm(ident, digest, self.route('5', kind='group')))
        self.assertIn('已确认', self.confirm(ident, digest))
        self.assertEqual(self.service.recall(self.route('6', user='88888')).facts, ())
        self.assertEqual(self.service.recall(self.route('7', kind='group')).facts, ())

    def test_confirmed_fact_survives_days_and_restart_with_original_evidence(self):
        now = time.time()
        ident, digest = self.remember()
        self.confirm(ident, digest)
        self.service.close()
        self.service = self.new_service()
        with patch('time.time', return_value=now+3*86400):
            view = self.service.recall(self.route('8'))
            self.assertTrue(view.current())
            fact = view.facts[0]
            self.assertEqual(fact['content'], '交流偏好：合成偏好甲')
            self.assertEqual(fact['provenance']['evidence'][0]['message_id'], '1')
            self.assertEqual(fact['provenance']['evidence'][0]['quote'], '合成偏好甲')
            self.assertEqual(fact['provenance']['confirmation']['message_id'], '2')
            self.assertGreater(fact['provenance']['expires_at'], now+29*86400)

    def test_group_confirmed_fact_stays_in_its_group_across_days(self):
        now = time.time()
        ident, digest = self.remember('合成群内偏好', self.route(kind='group'))
        self.confirm(ident, digest, self.route('2', kind='group'))
        with patch('time.time', return_value=now+2*86400):
            self.assertEqual(self.service.recall(self.route('3', kind='group')).facts[0]['content'], '交流偏好：合成群内偏好')
            self.assertEqual(self.service.recall(self.route('4')).facts, ())
            self.assertEqual(self.service.recall(self.route('5', user='88888', kind='group')).facts, ())

    def test_only_minimal_authorized_span_is_retained(self):
        ident, _ = self.remember()
        with self.service.store.connection() as db:
            source = db.execute('SELECT user_text,assistant_text FROM sources').fetchone()
            self.assertEqual(source['user_text'], '合成偏好甲')
            self.assertEqual(source['assistant_text'], '')
        review = self.service.command(self.route('2'), '/social-memory review '+ident)
        self.assertIn('原文片段：合成偏好甲', review)
        self.assertIn('有效期至', review)
        self.assertIn('本次好友私聊', review)

    def test_explicit_natural_request_produces_existing_pending_candidate(self):
        text = '请记住：我喜欢合成纸卡工坊'
        proposal = explicit_candidate(text)
        self.assertIsNotNone(proposal)
        response = self.service.propose_explicit(self.route(), text, proposal)
        ident = re.search(r'review ([a-f0-9]{32})', response)[1]
        self.assertEqual(self.service.recall(self.route('2')).facts, ())
        scope = self.service.scope(self.route('2'))
        card = self.service.people.review(scope, ident)
        self.confirm(ident, card['digest'])
        fact = self.service.recall(self.route('3')).facts[0]
        self.assertEqual(fact['provenance']['evidence'][0]['original_start'], text.index('合成纸卡工坊'))

    def test_nonexplicit_quoted_third_person_and_fabricated_spans_are_not_stored(self):
        for text in ('我喜欢合成纸卡工坊', '小乙说请记住我喜欢合成纸卡工坊',
                     '请记住小乙喜欢合成纸卡工坊', '请记住：如果我喜欢合成纸卡工坊呢？'):
            self.assertIsNone(explicit_request(text))
        text = '请记住：我更偏好合成简短回复'
        for proposal in (dict(field='preference', value='不是原文', start=0),
                         dict(field='preference', value='请记住', start=0)):
            with self.assertRaises((ValueError, PermissionError)):
                self.service.propose_explicit(self.route(), text, proposal)
        with self.service.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sources').fetchone()[0], 0)

    def test_expiry_revokes_view_and_erases_authorized_excerpt(self):
        ident, digest = self.remember()
        self.confirm(ident, digest)
        view = self.service.recall(self.route('3'))
        with patch('time.time', return_value=time.time()+31*86400):
            self.assertFalse(view.current())
            self.assertEqual(self.service.recall(self.route('4')).facts, ())
        with self.service.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sources WHERE user_text='合成偏好甲'").fetchone()[0], 0)

    def test_forget_and_old_content_restore_cannot_reintroduce_fact_or_confirmation(self):
        ident, digest = self.remember()
        self.confirm(ident, digest)
        backup = self.home/'old-content.sqlite3'
        with sqlite3.connect(self.service.store.path) as source, sqlite3.connect(backup) as dest:
            source.backup(dest)
        self.assertIn('已忘记', self.service.command(self.route('4'), '/social-memory forget '+ident))
        with sqlite3.connect(backup) as source, sqlite3.connect(self.service.store.path) as dest:
            source.backup(dest)
        self.service.close()
        self.service = self.new_service()
        self.assertEqual(self.service.recall(self.route('5')).facts, ())
        self.assertIn('无法处理', self.confirm(ident, digest, self.route('6')))

    def test_recall_revokes_confirmed_source_and_replay_does_not_recreate(self):
        ident, digest = self.remember()
        self.confirm(ident, digest)
        view = self.service.recall(self.route('3'))
        self.service.revoke('dm:77777', '1')
        self.assertFalse(view.current())
        self.assertEqual(self.service.recall(self.route('4')).facts, ())
        self.assertIn('无法处理', self.service.command(self.route(), '/social-memory remember preference 合成偏好甲'))

    def test_current_friend_qualification_is_required_for_every_view_and_command(self):
        ident, digest = self.remember()
        self.confirm(ident, digest)
        view = self.service.recall(self.route('3'))
        self.allowed.remove('dm:77777')
        self.assertFalse(view.current())
        with self.assertRaises(PermissionError): self.service.recall(self.route('4'))
        self.assertIn('无法处理', self.service.command(self.route('5'), '/social-memory mine'))

    def test_trusted_withdrawal_deletes_after_qualification_expiry_without_read_authority(self):
        ident, digest = self.remember()
        self.confirm(ident, digest)
        view = self.service.recall(self.route('3'))
        self.service.record_reference_delivery(self.route('3'), '900', view)
        self.allowed.clear()
        with self.assertRaises(PermissionError):
            self.service.revoke('dm:77777', '1', revoker='napcat:88888')
        self.assertIn('900', self.service.revoke('dm:77777', '1', revoker='napcat:77777'))
        with self.assertRaises(PermissionError): self.service.recall(self.route('4'))
        self.allowed.add('dm:77777')
        self.assertEqual(self.service.recall(self.route('5')).facts, ())

    def test_changed_policy_retires_semantic_sources_across_restarts(self):
        ident, digest = self.remember()
        self.confirm(ident, digest)
        view = self.service.recall(self.route('3'))
        self.config['explicit_memory']['ttl_days'] = 15
        self.assertFalse(view.current())
        self.assertFalse(self.service.active)
        self.service = self.new_service()
        self.assertEqual(self.service.recall(self.route('4')).facts, ())

    def test_new_policy_revision_does_not_reuse_prior_semantic_sources(self):
        ident, digest = self.remember()
        self.confirm(ident, digest)
        self.service.close()
        self.config['policy_revision'] = 2
        self.service = self.new_service()
        self.assertEqual(self.service.recall(self.route('4')).facts, ())

    def test_explicit_plan_does_not_need_conversation_and_never_grants_dm_proactivity(self):
        from datetime import datetime, timezone
        due = datetime.fromtimestamp(time.time()+2*86400, timezone.utc).isoformat()
        result = self.service.command(self.route(), '/social-memory plan '+due+' 合成复盘约定')
        ident = re.search(r'review ([a-f0-9]{32})', result)[1]
        scope = self.service.scope(self.route('2'))
        card = self.service.relations.review(scope, ident)
        self.assertFalse(card['contact_allowed'])
        self.confirm(ident, card['digest'])
        self.service.close()
        self.service = self.new_service()
        view = self.service.recall(self.route('3'))
        self.assertEqual(view.relations[0]['content'], '约定：合成复盘约定')
        self.assertTrue(view.relations[0]['evidence'])
        self.assertFalse(self.service.config['conversation']['enabled'])
        self.service.revoke('dm:77777', '1')
        self.assertFalse(view.current())
        self.assertEqual(self.service.recall(self.route('4')).relations, ())

    def test_explicit_experience_is_pending_then_confirmed_in_exact_scope_across_days(self):
        now = time.time()
        for kind in ('group', 'dm'):
            with self.subTest(kind=kind):
                route = self.route('100', kind=kind)
                response = self.service.command(route, '/social-memory episode 我们在合成工坊核对过纸卡编号')
                ident = re.search(r'review ([a-f0-9]{32})', response)[1]
                self.assertEqual(self.service.recall(self.route('101', kind=kind)).relations, ())
                scope = self.service.scope(route)
                card = self.service.relations.review_episode(scope, ident)
                self.assertIn('无法处理', self.confirm(ident, card['digest'], self.route('102', user='88888', kind=kind)))
                self.assertIn('已确认', self.confirm(ident, card['digest'], self.route('103', kind=kind)))
                self.service.close()
                self.service = self.new_service()
                with patch('time.time', return_value=now+3*86400):
                    view = self.service.recall(self.route('104', kind=kind))
                    experience = view.relations[0]
                    self.assertEqual(experience['verification'], 'subject_confirmed_statement')
                    self.assertEqual(experience['evidence'][0]['message_id'], '100')
                    self.assertEqual(experience['evidence'][0]['quote'], '我们在合成工坊核对过纸卡编号')
                    self.assertEqual(self.service.recall(self.route('105', user='88888', kind=kind)).relations, ())
                self.service.revoke(route.chat_id, '100')
                self.assertFalse(view.current())
                self.assertEqual(self.service.recall(self.route('106', kind=kind)).relations, ())
        with self.service.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sources').fetchone()[0], 0)

    def test_natural_explicit_episode_uses_owner_review_and_expires_without_raw_capture(self):
        text = '请记住：我们在合成工坊核对过纸卡编号'
        value = '我们在合成工坊核对过纸卡编号'
        result = self.service.propose_explicit(self.route(), text,
            dict(field='episode', value=value, start=text.index(value)))
        ident = re.search(r'review ([a-f0-9]{32})', result)[1]
        scope = self.service.scope(self.route('2'))
        card = self.service.relations.review_episode(scope, ident)
        self.confirm(ident, card['digest'])
        view = self.service.recall(self.route('3'))
        with patch('time.time', return_value=time.time()+31*86400):
            self.assertFalse(view.current())
            self.assertEqual(self.service.recall(self.route('4')).relations, ())
        with self.service.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sources').fetchone()[0], 0)

    def test_semantic_delivery_tracks_only_dependencies_and_forget_suppresses_ack(self):
        ident, digest = self.remember()
        self.confirm(ident, digest)
        view = self.service.recall(self.route('3'))
        self.service.record_reference_delivery(self.route('3'), '900', view)
        self.assertNotIn('900', self.service.suppressed_messages('dm:77777'))
        self.service.command(self.route('4'), '/social-memory forget '+ident)
        self.assertIn('900', self.service.suppressed_messages('dm:77777'))
        with self.service.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sources WHERE assistant_text!=''").fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
