"""Verify a host patch in a disposable tracked-source checkout, never in production.

Usage: python test_llm_patch.py --host-source /path/to/hermes-agent --python /path/to/python
The canonical host runner is used; its scratch root is adapted from /var/tmp to
/tmp only inside the disposable checkout for restricted filesystem environments.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import shutil

BASE = '5894e750c3dba980d43b25aa29e3958149ba2c61'
TEST = 'tests/hermes_cli/test_memory_bound_llm.py'


def run(argv, *, cwd, env, expected=0):
    result = subprocess.run(argv, cwd=cwd, env=env, timeout=180)
    if result.returncode != expected:
        raise RuntimeError(f'command returned {result.returncode}, expected {expected}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-source', type=Path, required=True)
    parser.add_argument('--python', required=True)
    parser.add_argument('--base', default=BASE)
    parser.add_argument('--prove-red', action='store_true', help='Require the new behavior tests to fail on the unpatched base first')
    args = parser.parse_args()
    patch = Path(__file__).with_name('llm-facade.patch').resolve()
    with tempfile.TemporaryDirectory(prefix='hermes-host-patch-', dir='/tmp') as temporary:
        root = Path(temporary)
        checkout = root / 'source'
        checkout.mkdir()
        env = {**os.environ, 'GIT_NO_LAZY_FETCH': '1', 'HOME': str(root/'os-home'),
               'HERMES_HOME': str(root/'test-home'), 'TMPDIR': str(root), 'HERMES_PYTHON': args.python}
        # Never let the canonical runner interpret an unsuitable interpreter as
        # permission to provision or synchronize a dependency environment.
        run([args.python, '-c', 'import pytest'], cwd=checkout, env=env)
        (root/'os-home').mkdir()
        archive = root / 'source.tar'
        with archive.open('wb') as output:
            subprocess.run(['git', 'archive', args.base], cwd=args.host_source.resolve(), env=env,
                           stdout=output, check=True, timeout=120)
        with tarfile.open(archive) as bundle:
            bundle.extractall(checkout, filter='data')
        run(['git', 'init', '-q'], cwd=checkout, env=env)
        # Test-only runner accommodation. The product patch does not change the
        # upstream disk-backed scratch policy or require access to /var/tmp.
        runner = checkout/'scripts/run_tests_parallel.py'
        original = runner.read_text()
        needle = 'root = f"/var/tmp/{name}"'
        if needle not in original:
            raise RuntimeError('host runner scratch contract changed; review the test harness')
        runner.write_text(original.replace(needle, 'root = f"/tmp/{name}"', 1))
        run(['git', 'apply', '--check', str(patch)], cwd=checkout, env=env)
        if args.prove_red:
            run(['git', 'apply', '--include='+TEST, str(patch)], cwd=checkout, env=env)
            run(['bash', 'scripts/run_tests.sh', TEST, '-q'], cwd=checkout, env=env, expected=1)
            run(['git', 'apply', '--exclude='+TEST, str(patch)], cwd=checkout, env=env)
        else:
            run(['git', 'apply', str(patch)], cwd=checkout, env=env)
        run(['bash', 'scripts/run_tests.sh', TEST, 'tests/hermes_cli/test_plugin_host.py',
             'tests/hermes_cli/test_plugin_auxiliary_tasks.py', 'tests/agent/test_plugin_llm.py', '-q'],
            cwd=checkout, env=env)
        plugin = Path(__file__).resolve().parents[2]/'plugins'/'hermes-scoped-memory'
        staged = checkout/'scoped_plugin_contract'
        shutil.copytree(plugin, staged, ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
        # Reuse the actual plugin tests and loader, against this patched source.
        run([args.python, '-m', 'pytest', '-q', str(staged/'tests'/'test_host_binding_integration.py')],
            cwd=checkout, env={**env, 'PYTHONPATH': str(checkout)})
        print('PASS: patch applied and host behavior tests passed in a disposable checkout; production source untouched')


if __name__ == '__main__':
    main()
