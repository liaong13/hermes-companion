"""Synthetic-only privacy checks, including a deleted fixture still in Git history."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('companion_portability', REPO/'scripts/check_portability.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class PortabilityTests(unittest.TestCase):
    def test_worktree_only_cannot_prove_history_clean(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root/'synthetic-repo'
            repo.mkdir()
            def git(*args):
                return subprocess.run(['git', '-C', str(repo), '-c', 'commit.gpgsign=false',
                    '-c', 'user.name='+checker.ANONYMOUS_NAME,
                    '-c', 'user.email='+checker.ANONYMOUS_EMAIL, *args],
                    check=True, capture_output=True)
            git('init')
            canary = 'SYNTHETIC_PRIVATE_CANARY'
            sample = repo/'sample.txt'
            sample.write_text(canary)
            git('add', 'sample.txt')
            git('commit', '-m', 'synthetic fixture')
            sample.write_text('synthetic clean replacement')
            git('add', 'sample.txt')
            git('commit', '-m', 'remove fixture')
            forbidden = root/'private-audit.json'
            forbidden.write_text(json.dumps([canary]))
            with patch.object(checker, 'ROOT', repo), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(checker.main(['--forbid-file',str(forbidden)]), 0)
                self.assertEqual(checker.main(['--history','--forbid-file',str(forbidden)]), 1)
            self.assertNotIn(canary, output.getvalue())
            self.assertIn('private audit term', output.getvalue())

    def test_generic_patterns_accept_synthetic_material(self):
        self.assertTrue(checker.categories('/'.join(('', 'home','SYNTHETIC_USER','file')), []))
        self.assertEqual(checker.categories('synthetic public material', []), [])

    def test_non_anonymous_metadata_is_reported_without_echoing_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            subprocess.run(['git','init',str(root)],check=True,capture_output=True)
            (root/'sample.txt').write_text('synthetic content')
            subprocess.run(['git','-C',str(root),'add','sample.txt'],check=True,capture_output=True)
            subprocess.run(['git','-C',str(root),'-c','commit.gpgsign=false',
                '-c','user.name=Synthetic Committer','-c','user.email=synthetic@example.invalid',
                'commit','-m','synthetic fixture'],check=True,capture_output=True)
            with patch.object(checker,'ROOT',root), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(checker.main(['--history']),1)
            self.assertIn('non-anonymous Git identity',output.getvalue())
            self.assertNotIn('Synthetic Committer',output.getvalue())

    def test_private_terms_cannot_be_saved_in_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            terms=root/'terms.json'
            terms.write_text('["SYNTHETIC_PRIVATE_CANARY"]')
            with patch.object(checker,'ROOT',root), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                checker.main(['--forbid-file',str(terms)])
