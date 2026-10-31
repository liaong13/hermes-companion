# pyright: reportMissingImports=false
import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support
from support import FakeAdapter, FakeContext, FakePort
from hermes_social import register
from hermes_social.migration import migrate_napcat_state_v1
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.home = Path(self.temp.name)
        self.token = set_hermes_home_override(self.home)
        self.port, self.ctx = FakePort(self.home), FakeContext()
        self.source = self.home / 'napcat/social-state.json'
        self.destination = self.home / 'plugin-data/hermes-social/napcat/social-state.json'
        self.snapshot = {'schema_version': 1, 'chats': {'group:20001': {
            'last_attempt': 10, 'last_check': 20, 'unanswered': True,
            'paused': False, 'day': '2026-10-29', 'daily': 2}}}
        self.status_patch = patch('gateway.status.live_gateway_pid_for_home', return_value=None)
        self.status = self.status_patch.start()

    async def asyncTearDown(self):
        self.ctx.unload()
        await asyncio.sleep(0)
        for task in self.ctx.tasks:
            await asyncio.gather(task, return_exceptions=True)
        if self.port.controller and self.port.controller.binding.shutdown_task:
            await self.port.controller.binding.shutdown_task
        self.status_patch.stop()
        reset_hermes_home_override(self.token)
        self.temp.cleanup()

    def legacy(self, data=None, mode=0o600):
        self.source.parent.mkdir(mode=0o700, exist_ok=True)
        self.source.write_text(json.dumps(self.snapshot if data is None else data))
        self.source.chmod(mode)

    async def test_absent_source_creates_nothing_and_does_not_check_live_gateway(self):
        self.assertEqual(migrate_napcat_state_v1(self.port), 'absent')
        self.status.assert_not_called()
        self.assertFalse((self.home / 'plugin-data').exists())

    async def test_atomic_copy_preserves_source_and_private_modes(self):
        self.legacy()
        content = self.source.read_bytes()
        self.assertEqual(migrate_napcat_state_v1(self.port), 'migrated')
        self.status.assert_called_once_with(self.home)
        self.assertEqual(json.loads(self.destination.read_text()), self.snapshot)
        self.assertEqual(self.source.read_bytes(), content)
        for path in [self.destination.parent, self.destination.parent.parent, self.home / 'plugin-data']:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.destination.parent.glob('.state-migration-*')), [])

    async def test_existing_destination_never_overwritten_even_if_source_is_bad(self):
        self.legacy(mode=0o644)
        self.destination.parent.mkdir(mode=0o700, parents=True)
        self.destination.write_text('CURRENT_TARGET_CANARY')
        self.assertEqual(migrate_napcat_state_v1(self.port), 'exists')
        self.assertEqual(self.destination.read_text(), 'CURRENT_TARGET_CANARY')
        self.status.assert_not_called()

    async def test_racing_destination_is_not_replaced(self):
        self.legacy()
        actual_link = os.link
        def race(src, dst, **kwargs):
            Path(dst).write_text('RACING_TARGET_CANARY')
            return actual_link(src, dst, **kwargs)
        with patch('hermes_social.migration.os.link', side_effect=race):
            self.assertEqual(migrate_napcat_state_v1(self.port), 'exists')
        self.assertEqual(self.destination.read_text(), 'RACING_TARGET_CANARY')
        self.assertEqual(list(self.destination.parent.glob('.state-migration-*')), [])

    async def test_another_live_gateway_refused_without_creating_destination(self):
        self.legacy()
        self.status.return_value = os.getpid() + 1
        with self.assertRaises(RuntimeError):
            migrate_napcat_state_v1(self.port)
        self.assertFalse((self.home / 'plugin-data').exists())
        self.assertEqual(json.loads(self.source.read_text()), self.snapshot)

    async def test_this_gateway_pid_is_allowed(self):
        self.legacy()
        self.status.return_value = os.getpid()
        self.assertEqual(migrate_napcat_state_v1(self.port), 'migrated')

    async def test_registration_does_not_copy_and_attach_uses_latest_stopped_source(self):
        self.ctx.migrate = True
        self.legacy()
        register(self.ctx)
        self.assertFalse(self.destination.exists())
        self.snapshot['chats']['group:20001'].update(daily=3, paused=True, last_attempt=123)
        self.legacy()
        adapter = FakeAdapter(self.port)
        controller = self.ctx.handlers['napcat'](None, adapter)
        state = controller.states['group:20001']
        self.assertEqual((state.daily, state.paused, state.last_attempt), (3, True, 123))
        self.assertTrue(state.unanswered)
        self.assertEqual(json.loads(self.destination.read_text()), self.snapshot)
        controller.binding.close()
        await asyncio.sleep(0)
        if controller.binding.shutdown_task:
            await controller.binding.shutdown_task

    async def test_migration_is_explicit_and_boolean(self):
        self.legacy()
        register(self.ctx)
        controller = self.ctx.handlers['napcat'](None, FakeAdapter(self.port))
        self.assertFalse(self.destination.exists())
        self.assertFalse(controller.states['group:20001'].unanswered)
        self.status.assert_not_called()
        self.ctx.migrate = 'true'
        with self.assertRaises(ValueError):
            self.ctx.handlers['napcat'](None, FakeAdapter(FakePort(self.home)))

    async def test_private_or_unknown_fields_and_bad_types_refused(self):
        invalid = []
        for key, value in [('text', 'PRIVATE_CANARY'), ('rows', []), ('daily', True),
                           ('paused', 'true'), ('last_attempt', float('nan')), ('last_check', -1)]:
            data = copy.deepcopy(self.snapshot)
            data['chats']['group:20001'][key] = value
            invalid.append(data)
        invalid += [dict(self.snapshot, messages=['PRIVATE_CANARY']), {'schema_version': True, 'chats': {}},
                    {'schema_version': 1, 'chats': {'group:020001': {}}}]
        for data in invalid:
            with self.subTest(data=data):
                self.legacy(data)
                with self.assertRaises(ValueError):
                    migrate_napcat_state_v1(self.port)
                self.assertFalse(self.destination.exists())
        self.assertFalse((self.home / 'plugin-data').exists())

    async def test_insecure_nonregular_symlink_oversized_and_wrong_owner_refused(self):
        self.legacy(mode=0o644)
        with self.assertRaises(ValueError):
            migrate_napcat_state_v1(self.port)
        self.source.unlink()
        self.source.symlink_to(self.home / 'missing')
        with self.assertRaises(ValueError):
            migrate_napcat_state_v1(self.port)
        self.source.unlink()
        self.source.mkdir()
        with self.assertRaises(ValueError):
            migrate_napcat_state_v1(self.port)
        self.source.rmdir()
        self.legacy()
        self.source.write_bytes(b' ' * (128 * 1024 + 1))
        with self.assertRaises(ValueError):
            migrate_napcat_state_v1(self.port)
        self.legacy()
        with patch('hermes_social.migration.os.getuid', return_value=os.getuid() + 1):
            with self.assertRaises(ValueError):
                migrate_napcat_state_v1(self.port)
        self.assertFalse((self.home / 'plugin-data').exists())

    async def test_cross_profile_migration_refused_before_source_read_or_write(self):
        self.legacy()
        token = set_hermes_home_override(self.home / 'another-profile')
        try:
            with self.assertRaises(ValueError):
                migrate_napcat_state_v1(self.port)
        finally:
            reset_hermes_home_override(token)
        self.status.assert_not_called()
        self.assertFalse((self.home / 'plugin-data').exists())
