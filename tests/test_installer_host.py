"""Actual Hermes CLI/parser and bubblewrap, only in disposable homes.

The fixture borrows prepared dependencies and stubs PM provisioning. It never
starts a gateway, reuses an account, connects QQ, or modifies the real host.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO/'scripts'))
from companion_setup.host import PATCHES


@unittest.skipUnless(os.environ.get('HERMES_SOURCE') and os.environ.get('HERMES_PYTHON')
                     and os.environ.get('HERMES_LAUNCHER'),
                     'set HERMES_SOURCE, HERMES_PYTHON and HERMES_LAUNCHER for isolated SDK integration')
class HostInstallTests(unittest.TestCase):
    def test_fresh_full_install_and_repeat_use_real_cli_and_worker(self):
        source = Path(os.environ['HERMES_SOURCE']).resolve()
        python = Path(os.environ['HERMES_PYTHON']).absolute()
        launcher = Path(os.environ['HERMES_LAUNCHER']).absolute()
        with tempfile.TemporaryDirectory(prefix='companion-sdk-install-') as directory:
            root = Path(directory)
            home, overlay = root/'profile', root/'host'
            home.mkdir(mode=0o700)
            overlay.mkdir()
            # The real CLI boots through PM even for config commands. Borrow the
            # already prepared generation under this disposable owner's state;
            # never provision or rewrite the real install's dependency graph.
            key = hashlib.sha256(str(source).encode()).hexdigest()[:16]
            state = home/'installs'/key
            state.mkdir(parents=True)
            prepared = python.parent.parent
            (state/'environments').symlink_to(prepared.parent.parent, target_is_directory=True)
            (state/'facts.json').write_text(json.dumps({'packages': {'venv': {'environment': str(prepared)}}}))
            targets = set()
            for name in PATCHES:
                for line in (REPO/'integration/hermes-host'/(name+'.patch')).read_text().splitlines():
                    if line.startswith('+++ b/'):
                        targets.add(line[6:])
            for name in ('agent', 'gateway', 'tests'):
                package = overlay/name
                package.mkdir()
                init = source/name/'__init__.py'
                text = init.read_text() if init.is_file() else ''
                (package/'__init__.py').write_text(text+'\n__path__.append('+repr(str(source/name))+')\n')
            for path in source.iterdir():
                if path.name in ('agent', 'gateway', 'tests') or path.name.startswith('.'):
                    continue
                if path.is_dir() or path.suffix == '.py':
                    (overlay/path.name).symlink_to(path, target_is_directory=path.is_dir())
            for name in targets:
                path = overlay/name
                if path.is_symlink():
                    path.unlink()
                result = subprocess.run(['git','-C',str(source),'show','HEAD:'+name], capture_output=True)
                if result.returncode == 0:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(result.stdout)
            env = dict(os.environ, HERMES_HOME=str(home), HERMES_DISABLE_LAZY_INSTALLS='1',
                       PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(source))
            env.pop('NAPCAT_ACCESS_TOKEN', None)
            defaults = subprocess.run([str(python), '-B', '-c',
                'from hermes_cli.config import DEFAULT_CONFIG; import json; print(json.dumps(DEFAULT_CONFIG))'],
                env=env, capture_output=True, text=True, check=True)
            config = json.loads(defaults.stdout)
            config['model'] = {'default': 'synthetic-model', 'provider': 'custom',
                               'base_url': 'http://127.0.0.1:9/v1'}
            config['memory']['provider'] = 'mem0'
            (home/'config.yaml').write_text(json.dumps(config))
            token_file = root/'token'
            token_file.write_text('SYNTHETIC_TOKEN_NEVER_PRINT')
            token_file.chmod(0o600)
            facade = root/'hermes-fixture'
            facade.write_text('#!'+sys.executable+'\n'+
                'import json, os, sys\n'+
                "if sys.argv[1:] == ['--print-runtime-command']:\n"+
                " print(json.dumps(["+repr(str(python))+", '-c', "+
                repr('import sys; sys.path.insert(0, '+repr(str(overlay))+')')+"]))\n"+
                "elif sys.argv[1:4] == ['pm', 'install', 'python']:\n"+
                " print('fixture: existing prepared dependency generation')\n"+
                'else:\n os.execv('+repr(str(launcher))+', ['+repr(str(launcher))+', *sys.argv[1:]])\n')
            facade.chmod(0o700)
            command = [sys.executable, '-B', str(REPO/'scripts/install.py'), '--home', str(home),
                       '--host-source', str(overlay), '--hermes', str(facade), '--python', str(python),
                       '--self-id', '90001', '--owner-id', '10001', '--groups', '20001',
                       '--runtime-home', str(root/'worker'), '--features', 'local-memory',
                       '--token-file', str(token_file)]
            plan = subprocess.run(command, env=env, capture_output=True, text=True, timeout=90)
            self.assertEqual(plan.returncode, 0, plan.stdout+plan.stderr)
            self.assertFalse((home/'plugins').exists())
            first = subprocess.run([*command, '--apply'], env=env, capture_output=True, text=True, timeout=180)
            if first.returncode != 3:
                # Every value in this fixture is synthetic; private production logs are never printed.
                for log in (home/'backups/hermes-companion').glob('*/command-error.txt'):
                    self.fail(first.stdout+'\n'+log.read_text())
            self.assertEqual(first.returncode, 3, first.stdout+first.stderr)
            report = json.loads(first.stdout)
            self.assertTrue(report['worker_verified'])
            self.assertEqual(report['status'], 'restart_required')
            self.assertNotIn('SYNTHETIC_TOKEN_NEVER_PRINT', first.stdout+first.stderr)
            self.assertIn('public_memory_context_contract = 1', (overlay/'gateway/run_turn_runner.py').read_text())
            readback = subprocess.run([str(python), '-B', '-c',
                'from hermes_yaml import safe_load; from pathlib import Path; import os,json; '
                'print(json.dumps(safe_load((Path(os.environ["HERMES_HOME"])/"config.yaml").read_text())))'],
                env=env, capture_output=True, text=True, check=True)
            actual = json.loads(readback.stdout)
            self.assertEqual(actual['memory']['provider'], 'mem0')
            self.assertTrue(actual['platforms']['napcat']['extra']['local_conversation_context'])
            config_mtime = (home/'config.yaml').stat().st_mtime_ns
            repeated = subprocess.run([*command, '--apply'], env=env, capture_output=True, text=True, timeout=180)
            self.assertEqual(repeated.returncode, 3, repeated.stdout+repeated.stderr)
            self.assertEqual((home/'config.yaml').stat().st_mtime_ns, config_mtime)
            self.assertFalse((home/'gateway.pid').exists())


if __name__ == '__main__':
    unittest.main()
