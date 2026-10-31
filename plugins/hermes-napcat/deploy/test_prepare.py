"""Deployment tests do not log in QQ or touch the active deployment."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('prepare',Path(__file__).with_name('prepare.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
IMAGE = 'mlikiowa/napcat-docker@sha256:'+'a'*64


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='napcat-deploy-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'napcat'

    def prepare(self):
        with patch.object(module.subprocess,'run') as run, contextlib.redirect_stdout(io.StringIO()) as output:
            module.prepare(self.root,IMAGE)
            return run,output.getvalue()

    def test_permissions_tokens_and_compose(self):
        run,output=self.prepare()
        cfg=json.loads((self.root/'data/config/onebot11.json').read_text())
        web=json.loads((self.root/'data/config/webui.json').read_text())
        ws=cfg['network']['websocketServers'][0]
        self.assertTrue(ws['enable'])
        self.assertFalse(ws['reportSelfMessage'])
        self.assertGreaterEqual(len(ws['token']),40)
        self.assertNotEqual(ws['token'],web['token'])
        self.assertIn(ws['token'],(self.root/'adapter.env').read_text())
        self.assertNotIn(ws['token'],output)
        self.assertNotIn(web['token'],output)
        for file in ['adapter.env','.env','compose.yaml','data/config/onebot11.json','data/config/webui.json']:
            self.assertEqual(stat.S_IMODE((self.root/file).stat().st_mode),0o600)
        compose=(self.root/'compose.yaml').read_text()
        self.assertIn('127.0.0.1:16099:6099',compose)
        self.assertIn('127.0.0.1:13011:3001',compose)
        self.assertNotIn('privileged:',compose)
        self.assertNotIn('docker.sock',compose)
        self.assertNotIn('MODE:',compose)
        run.assert_called_once()

    def test_invalid_image_refused_before_write(self):
        for image in ['latest',IMAGE+'\nEVIL=true',IMAGE[:-1],IMAGE.upper()]:
            with self.assertRaises(SystemExit):
                module.prepare(self.root,image)
            self.assertFalse(self.root.exists())

    def test_existing_deployment_unchanged(self):
        self.prepare()
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with self.assertRaises(SystemExit):
            self.prepare()
        self.assertEqual(before,{p:p.read_bytes() for p in before})

    def test_existing_adapter_secret_refused(self):
        self.root.mkdir()
        (self.root/'adapter.env').write_text('preserved')
        with self.assertRaises(SystemExit):
            self.prepare()
        self.assertEqual((self.root/'adapter.env').read_text(),'preserved')
        self.assertFalse((self.root/'.env').exists())

    def test_broken_config_symlink_refused(self):
        self.root.mkdir()
        (self.root/'.env').symlink_to(self.root/'missing')
        with self.assertRaises(SystemExit):
            self.prepare()
        self.assertFalse((self.root/'adapter.env').exists())


if __name__=='__main__':
    unittest.main()
