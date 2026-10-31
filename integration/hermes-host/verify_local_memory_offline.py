#!/usr/bin/env python3
"""Verify the local-memory host increment in a disposable source overlay.

Copies only four gateway source files, applies the patch there and imports all
other host modules read-only. Never copies profiles or modifies the actual host.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host-source', type=Path, required=True)
    parser.add_argument('--python', type=Path, required=True)
    args = parser.parse_args()
    host, python = args.host_source.resolve(strict=True), args.python.expanduser().absolute()
    if not python.is_file():
        raise ValueError('configured Python interpreter is unavailable')
    companion = Path(__file__).resolve().parents[2]
    patch = Path(__file__).with_name('napcat-local-memory-context.patch')
    with tempfile.TemporaryDirectory(prefix='companion-local-host-', dir=os.environ['TMPDIR']) as scratch:
        overlay = Path(scratch)
        (overlay/'gateway').mkdir()
        for name in ('__init__.py', 'run.py', 'run_turn_runner.py', 'session_execution.py'):
            (overlay/'gateway'/name).write_bytes((host/'gateway'/name).read_bytes())
        # A test-only overlay package: remaining modules are read from the
        # explicitly selected host tree, not copied or rewritten.
        init = overlay/'gateway/__init__.py'
        init.write_text(init.read_text()+'\n__path__.append('+repr(str(host/'gateway'))+')\n')
        (overlay/'tests').symlink_to(host/'tests', target_is_directory=True)
        (overlay/'gateway/run_turn.py').symlink_to(host/'gateway/run_turn.py')
        subprocess.run(['git','apply','--check',str(patch)], cwd=overlay, check=True)
        subprocess.run(['git','apply',str(patch)], cwd=overlay, check=True)
        env = dict(os.environ, HERMES_SOURCE=str(overlay), HERMES_DISABLE_LAZY_INSTALLS='1',
                   PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=os.pathsep.join((str(overlay),str(host))))
        commands = (
            ['-m','unittest','discover','-s','plugins/hermes-napcat/tests','-p','test_local_memory.py','-v'],
            ['-m','unittest','discover','-s','plugins/hermes-napcat/tests','-p','test_local_contacts.py','-v'],
            ['-m','pytest','-q','integration/hermes-host/test_napcat_local_memory_context.py'],
        )
        for command in commands:
            subprocess.run([str(python), *command], cwd=companion, env=env, check=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
