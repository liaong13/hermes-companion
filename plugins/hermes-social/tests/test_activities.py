"""Activity results and feedback are synthetic; no socket, model or sender."""
import asyncio
import os
from pathlib import Path
import shutil
import tempfile
import unittest

import support
from hermes_social.activities import ActivityStore, PublishedDocument


URL = 'https://example.org/public-note'
CHAT = 'group:20001'
OTHER = 'group:20002'


class Clock:
    def __init__(self): self.now = 1800000000.0
    def __call__(self): return self.now
    def step(self, seconds=1): self.now += seconds


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.path = self.root/'activity.sqlite3'
        self.settings = dict(allowed_chats=[CHAT, OTHER, 'dm:10002'],
                             allowed_sources={CHAT: [URL], OTHER: [URL], 'dm:10002': [URL]},
                             clock=self.clock)
        self.store = ActivityStore(self.path, **self.settings)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def doc(self, url=URL, text='公开资料的合成正文。它是一份可以核对出处的说明。', **kw):
        return PublishedDocument(url, url, text, '合成公开资料', self.clock(), **kw)

    async def read(self, *, chat=CHAT, key=None, text='公开资料的合成正文。'):
        ident = self.store.queue_read(chat, URL, request_key=key)
        result = await self.store.advance(lambda url: self.doc(url, text), chat=chat)
        self.assertEqual(result[0]['state'], 'completed')
        return ident, result[0]['source_id']

    async def offer(self, *, chat=CHAT, key=None, text='公开资料的合成正文。', recipient=''):
        _, source = await self.read(chat=chat, key=key, text=text)
        reference = self.store.create_opportunity(chat, source, recipient=recipient)
        offer = self.store.next_opportunity(chat)
        self.assertEqual(offer.reference, reference)
        return source, offer

    def delivered(self, offer, mid='outbound'):
        self.assertTrue(offer.current())
        offer.claim('attempt-'+mid)
        self.assertTrue(offer.current())
        self.assertTrue(offer.record_delivery(mid))
        offer.finish('notified')
        self.assertFalse(offer.current())

    def exchange_feedback(self, mid, *, status='corrected', chat=CHAT, recipient='10002', dependencies=()):
        ref = self.store.register_exchange(chat, mid, recipient=recipient, dependencies=dependencies)
        feedback = self.store.record_feedback(chat, ref, status, actor=recipient,
            message_id='reply-'+mid, reply_to=mid)
        return ref, feedback

    def retained_trial(self, tag):
        _, original = self.exchange_feedback(tag+'-baseline')
        trial = self.store.propose_trial(CHAT, {'max_reply_chars': 280}, [original])
        self.clock.step()
        self.store.start_trial(CHAT, trial)
        self.clock.step()
        _, first = self.exchange_feedback(tag+'-one', status='replied')
        self.clock.step()
        _, second = self.exchange_feedback(tag+'-two', status='replied')
        self.assertEqual(self.store.evaluate_trial(CHAT, trial, [first, second])['state'], 'retained')
        return trial

    async def test_disabled_sources_never_fetch(self):
        empty = ActivityStore(self.root/'empty.sqlite3', clock=self.clock)
        called = []
        self.assertEqual(await empty.advance(lambda url: called.append(url)), [])
        self.assertEqual(called, [])
        with self.assertRaises(PermissionError):
            empty.queue_read(CHAT, URL)
        empty.close()

    async def test_success_records_real_result_separates_body_and_metadata(self):
        ident, source = await self.read()
        self.assertEqual(self.store.activity(CHAT, ident)['state'], 'completed')
        note = self.store.source(CHAT, source)
        self.assertEqual(note['url'], URL)
        self.assertEqual(note['fetched'], self.clock())
        with self.store.connection() as db:
            columns = {row[1] for row in db.execute('PRAGMA table_info(activities)')}
            self.assertNotIn('text', columns)
            self.assertEqual(db.execute('SELECT text FROM contents WHERE source_id=?', (source,)).fetchone()[0], '公开资料的合成正文。')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.control_path.stat().st_mode & 0o777, 0o600)

    async def test_failed_fetch_has_no_note_or_share(self):
        ident = self.store.queue_read(CHAT, URL)
        def fail(_): raise RuntimeError('SYNTHETIC_PRIVATE_ERROR_MESSAGE')
        result = await self.store.advance(fail, chat=CHAT)
        self.assertEqual(result[0]['state'], 'failed')
        self.assertEqual(self.store.notes(CHAT), [])
        self.assertIsNone(self.store.next_opportunity(CHAT))
        self.assertEqual(self.store.activity(CHAT, ident)['error_type'], 'RuntimeError')
        self.assertNotIn('SYNTHETIC_PRIVATE_ERROR_MESSAGE', self.path.read_bytes().decode(errors='ignore'))

    async def test_redirect_empty_future_or_missing_fetch_evidence_is_rejected(self):
        documents = [PublishedDocument(URL, 'https://example.org/redirect', 'body', fetched_at=self.clock()),
                     self.doc(text=''), self.doc(status_code=500),
                     PublishedDocument(URL, URL, 'body', fetched_at=self.clock()+100), {'text': 'not a receipt'}]
        for index, document in enumerate(documents):
            with self.subTest(document=document):
                self.store.queue_read(CHAT, URL, request_key='bad-'+str(index))
                result = await self.store.advance(lambda _: document, chat=CHAT)
                self.assertEqual(result[0]['state'], 'failed')
        self.assertFalse(self.store.notes(CHAT))

    async def test_exact_chat_and_source_authorization_precedes_access(self):
        _, source = await self.read()
        with self.assertRaises(PermissionError): self.store.queue_read(CHAT, 'https://example.org/other')
        with self.assertRaises(PermissionError): self.store.source(OTHER, source)
        with self.assertRaises(PermissionError): self.store.public_context('group:99999')
        self.assertEqual(self.store.public_context(OTHER), [])

    async def test_request_keys_are_idempotent_and_cannot_change_scope(self):
        first = self.store.queue_read(CHAT, URL, request_key='same')
        self.assertEqual(self.store.queue_read(CHAT, URL, request_key='same'), first)
        changed = ActivityStore(self.path, **{**self.settings, 'policy_revision': 2})
        with self.assertRaises(PermissionError): changed.queue_read(CHAT, URL, request_key='same')
        changed.close()

    async def test_pending_read_survives_restart(self):
        ident = self.store.queue_read(CHAT, URL)
        restarted = ActivityStore(self.path, **self.settings)
        result = await restarted.advance(lambda url: self.doc(url), chat=CHAT)
        self.assertEqual(result[0]['activity_id'], ident)
        self.assertEqual(result[0]['state'], 'completed')
        restarted.close()

    async def test_expired_read_lease_recovers_and_late_reader_cannot_overwrite(self):
        ident = self.store.queue_read(CHAT, URL)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(url):
            entered.set()
            await release.wait()
            return self.doc(url, '迟到的旧结果')
        job = asyncio.create_task(self.store.advance(slow, chat=CHAT))
        await entered.wait()
        self.clock.step(61)
        restarted = ActivityStore(self.path, **self.settings)
        current = await restarted.advance(lambda url: self.doc(url, '新的真实读取结果'), chat=CHAT)
        release.set()
        old = await job
        self.assertEqual(current[0]['state'], 'completed')
        self.assertEqual(old[0]['state'], 'failed')
        self.assertEqual(restarted.activity(CHAT, ident)['state'], 'completed')
        self.assertEqual(restarted.notes(CHAT)[0]['note'], '新的真实读取结果')
        restarted.close()

    async def test_cancel_during_fetch_does_not_publish_a_source(self):
        ident = self.store.queue_read(CHAT, URL)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(url):
            entered.set()
            await release.wait()
            return self.doc(url)
        job = asyncio.create_task(self.store.advance(slow, chat=CHAT))
        await entered.wait()
        self.store.cancel_read(CHAT, ident)
        release.set()
        self.assertEqual((await job)[0]['state'], 'failed')
        self.assertEqual(self.store.activity(CHAT, ident)['state'], 'cancelled')
        self.assertFalse(self.store.notes(CHAT))

    async def test_lifecycle_or_validity_change_during_fetch_is_rechecked(self):
        ident = self.store.queue_read(CHAT, URL)
        allowed = [True]
        def fetch(url):
            allowed[0] = False
            return self.doc(url)
        result = await self.store.advance(fetch, chat=CHAT, valid=lambda: allowed[0])
        self.assertEqual(result[0]['state'], 'failed')
        self.assertFalse(self.store.notes(CHAT))

    async def test_offer_has_real_url_time_excerpt_and_bounded_text(self):
        _, offer = await self.offer(text='合成资料'*5000)
        self.assertIn(URL, offer.text)
        self.assertIn('抓取时间', offer.text)
        self.assertIn('合成资料', offer.text)
        self.assertLessEqual(len(offer.text), 1000)
        self.assertEqual(offer.recipient, '')
        context = self.store.public_context(CHAT, 2400)
        self.assertEqual(context[0]['provenance'], 'public_reading_excerpt')
        self.assertNotIn('text', context[0])

    async def test_missing_delivery_id_cannot_be_called_notified(self):
        _, offer = await self.offer()
        offer.claim('attempt')
        with self.assertRaises(PermissionError): offer.finish('notified')
        offer.finish('unknown')
        self.assertFalse(offer.current())

    async def test_consumed_unknown_offer_is_not_replayed_after_main_db_restore(self):
        _, offer = await self.offer()
        backup = self.root/'before.sqlite3'
        shutil.copyfile(self.path, backup)
        offer.claim('attempt')
        offer.finish('unknown')
        shutil.copyfile(backup, self.path)
        restarted = ActivityStore(self.path, **self.settings)
        self.assertIsNone(restarted.next_opportunity(CHAT))
        restarted.close()

    async def test_content_duplicate_is_not_reshared_as_a_new_activity(self):
        _, first = await self.offer(key='first', text='同一份公开内容')
        self.delivered(first)
        _, source = await self.read(key='second', text='同一份公开内容')
        self.assertEqual(self.store.create_opportunity(CHAT, source), first.reference)
        self.assertIsNone(self.store.next_opportunity(CHAT))

    async def test_forget_revokes_offer_and_old_content_backup_cannot_restore_it(self):
        source, offer = await self.offer()
        backup = self.root/'before-forget.sqlite3'
        shutil.copyfile(self.path, backup)
        self.store.forget_source(CHAT, source)
        self.assertFalse(offer.current())
        shutil.copyfile(backup, self.path)
        restarted = ActivityStore(self.path, **self.settings)
        self.assertIsNone(restarted.next_opportunity(CHAT))
        self.assertEqual(restarted.notes(CHAT), [])
        with self.assertRaises(PermissionError): restarted.source(CHAT, source)
        restarted.close()

    async def test_feedback_requires_exact_delivery_and_target_actor(self):
        _, offer = await self.offer(recipient='10002')
        self.delivered(offer)
        for actor, reply_to in [('10003', 'outbound'), ('10002', 'another-message')]:
            with self.assertRaises(PermissionError):
                self.store.record_feedback(CHAT, offer.reference, 'replied', actor=actor,
                                           message_id='input', reply_to=reply_to)
        feedback = self.store.record_feedback(CHAT, offer.reference, 'replied', actor='10002',
                                               message_id='input', reply_to='outbound')
        self.assertEqual(self.store.record_feedback(CHAT, offer.reference, 'corrected', actor='10002',
                                                    message_id='input', reply_to='outbound'), feedback)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT status FROM feedback WHERE id=?', (feedback,)).fetchone()[0], 'corrected')
        self.store.record_feedback(CHAT, offer.reference, 'replied', actor='10002', message_id='input', reply_to='outbound')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT status FROM feedback WHERE id=?', (feedback,)).fetchone()[0], 'corrected')

    async def test_activity_observed_does_not_become_trial_evidence(self):
        _, offer = await self.offer()
        self.delivered(offer)
        feedback = self.store.record_feedback(CHAT, offer.reference, 'activity_observed', actor='10002', message_id='input')
        with self.assertRaises(PermissionError):
            self.store.propose_trial(CHAT, {'cooldown_multiplier': 2}, [feedback])
        self.assertIsNone(self.store.reflect(CHAT))

    async def test_no_response_is_not_recorded_before_verified_delivery_and_expiry(self):
        _, offer = await self.offer()
        self.delivered(offer)
        with self.assertRaises(PermissionError):
            self.store.record_feedback(CHAT, offer.reference, 'no_response')
        self.clock.step(86401)
        feedback = self.store.record_feedback(CHAT, offer.reference, 'no_response')
        self.assertTrue(feedback)

    async def test_dm_offer_cannot_change_recipient(self):
        _, source = await self.read(chat='dm:10002')
        with self.assertRaises(PermissionError):
            self.store.create_opportunity('dm:10002', source, recipient='10003')
        self.store.create_opportunity('dm:10002', source)
        self.assertEqual(self.store.next_opportunity('dm:10002').recipient, '10002')

    async def test_ordinary_exchange_uses_metadata_and_exact_quote_feedback(self):
        ref = self.store.register_exchange(CHAT, 'bot-reply', recipient='10002', reply_to='user-request', dependencies=['source-one'])
        self.assertEqual(self.store.exchange_for_delivery(CHAT, 'bot-reply'), ref)
        with self.assertRaises(PermissionError):
            self.store.record_feedback(CHAT, ref, 'corrected', actor='10003', message_id='incoming', reply_to='bot-reply')
        ident = self.store.record_feedback(CHAT, ref, 'corrected', actor='10002', message_id='incoming', reply_to='bot-reply')
        trial = self.store.propose_trial(CHAT, {'max_reply_chars': 280}, [ident])
        self.store.start_trial(CHAT, trial)
        self.assertEqual(self.store.strategy(CHAT)['max_reply_chars'], 280)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM contents').fetchone()[0], 0)

    async def test_feedback_reflection_needs_two_actual_events(self):
        _, first = self.exchange_feedback('first')
        self.assertIsNone(self.store.reflect(CHAT))
        self.clock.step()
        _, second = self.exchange_feedback('second')
        trial = self.store.reflect(CHAT)
        self.assertTrue(trial)
        self.assertEqual(set(self.store.trial(CHAT, trial)['evidence']), {first, second})
        self.store.start_trial(CHAT, trial)
        self.assertEqual(self.store.strategy(CHAT)['max_reply_chars'], 280)

    async def test_trial_cannot_change_permissions_or_exceed_finite_preferences(self):
        _, feedback = self.exchange_feedback('first')
        for patch in ({'allowed_groups': ['99999']}, {'cooldown_multiplier': 0.5}, {'max_reply_chars': 20000}, {'sharing_enabled': 'yes'}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.store.propose_trial(CHAT, patch, [feedback])
        with self.assertRaises(PermissionError):
            self.store.propose_trial(OTHER, {'max_reply_chars': 280}, [feedback])

    async def test_later_two_verified_replies_can_retain_a_trial_and_manual_rollback_works(self):
        _, original = self.exchange_feedback('baseline')
        trial = self.store.propose_trial(CHAT, {'max_reply_chars': 280}, [original])
        self.clock.step()
        self.store.start_trial(CHAT, trial)
        with self.assertRaises(PermissionError):
            self.store.evaluate_trial(CHAT, trial, [original])
        self.clock.step()
        _, first = self.exchange_feedback('after-one', status='replied')
        self.assertEqual(self.store.evaluate_trial(CHAT, trial, [first])['state'], 'trial')
        self.clock.step()
        _, second = self.exchange_feedback('after-two', status='replied')
        result = self.store.evaluate_trial(CHAT, trial, [first, second])
        self.assertEqual(result['state'], 'retained')
        self.assertEqual(self.store.strategy(CHAT)['state'], 'retained')
        self.store.revert_trial(CHAT, trial)
        self.assertEqual(self.store.strategy(CHAT)['state'], 'baseline')

    async def test_contradictory_feedback_reverts_trial(self):
        _, original = self.exchange_feedback('baseline')
        trial = self.store.propose_trial(CHAT, {'max_reply_chars': 280}, [original])
        self.clock.step()
        self.store.start_trial(CHAT, trial)
        self.clock.step()
        _, later = self.exchange_feedback('after', status='declined')
        self.assertEqual(self.store.evaluate_trial(CHAT, trial, [later])['state'], 'reverted')
        self.assertEqual(self.store.strategy(CHAT)['max_reply_chars'], 600)

    async def test_retained_trial_reverts_on_new_explicit_counterexample(self):
        for status in ('declined', 'corrected'):
            with self.subTest(status=status):
                trial = self.retained_trial(status)
                self.clock.step()
                _, later = self.exchange_feedback(status+'-later', status=status)
                self.assertEqual(self.store.evaluate_trial(CHAT, trial, [later])['state'], 'reverted')
                self.assertEqual(self.store.strategy(CHAT)['state'], 'baseline')

    async def test_retained_trial_reverts_on_verified_no_response(self):
        trial = self.retained_trial('no-response')
        _, offer = await self.offer()
        self.delivered(offer)
        self.clock.step(86401)
        feedback = self.store.record_feedback(CHAT, offer.reference, 'no_response')
        self.assertEqual(self.store.evaluate_trial(CHAT, trial, [feedback])['state'], 'reverted')
        self.assertEqual(self.store.strategy(CHAT)['state'], 'baseline')

    async def test_positive_feedback_preserves_retained_trial_lifetime_and_original_baseline(self):
        trial = self.retained_trial('positive')
        original = self.store.trial(CHAT, trial)
        self.clock.step()
        _, later = self.exchange_feedback('positive-later', status='replied')
        result = self.store.evaluate_trial(CHAT, trial, [later])
        retained = self.store.trial(CHAT, trial)
        self.assertEqual(result['state'], 'retained')
        for key in ('started', 'created', 'expires', 'baseline', 'evaluation', 'patch'):
            self.assertEqual(retained[key], original[key], key)
        self.assertIn(later, retained['evidence'])

    async def test_forget_exchange_dependency_reverts_trial_and_survives_restore(self):
        ref, feedback = self.exchange_feedback('bot', dependencies=['original-user-message'])
        trial = self.store.propose_trial(CHAT, {'max_reply_chars': 280}, [feedback])
        self.store.start_trial(CHAT, trial)
        backup = self.root/'before-exchange-forget.sqlite3'
        shutil.copyfile(self.path, backup)
        self.assertEqual(self.store.forget_messages(CHAT, ['original-user-message']), (ref,))
        self.assertEqual(self.store.strategy(CHAT)['state'], 'baseline')
        shutil.copyfile(backup, self.path)
        restored = ActivityStore(self.path, **self.settings)
        self.assertEqual(restored.strategy(CHAT)['state'], 'baseline')
        with self.assertRaises(PermissionError): restored.exchange_for_delivery(CHAT, 'bot')
        restored.close()

    async def test_forget_feedback_message_reverts_trial_and_survives_content_restore(self):
        reference, evidence = self.exchange_feedback('bot')
        trial = self.store.propose_trial(CHAT, {'max_reply_chars': 280}, [evidence])
        self.store.start_trial(CHAT, trial)
        backup = self.root/'before-feedback-forget.sqlite3'
        shutil.copyfile(self.path, backup)
        self.assertEqual(self.store.forget_messages(CHAT, ['reply-bot']), ())
        self.assertEqual(self.store.trial(CHAT, trial)['state'], 'reverted')
        self.assertEqual(self.store.feedback_evidence(CHAT), [])
        self.assertEqual(self.store.exchange_for_delivery(CHAT, 'bot'), reference)
        with self.assertRaises(PermissionError):
            self.store.record_feedback(CHAT, reference, 'corrected', actor='10002',
                                       message_id='reply-bot', reply_to='bot')
        shutil.copyfile(backup, self.path)
        restored = ActivityStore(self.path, **self.settings)
        self.assertEqual(restored.strategy(CHAT)['state'], 'baseline')
        self.assertEqual(restored.feedback_evidence(CHAT), [])
        restored.close()

    async def test_background_dependency_revocation_closes_multiple_outbound_hops_and_survives_restore(self):
        first = self.store.register_exchange(CHAT, 'derived-b', reply_to='question-r', dependencies=['background-a'])
        second, evidence = self.exchange_feedback('derived-c', dependencies=['derived-b'])
        unrelated = self.store.register_exchange(OTHER, 'derived-b', dependencies=['background-a'])
        trial = self.store.propose_trial(CHAT, {'max_reply_chars': 280}, [evidence])
        self.store.start_trial(CHAT, trial)
        backup = self.root/'before-multi-hop-forget.sqlite3'
        shutil.copyfile(self.path, backup)
        references = self.store.forget_messages(CHAT, ['background-a'])
        self.assertEqual(set(references), {first, second})
        self.assertEqual(self.store.outbound_ids(CHAT, references), ('derived-b', 'derived-c'))
        self.assertEqual(self.store.trial(CHAT, trial)['state'], 'reverted')
        self.assertEqual(self.store.exchange_for_delivery(OTHER, 'derived-b'), unrelated)
        with self.assertRaises(PermissionError):
            self.store.outbound_ids(OTHER, references)
        shutil.copyfile(backup, self.path)
        restored = ActivityStore(self.path, **self.settings)
        self.assertEqual(restored.strategy(CHAT)['state'], 'baseline')
        for mid in ('derived-b', 'derived-c'):
            with self.assertRaises(PermissionError):
                restored.exchange_for_delivery(CHAT, mid)
        with self.assertRaises(PermissionError):
            restored.register_exchange(CHAT, 'new-reply', dependencies=['derived-c'])
        restored.close()

    async def test_public_source_revocation_closes_all_outbound_hops_and_survives_restore(self):
        _, source = await self.read()
        self.store.register_exchange(CHAT, 'source-b', dependencies=['public-source:'+source])
        self.store.register_exchange(CHAT, 'source-c', dependencies=['source-b'])
        backup = self.root/'before-source-multi-hop-forget.sqlite3'
        shutil.copyfile(self.path, backup)
        self.assertEqual(self.store.forget_source(CHAT, source), ('source-b', 'source-c'))
        shutil.copyfile(backup, self.path)
        restored = ActivityStore(self.path, **self.settings)
        for mid in ('source-b', 'source-c'):
            with self.assertRaises(PermissionError):
                restored.exchange_for_delivery(CHAT, mid)
        with self.assertRaises(PermissionError):
            restored.register_exchange(CHAT, 'later-copy', dependencies=['source-c'])
        self.assertEqual(restored.forget_source(CHAT, source), ('source-b', 'source-c'))
        restored.close()

    async def test_revocation_closure_larger_than_old_input_limit_is_not_truncated(self):
        parent = 'original'
        for index in range(130):
            mid = 'chain-'+str(index)
            self.store.register_exchange(CHAT, mid, reply_to=parent)
            parent = mid
        references = self.store.forget_messages(CHAT, ['original'])
        self.assertEqual(len(references), 130)
        ids = self.store.outbound_ids(CHAT, references)
        self.assertEqual(len(ids), 130)
        self.assertEqual(set(self.store.forget_messages(CHAT, ['original', *ids])), set(references))

    async def test_outbound_id_lookup_rejects_unknown_active_and_cross_chat_references(self):
        reference = self.store.register_exchange(CHAT, 'still-active')
        with self.assertRaises(PermissionError):
            self.store.outbound_ids(CHAT, [reference])
        with self.assertRaises(PermissionError):
            self.store.outbound_ids(CHAT, ['exchange:missing'])
        self.store.forget_messages(CHAT, ['still-active'])
        with self.assertRaises(PermissionError):
            self.store.outbound_ids(OTHER, [reference])
        self.assertEqual(self.store.outbound_ids(CHAT, [reference]), ('still-active',))

    async def test_forget_activity_feedback_message_revokes_retained_trial_evidence(self):
        _, offer = await self.offer()
        self.delivered(offer, 'public-outbound')
        evidence = self.store.record_feedback(CHAT, offer.reference, 'corrected', actor='10002',
            message_id='public-feedback', reply_to='public-outbound')
        trial = self.store.propose_trial(CHAT, {'max_reply_chars': 280}, [evidence])
        self.store.start_trial(CHAT, trial)
        self.clock.step()
        _, first = self.exchange_feedback('followup-one', status='replied')
        self.clock.step()
        _, second = self.exchange_feedback('followup-two', status='replied')
        self.assertEqual(self.store.evaluate_trial(CHAT, trial, [first, second])['state'], 'retained')
        self.store.forget_messages(CHAT, ['public-feedback'])
        self.assertEqual(self.store.trial(CHAT, trial)['state'], 'reverted')
        self.assertNotIn(evidence, self.store.feedback_evidence(CHAT))
        self.assertEqual(self.store.source(CHAT, offer.source_id)['id'], offer.source_id)

    async def test_trial_expiry_reverts_without_changing_authorization(self):
        _, evidence = self.exchange_feedback('baseline')
        trial = self.store.propose_trial(CHAT, {'cooldown_multiplier': 2}, [evidence], days=1)
        self.store.start_trial(CHAT, trial)
        self.clock.step(86401)
        self.assertEqual(self.store.strategy(CHAT)['state'], 'baseline')
        self.assertEqual(self.store.trial(CHAT, trial)['state'], 'reverted')

    async def test_policy_removal_and_source_expiry_hide_notes_and_offers(self):
        _, offer = await self.offer()
        changed = ActivityStore(self.path, **{**self.settings, 'allowed_sources': {}})
        self.assertEqual(changed.notes(CHAT), [])
        self.assertIsNone(changed.next_opportunity(CHAT))
        changed.close()
        self.clock.step(31*86400)
        self.store.prune()
        self.assertEqual(self.store.notes(CHAT), [])
        self.assertFalse(offer.current())

    async def test_read_budget_is_atomic_scoped_and_survives_main_db_restore(self):
        backup = self.root/'before-budget.sqlite3'
        shutil.copyfile(self.path, backup)
        self.assertEqual(self.store.consume_read_budget(CHAT, '2027-01-01', 2), 1)
        second = ActivityStore(self.path, **self.settings)
        self.assertEqual(second.consume_read_budget(CHAT, '2027-01-01', 2), 2)
        with self.assertRaises(PermissionError): second.consume_read_budget(CHAT, '2027-01-01', 2)
        shutil.copyfile(backup, self.path)
        self.assertEqual(second.read_budget_used(CHAT, '2027-01-01'), 2)
        self.assertEqual(second.consume_read_budget(OTHER, '2027-01-01', 2), 1)
        self.assertEqual(second.consume_read_budget(CHAT, '2027-01-02', 2), 1)
        second.close()

    async def test_feedback_evidence_and_current_trials_exclude_other_chats_and_revoked_inputs(self):
        ref, original = self.exchange_feedback('baseline', dependencies=['original'])
        trial = self.store.propose_trial(CHAT, {'max_reply_chars': 280}, [original])
        self.assertEqual(self.store.current_trials(CHAT)[0]['id'], trial)
        self.assertEqual(self.store.feedback_evidence(CHAT), [original])
        self.assertEqual(self.store.feedback_evidence(OTHER), [])
        self.assertEqual(self.store.feedback_evidence(CHAT, after=self.clock()), [])
        self.store.forget_messages(CHAT, ['original'])
        self.assertEqual(self.store.feedback_evidence(CHAT), [])
        self.assertEqual(self.store.current_trials(CHAT), [])

    async def test_opportunity_cancellation_survives_content_restore(self):
        _, offer = await self.offer()
        backup = self.root/'before-cancel.sqlite3'
        shutil.copyfile(self.path, backup)
        self.store.cancel_opportunity(CHAT, offer.reference)
        self.assertFalse(offer.current())
        shutil.copyfile(backup, self.path)
        self.assertIsNone(self.store.next_opportunity(CHAT))

    async def test_reading_dependency_revokes_normal_exchange_and_derived_trial(self):
        _, source = await self.read()
        dependency = self.store.public_context(CHAT)[0]['dependency']
        _, feedback = self.exchange_feedback('cited-reply', dependencies=[dependency])
        trial = self.store.propose_trial(CHAT, {'max_reply_chars': 280}, [feedback])
        self.store.start_trial(CHAT, trial)
        backup = self.root/'before-reading-dependency.sqlite3'
        shutil.copyfile(self.path, backup)
        self.assertEqual(self.store.forget_source(CHAT, source), ('cited-reply',))
        self.assertEqual(self.store.strategy(CHAT)['state'], 'baseline')
        with self.assertRaises(PermissionError): self.store.exchange_for_delivery(CHAT, 'cited-reply')
        shutil.copyfile(backup, self.path)
        restored = ActivityStore(self.path, **self.settings)
        self.assertEqual(restored.strategy(CHAT)['state'], 'baseline')
        with self.assertRaises(PermissionError): restored.exchange_for_delivery(CHAT, 'cited-reply')
        restored.close()

    async def test_private_literal_or_credential_urls_are_not_public_sources(self):
        for index, url in enumerate(('http://example.org/', 'https://user:password@example.org/',
                                     'https://127.0.0.1/', 'https://[::1]/', 'https://localhost/',
                                     'https://example.org/\nheader')):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ActivityStore(self.root/('invalid-'+str(index)+'.sqlite3'), allowed_chats=[CHAT],
                              allowed_sources={CHAT: [url]})

    async def test_complete_snapshot_dependencies_are_retained_up_to_128(self):
        deps = ['input-'+str(index) for index in range(128)]
        reference = self.store.register_exchange(CHAT, 'outbound', dependencies=deps)
        self.assertEqual(self.store.exchange_for_delivery(CHAT, 'outbound'), reference)
        self.assertEqual(self.store.forget_messages(CHAT, [deps[-1]]), (reference,))
        with self.assertRaises(ValueError):
            self.store.register_exchange(CHAT, 'oversized', dependencies=[*deps, 'one-too-many'])

    async def test_trial_retains_original_and_full_followup_evidence(self):
        _, baseline = self.exchange_feedback('baseline')
        trial = self.store.propose_trial(CHAT, {'max_reply_chars': 280}, [baseline])
        self.clock.step()
        self.store.start_trial(CHAT, trial)
        followups = []
        for index in range(16):
            self.clock.step()
            _, feedback = self.exchange_feedback('later-'+str(index), status='replied')
            followups.append(feedback)
        self.assertEqual(self.store.evaluate_trial(CHAT, trial, followups)['state'], 'retained')
        self.assertEqual(len(self.store.trial(CHAT, trial)['evidence']), 17)
        self.assertEqual(self.store.strategy(CHAT)['state'], 'retained')

    async def test_configured_trial_duration_is_applied_and_cannot_extend_proposal(self):
        _, evidence = self.exchange_feedback('baseline')
        trial = self.store.propose_trial(CHAT, {'max_reply_chars': 280}, [evidence], days=1)
        self.store.start_trial(CHAT, trial, duration_seconds=600)
        self.assertEqual(self.store.trial(CHAT, trial)['expires'], self.clock()+600)
        self.clock.step(601)
        self.assertEqual(self.store.strategy(CHAT)['state'], 'baseline')
        second = self.store.propose_trial(CHAT, {'max_reply_chars': 300}, [evidence], days=1)
        expected = self.store.trial(CHAT, second)['expires']
        self.store.start_trial(CHAT, second, duration_seconds=604800)
        self.assertEqual(self.store.trial(CHAT, second)['expires'], expected)

    async def test_late_semantic_feedback_refinement_revokes_retained_trial(self):
        _, baseline = self.exchange_feedback('baseline')
        trial = self.store.propose_trial(CHAT, {'max_reply_chars': 280}, [baseline])
        self.clock.step()
        self.store.start_trial(CHAT, trial)
        self.clock.step()
        ref, first = self.exchange_feedback('after-one', status='replied')
        self.clock.step()
        _, second = self.exchange_feedback('after-two', status='replied')
        self.assertEqual(self.store.evaluate_trial(CHAT, trial, [first, second])['state'], 'retained')
        refined = self.store.record_feedback(CHAT, ref, 'corrected', actor='10002',
            message_id='reply-after-one', reply_to='after-one')
        self.assertEqual(refined, first)
        self.assertEqual(self.store.trial(CHAT, trial)['state'], 'reverted')
        self.assertEqual(self.store.strategy(CHAT)['state'], 'baseline')
        self.store.record_feedback(CHAT, ref, 'replied', actor='10002',
            message_id='reply-after-one', reply_to='after-one')
        self.assertEqual(self.store.trial(CHAT, trial)['state'], 'reverted')

    async def test_prune_keeps_a_recent_reading_of_an_old_scheduled_activity(self):
        scheduled = ActivityStore(self.root/'scheduled.sqlite3', **self.settings, retention_days=1)
        due = self.clock()+2*86400
        ident = scheduled.queue_read(CHAT, URL, due_at=due)
        self.clock.step(2*86400)
        result = await scheduled.advance(lambda url: self.doc(url), chat=CHAT)
        self.assertEqual(result[0]['state'], 'completed')
        scheduled.prune()
        self.assertEqual(scheduled.source(CHAT, result[0]['source_id'])['activity_id'], ident)
        scheduled.close()

    async def test_prune_releases_terminal_activity_capacity_after_expiry(self):
        bounded = ActivityStore(self.root/'bounded.sqlite3', **self.settings, max_activities=1)
        bounded.queue_read(CHAT, URL, request_key='first')
        def fail(_): raise ValueError('synthetic fetch failure')
        await bounded.advance(fail, chat=CHAT)
        with self.assertRaises(ValueError): bounded.queue_read(CHAT, URL, request_key='second')
        self.clock.step(86401)
        bounded.prune()
        self.assertTrue(bounded.queue_read(CHAT, URL, request_key='second'))
        bounded.close()

    async def test_concurrent_read_budget_claims_cannot_overspend(self):
        other = ActivityStore(self.path, **self.settings)
        stores = [self.store, other]*4
        results = await asyncio.gather(*(asyncio.to_thread(store.consume_read_budget, CHAT, '2027-01-01', 2)
                                         for store in stores), return_exceptions=True)
        self.assertEqual(sorted(item for item in results if isinstance(item, int)), [1, 2])
        self.assertEqual(sum(isinstance(item, PermissionError) for item in results), 6)
        self.assertEqual(self.store.read_budget_used(CHAT, '2027-01-01'), 2)
        other.close()

    async def test_fetch_result_after_expired_execution_lease_is_not_published(self):
        self.store.queue_read(CHAT, URL)
        def late(url):
            self.clock.step(61)
            return self.doc(url)
        result = await self.store.advance(late, chat=CHAT)
        self.assertEqual(result[0]['state'], 'failed')
        self.assertEqual(self.store.notes(CHAT), [])


if __name__ == '__main__':
    unittest.main()
