"""Real pipe cleanup without requiring a namespace or an external service."""
import asyncio
import os
import sys
import unittest

from test_plugin import ROOT
from hermes_napcat.process_owner import ProcessOwner


class ProcessOwnerTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(hasattr(os, 'fork'), 'requires POSIX process inheritance')
    async def test_close_without_exchange_sends_eof_to_inherited_pipes(self):
        script = """import os, sys, time
if os.fork() == 0:
    sys.stdin.buffer.read()
    os._exit(0)
print('ready', flush=True)
time.sleep(30)
"""
        owner = ProcessOwner([sys.executable, '-I', '-c', script])
        try:
            process = await owner.start()
            self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 2), b'ready\n')
            await asyncio.wait_for(owner.close(), 2)
            self.assertIsNotNone(process.returncode)
            self.assertTrue(process.stdin.is_closing())
            self.assertTrue(process.stdout.at_eof())
        finally:
            await owner.close()
