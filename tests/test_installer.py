"""Synthetic installs only; never changes a running Hermes profile or contacts QQ."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO/'scripts'))
from companion_setup import install, host
from companion_setup.settings import SetupError, build_settings, PLUGINS, SKILLS
from companion_setup.transaction import Transaction, digest


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='companion-install-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root/'private'
        self.home.mkdir()
        self.host = self.root/'host'
        self.host.mkdir()
        self.config = {'model': {'default': 'existing-model', 'provider': 'existing-provider'},
                       'memory': {'provider': 'mem0'}, 'unrelated': {'keep': 'yes'}}
        self.options = dict(self_id='90001', owner_id='10001', groups='20001,20002',
                            runtime_home=str(self.root/'worker'))

    def test_shared_persona_and_daily_memory_are_explicit_and_preserved(self):
        base, *_ = build_settings(self.config, self.options, self.home)
        self.assertNotIn('shared_identity', base['platforms.napcat']['extra'])
        for option, expected in (('share_persona', {'enabled':True}),
                                 ('share_daily_memory', {'enabled':True, 'daily_memory':True})):
            updates, _, _, local = build_settings(self.config, {**self.options, option:True}, self.home)
            self.assertTrue(local)  # Shared identity also requires fresh agent/history isolation.
            self.assertEqual(updates['platforms.napcat']['extra']['shared_identity'], expected)
            self.assertNotIn('memory', updates)
            self.assertFalse(updates['plugins.entries']['hermes-life-memory']['settings']['memory']
                             .get('conversation', {}).get('enabled', False))
            current = {**self.config, 'platforms':{'napcat':updates['platforms.napcat']}}
            repeated, *_ = build_settings(current, self.options, self.home)
            self.assertEqual(repeated['platforms.napcat']['extra']['shared_identity'], expected)
        args = install.parser().parse_args(['--share-daily-memory'])
        self.assertTrue(args.share_daily_memory)

    def test_basic_install_preserves_private_provider_and_adds_no_idle_grant(self):
        updates, worker, policy, local = build_settings(self.config, self.options, self.home)
        self.assertEqual(self.config['memory']['provider'], 'mem0')
        self.assertNotIn('memory', updates)
        self.assertFalse(local)
        self.assertEqual(updates['plugins.entries']['hermes-social']['settings']['channels']['napcat']['targets'], [])
        life = updates['plugins.entries']['hermes-life-memory']['settings']['memory']
        self.assertNotIn('conversation', life)
        self.assertEqual(worker, self.root/'worker')
        self.assertEqual(set(policy), {'schema_version','mode','domain_id','policy_revision','host_contract'})

    def test_local_memory_and_proactive_contact_require_explicit_distinct_choices(self):
        options = {**self.options, 'features': 'local-memory'}
        updates, _, _, local = build_settings(self.config, options, self.home)
        self.assertTrue(local)
        memory = updates['plugins.entries']['hermes-life-memory']['settings']['memory']
        self.assertTrue(all(memory[name]['enabled'] for name in ('conversation','candidates','episodes','agreements')))
        self.assertFalse(memory['agreements'].get('proactive', False))
        with self.assertRaises(SetupError):
            build_settings(self.config, {**options, 'proactive_agreements': True}, self.home)
        updates, *_ = build_settings(self.config, {**options, 'proactive_agreements': True,
                                                   'idle_groups': '20001'}, self.home)
        self.assertTrue(updates['plugins.entries']['hermes-life-memory']['settings']['memory']['agreements']['proactive'])

    def test_target_expansion_and_parent_traversal_are_refused(self):
        with self.assertRaises(SetupError):
            build_settings(self.config, {**self.options, 'idle_groups': '30001'}, self.home)
        with self.assertRaises(SetupError):
            build_settings(self.config, {**self.options, 'runtime_home': str(self.root/'public/../private/worker')}, self.home)
        alias = self.root/'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(SetupError):
            build_settings(self.config, {**self.options, 'runtime_home': str(alias/'worker')}, self.home)

    def test_existing_feature_choice_and_explicit_owner_change_are_consistent(self):
        first, *_ = build_settings(self.config, {**self.options, 'features': 'local-memory'}, self.home)
        config = deepcopy(self.config)
        config['platforms'] = {'napcat': first['platforms.napcat']}
        config['plugins'] = {'entries': first['plugins.entries']}
        updates, _, _, local = build_settings(config, {**self.options, 'owner_id': '10003'}, self.home)
        self.assertTrue(local)
        self.assertIn('10003', updates['platforms.napcat']['extra']['allowed_users'])
        self.assertNotIn('10001', updates['platforms.napcat']['extra']['allowed_users'])
        config['platforms']['napcat']['extra']['isolation']['guest_dm_users'] = ['10004']
        with self.assertRaises(SetupError):
            build_settings(config, self.options, self.home)

    def configured(self, options=None):
        first, *_ = build_settings(self.config, {**self.options, **(options or {})}, self.home)
        return {**deepcopy(self.config), 'platforms': {'napcat': first['platforms.napcat']},
                'plugins': {'entries': first['plugins.entries']}}

    def test_friend_acl_is_independent_and_upgrade_preserves_legacy_guests(self):
        config = self.configured({'friend_users': '30001,30002', 'friend_media': True})
        extra = config['platforms']['napcat']['extra']
        self.assertEqual(extra['allowed_users'], ['10001'])
        self.assertEqual(extra['friend_dm'], dict(enabled=True, admission='explicit',
                                                users=['30001', '30002'], media=True))
        extra['allowed_users'].append('30003')
        extra['isolation']['guest_dm_users'] = ['30003']
        updates, *_ = build_settings(config, {}, self.home)
        saved = updates['platforms.napcat']['extra']
        self.assertEqual(saved['friend_dm'], extra['friend_dm'])
        self.assertEqual(saved['isolation']['guest_dm_users'], ['30003'])
        self.assertEqual(saved['allowed_users'], ['10001', '30003'])
        extra.pop('friend_dm')
        updates, *_ = build_settings(config, {}, self.home)
        self.assertNotIn('friend_dm', updates['platforms.napcat']['extra'])
        self.assertEqual(updates['platforms.napcat']['extra']['isolation']['guest_dm_users'], ['30003'])

    def test_friend_flags_never_authorize_owner_self_or_arbitrary_idle_friends(self):
        for options in ({'friend_users': '10001'}, {'friend_users': '90001'}, {'friend_media': True},
                        {'idle_friends': '30001'}, {'friend_users': '30001', 'idle_friends': '30002'}):
            with self.subTest(options=options), self.assertRaises(SetupError):
                build_settings(self.config, {**self.options, **options}, self.home)
        config = self.configured()
        config['platforms']['napcat']['extra']['friend_dm'] = dict(enabled=True, admission='verified_friends')
        with self.assertRaises(SetupError):
            build_settings(config, {'idle_friends': '30001'}, self.home)

    def test_explicit_group_member_friend_flag_never_adds_targets_or_private_acl(self):
        updates, *_ = build_settings(self.config, {**self.options, 'friend_group_members': True}, self.home)
        extra = updates['platforms.napcat']['extra']
        self.assertEqual(extra['allowed_users'], ['10001'])
        self.assertEqual(extra['friend_dm'], dict(enabled=True, allow_group_members=True))
        self.assertEqual(updates['plugins.entries']['hermes-social']['settings']['channels']['napcat']['targets'], [])
        with self.assertRaises(SetupError):
            build_settings(self.config, {**self.options, 'friend_group_members': True, 'idle_friends': '30001'}, self.home)

    def test_idle_friend_targets_preserve_existing_group_owner_and_need_exact_allowlist(self):
        config = self.configured({'friend_users': '30001,30002'})
        social = config['plugins']['entries']['hermes-social']['settings']['channels']['napcat']
        social['targets'] = ['group:20001', 'dm:10001', 'dm:30002']
        updates, *_ = build_settings(config, {'idle_friends': '30001'}, self.home)
        result = updates['plugins.entries']['hermes-social']['settings']['channels']['napcat']
        self.assertEqual(result['targets'], ['group:20001', 'dm:10001', 'dm:30001'])
        with self.assertRaises(SetupError):
            build_settings(config, {'friend_users': '30001'}, self.home)

    def test_complete_social_does_not_add_targets_sources_or_raw_chat_memory(self):
        config = self.configured()
        config['plugins']['entries']['hermes-social']['settings']['channels']['napcat']['enabled'] = False
        config['plugins']['disabled'] = ['hermes-social', 'hermes-life-memory']
        updates, _, _, local = build_settings(config, {'complete_social': True}, self.home)
        social = updates['plugins.entries']['hermes-social']['settings']['channels']['napcat']
        self.assertTrue(social['enabled'])
        self.assertEqual(social['initiative_mode'], 'model')
        self.assertEqual(social['targets'], [])
        self.assertNotIn('sources', social['activities'])
        for name in ('dialogue', 'activities', 'learning'):
            self.assertTrue(social[name]['enabled'])
        self.assertTrue(social['dialogue']['proactive'])
        self.assertTrue(social['activities']['proactive'])
        self.assertTrue(social['learning']['auto_trials'])
        self.assertFalse(local)
        memory = updates['plugins.entries']['hermes-life-memory']['settings']['memory']
        self.assertFalse(memory['enabled'])
        self.assertNotIn('conversation', memory)

    def test_enhanced_social_enables_selected_capabilities_without_collection_or_exploration(self):
        config = self.configured({'friend_users':'30001'})
        before = deepcopy(config)
        updates, _, _, filtered_history = build_settings(config, {'enhance_social':True}, self.home)
        social = updates['plugins.entries']['hermes-social']['settings']['channels']['napcat']
        memory = updates['plugins.entries']['hermes-life-memory']['settings']['memory']
        self.assertTrue(social['dialogue']['orchestration'])
        self.assertTrue(social['learning']['semantic'])
        self.assertTrue(social['learning']['semantic_auto_trials'])
        self.assertTrue(memory['explicit_memory']['enabled'])
        self.assertTrue(memory['friend_dm'])
        self.assertTrue(memory['agreements']['enabled'])
        self.assertFalse(memory.get('agreements',{}).get('proactive',False))
        self.assertFalse(memory.get('conversation',{}).get('enabled',False))
        self.assertFalse(memory.get('episodes',{}).get('enabled',False))
        self.assertTrue(filtered_history)
        self.assertTrue(updates['platforms.napcat']['extra']['local_conversation_context'])
        self.assertEqual(social['targets'],before['plugins']['entries']['hermes-social']['settings']['channels']['napcat']['targets'])
        self.assertNotIn('activities',social)
        self.assertEqual(updates['platforms.napcat']['extra']['friend_dm'],before['platforms']['napcat']['extra']['friend_dm'])
        self.assertEqual(config,before)

    def test_enhancement_does_not_admit_friends_or_enable_raw_memory_on_an_ordinary_upgrade(self):
        config = self.configured()
        updates, *_ = build_settings(config, {'enhance_social':True}, self.home)
        memory = updates['plugins.entries']['hermes-life-memory']['settings']['memory']
        self.assertFalse(memory['friend_dm'])
        config['platforms']['napcat'] = updates['platforms.napcat']
        config['plugins']['entries'] = updates['plugins.entries']
        repeated, *_ = build_settings(config, {}, self.home)
        self.assertEqual(repeated['plugins.entries'],updates['plugins.entries'])
        self.assertNotIn('friend_dm',updates['platforms.napcat']['extra'])

    def test_enhancement_cannot_restore_disabled_raw_collection_implicitly(self):
        config = self.configured({'features':'local-memory'})
        config['plugins']['disabled'] = ['hermes-life-memory','hermes-social']
        with self.assertRaises(SetupError):
            build_settings(config, {'enhance_social':True}, self.home)
        updates, *_ = build_settings(config, {'features':'basic','enhance_social':True}, self.home)
        memory = updates['plugins.entries']['hermes-life-memory']['settings']['memory']
        self.assertFalse(memory['conversation']['enabled'])
        self.assertTrue(memory['explicit_memory']['enabled'])

    def test_reading_mapping_is_exact_replacement_and_cannot_grant_chat_access(self):
        config = self.configured({'friend_users': '30001', 'idle_friends': '30001'})
        social = config['plugins']['entries']['hermes-social']['settings']['channels']['napcat']
        social['activities'] = dict(enabled=True, sources={'group:20001': ['https://old.example.invalid/']})
        path = self.root/'sources.json'
        sources = {'group:20002': ['https://example.invalid/article'], 'dm:30001': ['https://example.invalid/read']}
        path.write_text(json.dumps(sources))
        updates, *_ = build_settings(config, {'reading_sources': path, 'complete_social': True}, self.home)
        saved = updates['plugins.entries']['hermes-social']['settings']['channels']['napcat']
        self.assertEqual(saved['activities']['sources'], sources)
        self.assertEqual(saved['targets'], ['dm:30001'])
        for content in ['[]', '{"group:20002":[],"group:20002":["https://example.invalid/"]}',
                        '{"dm:30002":["https://example.invalid/"]}', '{"group:99999":["https://example.invalid/"]}',
                        '{"group:20001":["same","same"]}', 'x'*65537]:
            path.write_text(content)
            with self.subTest(content=content[:70]), self.assertRaises(SetupError):
                build_settings(config, {'reading_sources': path}, self.home)

    def test_complete_options_survive_synthetic_install_and_readback(self):
        plan, args, repo = self.plan_fixture()
        path = self.root/'sources.json'
        path.write_text(json.dumps({'dm:30001': ['https://example.invalid/read']}))
        plan['updates'], *_ = build_settings(self.config, {**self.options, 'friend_users': '30001',
            'idle_friends': '30001', 'complete_social': True, 'reading_sources': path}, self.home)
        with patch.object(install, 'helper', side_effect=self.mock_helper), \
                patch.object(install, 'run', side_effect=self.mock_cli), \
                patch.object(install, 'interpreter', return_value=Path(sys.executable)):
            result = install.apply_plan(plan, args, repo)
        self.assertEqual(result['status'], 'restart_required')
        saved = json.loads((self.home/'config.yaml').read_text())
        extra = saved['platforms']['napcat']['extra']
        self.assertEqual(extra['allowed_users'], ['10001'])
        self.assertEqual(extra['friend_dm']['users'], ['30001'])
        self.assertEqual(saved['plugins']['entries']['hermes-social']['settings']['channels']['napcat']['targets'], ['dm:30001'])
        self.assertNotIn('conversation', saved['plugins']['entries']['hermes-life-memory']['settings']['memory'])

    def test_new_cli_options_have_explicit_chinese_scope_help(self):
        args = install.parser().parse_args(['--friend-users', '30001', '--friend-media', '--friend-group-members', '--idle-friends', '30001',
                                           '--complete-social', '--enhance-social', '--reading-sources', 'scoped.json'])
        self.assertEqual(args.friend_users, '30001')
        self.assertTrue(args.friend_media)
        self.assertTrue(args.friend_group_members)
        self.assertTrue(args.complete_social)
        self.assertTrue(args.enhance_social)
        self.assertEqual(args.reading_sources, Path('scoped.json'))
        help_text = install.parser().format_help()
        self.assertIn('不加入 allowed_users', help_text)
        self.assertIn('不增加目标、来源或原始对话记忆', help_text)

    def test_upgrade_neither_expands_collection_nor_resumes_disabled_services(self):
        first, *_ = build_settings(self.config, {**self.options, 'features': 'local-memory'}, self.home)
        config = deepcopy(self.config)
        config['platforms'] = {'napcat': first['platforms.napcat']}
        config['plugins'] = {'entries': first['plugins.entries']}
        config['platforms']['napcat']['extra']['group_admission'] = 'allowed_users'
        memory = config['plugins']['entries']['hermes-life-memory']['settings']['memory']
        memory['groups'] = ['20001']
        social = config['plugins']['entries']['hermes-social']['settings']['channels']['napcat']
        social['participation_groups'] = ['20001']
        social.pop('initiative_mode')
        updates, *_ = build_settings(config, {}, self.home)
        self.assertEqual(updates['plugins.entries']['hermes-life-memory']['settings']['memory']['groups'], ['20001'])
        self.assertEqual(updates['platforms.napcat']['extra']['group_admission'], 'allowed_users')
        preserved = updates['plugins.entries']['hermes-social']['settings']['channels']['napcat']
        self.assertEqual(preserved['participation_groups'], ['20001'])
        self.assertEqual(preserved['initiative_mode'], 'model')
        memory['enabled'] = False
        config['platforms']['napcat']['extra'].pop('local_conversation_context')
        config['plugins']['entries']['hermes-social']['settings']['channels']['napcat']['enabled'] = False
        updates, _, _, local = build_settings(config, {}, self.home)
        self.assertFalse(updates['plugins.entries']['hermes-life-memory']['settings']['memory']['enabled'])
        self.assertFalse(updates['plugins.entries']['hermes-social']['settings']['channels']['napcat']['enabled'])
        self.assertFalse(local)
        config['plugins']['entries']['hermes-life-memory']['settings']['memory'].pop('enabled')
        config['plugins']['entries']['hermes-social']['settings']['channels']['napcat'].pop('enabled')
        config['platforms']['napcat']['extra'].pop('group_admission')
        updates, *_ = build_settings(config, {}, self.home)
        self.assertFalse(updates['plugins.entries']['hermes-life-memory']['settings']['memory']['enabled'])
        self.assertFalse(updates['plugins.entries']['hermes-social']['settings']['channels']['napcat']['enabled'])
        self.assertEqual(updates['platforms.napcat']['extra']['group_admission'], 'allowed_users')
        updates, *_ = build_settings(config, {'memory_groups': '20002', 'features': 'local-memory',
                                              'enable_social': True, 'follow_allowed_groups': True}, self.home)
        self.assertEqual(updates['plugins.entries']['hermes-life-memory']['settings']['memory']['groups'], ['20002'])
        self.assertNotIn('participation_groups', updates['plugins.entries']['hermes-social']['settings']['channels']['napcat'])
        # A global plugin stop must survive re-registering the matched bundle,
        # even when its old per-channel settings still say enabled.
        memory['enabled'] = True
        social['enabled'] = True
        config['plugins']['disabled'] = ['hermes-life-memory', 'hermes-social', 'other-plugin']
        updates, _, _, local = build_settings(config, {}, self.home)
        self.assertFalse(updates['plugins.entries']['hermes-life-memory']['settings']['memory']['enabled'])
        self.assertFalse(updates['plugins.entries']['hermes-social']['settings']['channels']['napcat']['enabled'])
        self.assertFalse(local)
        self.assertIn('other-plugin', updates['plugins.disabled'])
        restored, *_ = build_settings(config, {'features': 'local-memory', 'enable_social': True}, self.home)
        self.assertTrue(restored['plugins.entries']['hermes-life-memory']['settings']['memory']['enabled'])
        self.assertTrue(restored['plugins.entries']['hermes-social']['settings']['channels']['napcat']['enabled'])

    def test_unknown_local_edits_block_but_owned_upgrades_and_extra_files_survive(self):
        path = self.home/'plugins/demo/code.py'
        path.parent.mkdir(parents=True)
        path.write_text('user change')
        extra = path.with_name('notes.txt')
        extra.write_text('keep me')
        files = {'plugins/demo/code.py': b'new source'}
        with self.assertRaises(SetupError):
            install.check_targets(self.home, files, {})
        install.check_targets(self.home, files, {'files': {'plugins/demo/code.py': digest(path)}})
        self.assertEqual(extra.read_text(), 'keep me')

    def test_rollback_restores_only_owned_files_and_keeps_concurrent_edits(self):
        original = self.home/'config.yaml'
        original.write_text('old')
        original.chmod(0o640)
        added = self.home/'plugins/demo.py'
        tx = Transaction(self.home)
        tx.write(original, b'new')
        tx.write(added, b'created')
        self.assertEqual(tx.rollback(), [])
        self.assertEqual(original.read_text(), 'old')
        self.assertEqual(original.stat().st_mode & 0o777, 0o640)
        self.assertFalse(added.exists())
        tx = Transaction(self.home)
        tx.write(original, b'owned')
        original.write_text('concurrent user edit')
        self.assertEqual(tx.rollback(), [str(original)])
        self.assertEqual(original.read_text(), 'concurrent user edit')

    def test_explicit_host_must_match_launcher_binding(self):
        command = json.dumps([sys.executable, '-c', 'import sys; sys.path.insert(0, '+repr(str(self.host))+')'])
        other = self.root/'other'
        other.mkdir()
        with patch.object(host, 'run', return_value=command), self.assertRaisesRegex(SetupError, '同一宿主'):
            host.discover_host(self.home, other, 'hermes')

    def test_provenance_uses_actual_origin_without_embedded_credentials(self):
        with patch.object(install, 'run', side_effect=['a'*40, '', 'https://name:SECRET@github.com/example/fork.git']):
            sha, dirty, source = install.source_identity(self.root)
        self.assertEqual(source, 'https://github.com/example/fork.git')
        self.assertNotIn('SECRET', source)
        self.assertFalse(dirty)

    def plan_fixture(self):
        repo = self.root/'repo'
        repo.mkdir()
        files = {}
        for name in PLUGINS:
            path = repo/'plugins'/name/'__init__.py'
            path.parent.mkdir(parents=True)
            path.write_text('# synthetic\n')
            files[str(path.relative_to(repo))] = path.read_bytes()
        config = self.home/'config.yaml'
        config.write_text(json.dumps(self.config))
        (self.host/'gateway').mkdir()
        (self.host/'gateway/change.py').write_text('before\n')
        updates, worker, policy, _ = build_settings(self.config, self.options, self.home)
        plan = dict(home=self.home, host=self.host, python=Path(sys.executable), launcher='hermes',
                    updates=updates, worker=worker, worker_policy=policy, files=files, previous={},
                    host_changes={'gateway/change.py': b'after\n'}, patches={}, revision='a'*40,
                    dirty=False, token='', source='https://example.invalid/companion.git',
                    config_before=deepcopy(self.config))
        paths = [config, self.home/install.RECEIPT, worker/'scoped-memory.json',
                 self.host/'gateway/change.py', *(self.home/n for n in files)]
        plan['expected'] = {str(p): digest(p) for p in paths}
        args = install.parser().parse_args(['--apply'])
        return plan, args, repo

    def mock_helper(self, *args, **kwargs):
        operation = args[4]
        if operation == 'snapshot':
            return {'config': json.loads((self.home/'config.yaml').read_text())}
        if operation == 'status':
            return {'running': True, 'pid': 12345, 'home': str(self.home), 'napcat': 'connected'}
        return {'valid': True}

    def mock_cli(self, command, **kwargs):
        if command[1:3] == ['config', 'set']:
            path = self.home/'config.yaml'
            config = json.loads(path.read_text())
            parts = command[3].split('.')
            node = config
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = json.loads(command[4])
            path.write_text(json.dumps(config))
        return ''

    def test_apply_is_read_back_and_second_install_preserves_unrelated_data(self):
        plan, args, repo = self.plan_fixture()
        with patch.object(install, 'helper', side_effect=self.mock_helper), \
                patch.object(install, 'run', side_effect=self.mock_cli), \
                patch.object(install, 'interpreter', return_value=Path(sys.executable)):
            result = install.apply_plan(plan, args, repo)
        self.assertEqual(result['status'], 'restart_required')
        self.assertEqual((self.host/'gateway/change.py').read_text(), 'after\n')
        written = json.loads((self.home/'config.yaml').read_text())
        self.assertEqual(written['memory'], self.config['memory'])
        self.assertEqual(written['unrelated'], self.config['unrelated'])
        receipt = install.read_json(self.home/install.RECEIPT)
        install.check_targets(self.home, plan['files'], receipt)
        metadata = install.read_json(self.home/'plugins/.install-metadata.json')
        self.assertTrue(all(v['source'].endswith('#plugins/'+name) for name, v in metadata.items()))
        self.assertTrue((Path(result['backup'])/'manifest.json').is_file())

    def test_cli_failure_does_not_steal_concurrent_config_for_rollback(self):
        plan, args, repo = self.plan_fixture()
        def fail(command, **kwargs):
            path = self.home/'config.yaml'
            changed = json.loads(path.read_text())
            changed['concurrent_user_edit'] = True
            path.write_text(json.dumps(changed))
            raise SetupError('synthetic command failure')
        with patch.object(install, 'helper', side_effect=self.mock_helper), patch.object(install, 'run', side_effect=fail):
            with self.assertRaisesRegex(SetupError, '并发修改'):
                install.apply_plan(plan, args, repo)
        self.assertTrue(json.loads((self.home/'config.yaml').read_text())['concurrent_user_edit'])
        self.assertEqual((self.host/'gateway/change.py').read_text(), 'before\n')

    def test_old_connected_pid_cannot_claim_new_activation(self):
        plan, args, repo = self.plan_fixture()
        args.restart = True
        with patch.object(install, 'helper', side_effect=self.mock_helper), \
                patch.object(install, 'run', side_effect=self.mock_cli), \
                patch.object(install, 'interpreter', return_value=Path(sys.executable)), \
                patch.object(install.time, 'sleep'):
            result = install.apply_plan(plan, args, repo)
        self.assertEqual(result['status'], 'restart_required')
        self.assertNotIn('gateway_pid', result)

    def test_check_refuses_disabled_disk_config_despite_old_connected_transport(self):
        plan, args, repo = self.plan_fixture()
        plan['files'], plan['host_changes'] = {}, {}
        output = io.StringIO()
        with patch.object(install, 'make_plan', return_value=plan), \
                patch.object(install, 'helper', side_effect=self.mock_helper), \
                patch('sys.stdout', output):
            status = install.main(['--check'])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue())['status'], 'configuration_required')

    def test_check_requires_activation_evidence_not_just_connected_transport(self):
        plan, args, repo = self.plan_fixture()
        plan['files'], plan['host_changes'] = {}, {}
        for key, value in plan['updates'].items():
            node = plan['config_before']
            parts = key.split('.')
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        (self.home/install.RECEIPT).write_text(json.dumps({'needs_restart': True, 'gateway_pid_before': 12345}))
        output = io.StringIO()
        with patch.object(install, 'make_plan', return_value=plan), \
                patch.object(install, 'helper', side_effect=self.mock_helper), patch('sys.stdout', output):
            status = install.main(['--check'])
        self.assertEqual(status, 3)
        self.assertEqual(json.loads(output.getvalue())['status'], 'restart_required')

    def test_new_bound_gateway_pid_records_successful_activation(self):
        plan, args, repo = self.plan_fixture()
        args.restart = True
        states = iter([12345, 12346])
        def helper(*values, **kwargs):
            if values[4] == 'status':
                return {'running': True, 'pid': next(states), 'home': str(self.home), 'napcat': 'connected'}
            return self.mock_helper(*values, **kwargs)
        with patch.object(install, 'helper', side_effect=helper), \
                patch.object(install, 'run', side_effect=self.mock_cli), \
                patch.object(install, 'interpreter', return_value=Path(sys.executable)):
            result = install.apply_plan(plan, args, repo)
        self.assertEqual(result['status'], 'connected')
        receipt = install.read_json(self.home/install.RECEIPT)
        self.assertEqual(receipt['gateway_pid'], 12346)
        self.assertFalse(receipt['needs_restart'])

    def test_dependency_failure_restores_config_host_and_installed_files(self):
        plan, args, repo = self.plan_fixture()
        def command(argv, **kwargs):
            if argv[1:3] == ['pm', 'install']:
                raise SetupError('synthetic dependency network failure')
            return self.mock_cli(argv, **kwargs)
        with patch.object(install, 'helper', side_effect=self.mock_helper), \
                patch.object(install, 'run', side_effect=command):
            with self.assertRaisesRegex(SetupError, 'dependencies'):
                install.apply_plan(plan, args, repo)
        self.assertEqual(json.loads((self.home/'config.yaml').read_text()), self.config)
        self.assertEqual((self.host/'gateway/change.py').read_text(), 'before\n')
        self.assertFalse(any((self.home/name).exists() for name in plan['files']))

    def test_repository_inventory_has_full_sibling_api_and_installer_skill(self):
        files = install.inventory(REPO)
        for name in ['plugins/hermes-scoped-memory/api.py', 'plugins/hermes-napcat/social_memory.py',
                     'plugins/hermes-napcat/pyproject.toml', 'skills/hermes-companion-setup/SKILL.md',
                     'plugins/hermes-social/skills/social-participation/SKILL.md']:
            self.assertIn(name, files)
        self.assertFalse(any('__pycache__' in name or name.endswith('.sqlite3') for name in files))


class RealHostPatchTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('HERMES_SOURCE'), 'set HERMES_SOURCE for real host patch composition')
    def test_clean_base_and_already_patched_host_are_idempotent(self):
        live_source = Path(os.environ['HERMES_SOURCE']).resolve()
        paths = set()
        for name in host.PATCHES:
            for line in (REPO/'integration/hermes-host'/(name+'.patch')).read_text().splitlines():
                if line.startswith('+++ b/'):
                    paths.add(line[6:])
        with tempfile.TemporaryDirectory(prefix='companion-clean-host-') as directory:
            root = Path(directory)
            for name in paths:
                result = subprocess.run(['git','-C',str(live_source),'show','HEAD:'+name], capture_output=True)
                if result.returncode == 0:
                    target = root/name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(result.stdout)
            changes, states = host.patch_overlay(REPO, root, True)
            self.assertEqual(set(states), set(host.PATCHES))
            for name, data in changes.items():
                target = root/name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            repeated, states = host.patch_overlay(REPO, root, True)
            self.assertEqual(repeated, {})
            self.assertEqual(set(states.values()), {'already_applied'})


if __name__ == '__main__':
    unittest.main()
