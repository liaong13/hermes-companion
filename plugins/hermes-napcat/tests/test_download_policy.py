"""Offline download policies: real TLS/HTTP, synthetic DNS and local TCP routing."""
import asyncio
from contextlib import ExitStack
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web

from test_plugin import NapCatAdapter, Settings, event
from test_media import media_config
from hermes_napcat.media import MediaRef, MediaService
from hermes_napcat.media_io import MediaError, MediaFetcher, public_ip


class DownloadPolicyTests(unittest.TestCase):
    def test_closed_schema_and_compatible_default(self):
        for value in (None, True, False, 1, [], {}, '', 'public', 'PUBLIC_HTTPS'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'download_policy'):
                    Settings(media_config(download_policy=value).extra)
                with self.assertRaisesRegex(ValueError, 'download_policy'):
                    MediaFetcher(download_policy=value)
        for value in (None, '', {}, [1], ['*.example.invalid']):
            with self.subTest(hosts=value), self.assertRaises(ValueError):
                Settings(media_config(download_policy='public_https', download_hosts=value).extra)
        default = MediaService(Settings(media_config().extra).media).fetcher
        self.assertEqual(default.download_policy, 'allowlist')
        with self.assertRaises(MediaError):
            default.validate_url('https://attachments.example.invalid/test.txt')
        legacy = MediaService(Settings(media_config(download_policy='allowlist',
                              download_hosts=['cdn.example.invalid']).extra).media).fetcher
        legacy.validate_url('https://cdn.example.invalid/test.txt')
        for host in ('other.example.invalid', 'cdn.example.invalid.evil', 'sub.cdn.example.invalid'):
            with self.subTest(host=host), self.assertRaises(MediaError):
                legacy.validate_url('https://'+host+'/test.txt')

    def test_public_https_without_hosts_and_no_ingress_expansion(self):
        for hosts in ({}, {'download_hosts': []}, {'download_hosts': ['old.example.invalid']}):
            adapter = NapCatAdapter(media_config(download_policy='public_https', **hosts))
            self.assertEqual(adapter.media.fetcher.download_policy, 'public_https')
            for host in ('arbitrary.example.invalid', 'files.example.net', '1.1.1.1', '[2606:4700:4700::1111]'):
                url = 'https://'+host+'/test.txt?signature=synthetic'
                self.assertEqual(adapter.media.fetcher.validate_url(url), url)
            # Chat URLs remain text; they cannot become a media request.
            raw = event(message=[{'type': 'text', 'data': {'text': 'https://arbitrary.example.invalid/test.txt'}}])
            converted = adapter.convert_event(raw)
            self.assertIsNone(converted)  # still requires a real mention
            raw['message'].insert(0, {'type': 'at', 'data': {'qq': '90001'}})
            self.assertFalse(adapter.media.wants(raw, adapter.convert_event(raw)))

    def test_public_https_rejects_unsafe_or_ambiguous_urls(self):
        fetcher = MediaFetcher(download_policy='public_https')
        urls = [
            'http://files.example.invalid/a', 'ftp://files.example.invalid/a',
            'https://files.example.invalid:8443/a', 'https://files.example.invalid:/a',
            'https://files.example.invalid:0443/a', 'https://user@files.example.invalid/a',
            'https://user:pass@files.example.invalid/a', 'https://files.example.invalid/a#',
            'https://files.example.invalid/a#fragment', 'https://files.example.invalid\\@other.invalid/a',
            'https://files.example.invalid/a\n', 'https://files.example.invalid/a\x7f',
            'https://%66iles.example.invalid/a', 'https://files.example.invalid./a',
            'https://bad..example.invalid/a', 'https://☃.example.invalid/a',
            'https://2130706433/a', 'https://0177.0.0.1/a', 'https://0x7f000001/a',
            'https://0x7f.0.0.1/a', 'https://127.1/a', 'file:///etc/passwd',
        ]
        for host in ('0.0.0.0', '127.0.0.1', '10.0.0.1', '172.16.0.1', '192.168.1.1',
                     '169.254.169.254', '100.64.0.1', '192.0.2.1', '198.51.100.1',
                     '203.0.113.1', '224.0.0.1', '240.0.0.1', '[::]', '[::1]', '[fc00::1]',
                     '[fe80::1]', '[fe80::1%25eth0]', '[ff02::1]', '[2001:db8::1]',
                     '[::ffff:127.0.0.1]', '[::ffff:1.1.1.1]', '[64:ff9b::7f00:1]',
                     '[2002:7f00:1::1]'):
            urls.append('https://'+host+'/a')
        for url in urls:
            with self.subTest(url=url), self.assertRaisesRegex(MediaError, 'url_denied'):
                fetcher.validate_url(url)
        self.assertFalse(public_ip('::ffff:1.1.1.1'))


class DownloadTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # TMPDIR is the explicitly reviewed permissions-dev directory in run_checks.
        self.temp = tempfile.TemporaryDirectory(prefix='download-policy-tls-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        cert, key = root/'certificate.pem', root/'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(key), '-out', str(cert), '-days', '1',
                        '-subj', '/CN=attachments.example.invalid',
                        '-addext', 'subjectAltName=DNS:attachments.example.invalid'],
                       check=True, capture_output=True, timeout=10)
        server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ssl.load_cert_chain(cert, key)
        self.client_ssl = ssl.create_default_context(cafile=str(cert))
        self.requests, self.connections = [], []
        self.stall = asyncio.Event()
        self.addCleanup(self.stall.set)
        app = web.Application()
        app.router.add_get('/{name}', self.serve)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, '127.0.0.1', 0, ssl_context=server_ssl)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self.url = 'https://attachments.example.invalid'
        self.service = MediaService(Settings(media_config(download_policy='public_https').extra).media)
        self.fetcher = self.service.fetcher
        self.original_connector = aiohttp.TCPConnector
        self.original_connect = aiohttp.connector.aiohappyeyeballs.start_connection

    async def serve(self, request):
        self.requests.append((request.path, dict(request.headers)))
        if request.path == '/redirect':
            raise web.HTTPFound(self.url+'/text')
        if request.path == '/stall':
            await self.stall.wait()
        if request.path == '/stream':
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b'x'*64)
            await response.write_eof()
            return response
        if request.path == '/encoded':
            return web.Response(body=b'not decoded', headers={'Content-Encoding': 'gzip'})
        if request.path == '/empty':
            return web.Response(body=b'')
        return web.Response(body=b'synthetic attachment', content_type='text/plain',
                            headers={'Set-Cookie': 'synthetic=value'})

    @staticmethod
    def rows(*hosts):
        return [dict(hostname='attachments.example.invalid', host=host, port=443,
                     family=socket.AF_INET6 if ':' in host else socket.AF_INET,
                     proto=0, flags=socket.AI_NUMERICHOST) for host in hosts]

    def network(self, *, dns=None, trust=True):
        """Keep URL/DNS/TLS/client intact; route vetted public sockets to local TLS."""
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.dns = stack.enter_context(patch('aiohttp.resolver.DefaultResolver.resolve',
            dns or AsyncMock(return_value=self.rows('1.1.1.1'))))
        async def connect(*, addr_infos, **kwargs):
            self.connections.append(addr_infos)
            self.assertTrue(all(row[4][0] == '1.1.1.1' and row[4][1] == 443 for row in addr_infos))
            local = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', self.port))]
            return await self.original_connect(addr_infos=local, **kwargs)
        stack.enter_context(patch('aiohttp.connector.aiohappyeyeballs.start_connection', side_effect=connect))
        def connector(**kwargs):
            if trust:
                kwargs['ssl'] = self.client_ssl  # verification remains CERT_REQUIRED + check_hostname
            self.assertFalse(kwargs['use_dns_cache'])
            self.assertEqual(kwargs['limit'], 2)
            return self.original_connector(**kwargs)
        stack.enter_context(patch('hermes_napcat.media_io.aiohttp.TCPConnector', side_effect=connector))
        return stack

    async def test_real_tls_download_and_dns_binding_no_proxy_cookie_or_credentials(self):
        self.network()
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:1', 'HTTP_PROXY': 'http://127.0.0.1:1',
                                    'ALL_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': '',
                                    'NAPCAT_ACCESS_TOKEN': 'synthetic-onebot', 'OPENROUTER_API_KEY': 'synthetic-model'}):
            for _ in range(2):
                ref = MediaRef('file', '5', '10001', 0, location=self.url+'/text')
                self.assertEqual(await self.service.acquire(ref, SimpleNamespace(chat_id='group:20001'), None),
                                 (b'synthetic attachment', 'text/plain'))
        self.assertEqual(self.dns.await_count, 2)
        self.assertEqual(len(self.connections), 2)
        for _, headers in self.requests:
            self.assertEqual(headers['Host'], 'attachments.example.invalid')
            self.assertEqual(headers['Accept-Encoding'], 'identity')
            for name in ('Authorization', 'Proxy-Authorization', 'Cookie'):
                self.assertNotIn(name, headers)

    async def test_mixed_dns_private_ipv6_and_rebinding_never_connect(self):
        dns = AsyncMock(side_effect=[self.rows('1.1.1.1'), self.rows('127.0.0.1'),
            self.rows('1.1.1.1', '10.0.0.1'), self.rows('1.1.1.1', '::ffff:127.0.0.1'),
            self.rows('fc00::1'), [], self.rows('not-an-ip')])
        self.network(dns=dns)
        await self.fetcher.fetch(self.url+'/text')
        for _ in range(6):
            with self.assertRaisesRegex(MediaError, 'dns_denied'):
                await self.fetcher.fetch(self.url+'/text')
        self.assertEqual(self.dns.await_count, 7)
        self.assertEqual(len(self.connections), 1)
        self.assertEqual(len(self.requests), 1)

    async def test_tls_certificate_chain_and_hostname_are_verified(self):
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        def expected_handshake_reset(loop, context):
            if (context.get('message') == 'Error on transport creation for incoming connection'
                    and isinstance(context.get('exception'), ConnectionResetError)):
                return  # The local server observes the client's intended certificate rejection.
            if previous:
                previous(loop, context)
            else:
                loop.default_exception_handler(context)
        loop.set_exception_handler(expected_handshake_reset)
        self.addCleanup(loop.set_exception_handler, previous)
        stack = self.network(trust=False)
        with self.assertRaisesRegex(MediaError, 'download_failed'):
            await self.fetcher.fetch(self.url+'/text')
        stack.close()
        self.network()
        with self.assertRaisesRegex(MediaError, 'download_failed'):
            await self.fetcher.fetch('https://other.example.invalid/text')
        self.assertEqual(self.requests, [])

    async def test_redirect_encoding_empty_size_and_real_read_timeout(self):
        self.network()
        for path, reason in (('/redirect', 'download_rejected'), ('/encoded', 'encoding_denied'),
                             ('/empty', 'empty_media')):
            with self.subTest(path=path), self.assertRaisesRegex(MediaError, reason):
                await self.fetcher.fetch(self.url+path)
        self.assertNotIn('/text', [path for path, _ in self.requests])
        small = MediaFetcher(download_policy='public_https', max_bytes=8)
        for path in ('/text', '/stream'):
            with self.subTest(path=path), self.assertRaisesRegex(MediaError, 'size_limit'):
                await small.fetch(self.url+path)
        timeout = aiohttp.ClientTimeout
        def fast_timeout(**kwargs):
            self.assertEqual(kwargs, dict(total=20, connect=5, sock_read=5))
            return timeout(total=.1, connect=.1, sock_read=.02)
        with patch('hermes_napcat.media_io.aiohttp.ClientTimeout', side_effect=fast_timeout):
            with self.assertRaisesRegex(MediaError, 'download_failed'):
                await self.fetcher.fetch(self.url+'/stall')
        self.stall.set()
