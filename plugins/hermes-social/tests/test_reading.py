"""Public-source policy and local TLS HTTP boundaries; no external network."""
import asyncio
import gzip
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web

import support
from hermes_social.reading import DocumentText, PublicReader, PublicResolver, validate_public_url

REAL_CONNECTOR = aiohttp.TCPConnector
REAL_RESOLVER = PublicResolver


class SourcePolicyTests(unittest.TestCase):
    def test_rejects_nonpublic_and_credential_or_ambiguous_urls(self):
        for url in ('http://example.org/a', 'https://127.0.0.1/a', 'https://[::1]/a',
                    'https://169.254.169.254/a', 'https://example.org:444/a',
                    'https://name:secret@example.org/a', 'https://example.org/a#private',
                    'https://example.org./a', 'https://localhost/a', 'https://example.org\\evil/a',
                    'https://%65xample.org/a', 'https://[::ffff:127.0.0.1]/a',
                    'https://@example.org/a', 'https://:@example.org/a',
                    'https://EXAMPLE.org/a', 'https://example.org/%zz'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_public_url(url)
        self.assertEqual(validate_public_url('https://example.org/a?q=public'), 'https://example.org/a?q=public')

    def test_extracts_only_visible_text_and_never_executes_markup(self):
        parser = DocumentText()
        parser.feed('<title>Public &amp; title</title><script>SECRET_SCRIPT</script>'
                    '<style>SECRET_STYLE</style><main>事实一 <b>事实二</b></main>')
        self.assertEqual(parser.text(), '事实一 事实二')
        self.assertEqual(''.join(parser.titles), 'Public & title')

    def test_nested_explicitly_hidden_nodes_do_not_enter_text_or_title(self):
        parser = DocumentText()
        parser.feed('<template><title>HIDDEN_TITLE</title><span>HIDDEN_TEMPLATE</span></template>'
                    '<title>Visible title</title><p hidden>HIDDEN_ATTR <b>HIDDEN_CHILD</b></p>'
                    '<p aria-hidden="true">HIDDEN_ARIA</p><p style="display: none !important">HIDDEN_CSS</p>'
                    '<p style="color: red; visibility:hidden;">HIDDEN_VISIBILITY</p>'
                    '<p>Visible <img hidden>tail <span>nested</span></p>')
        parser.close()
        self.assertEqual(parser.text(),'Visible tail nested')
        self.assertEqual(''.join(parser.titles),'Visible title')

    def test_reader_limits_are_explicit_and_bounded(self):
        for options in ({'max_bytes':True}, {'max_bytes':0}, {'max_bytes':4194305},
                        {'max_chars':False}, {'max_chars':-1}, {'max_chars':200001}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                PublicReader(['https://example.org/a'],**options)


class SourceNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_or_private_dns_is_rejected_before_connect(self):
        resolver = PublicResolver()
        try:
            for addresses in [('127.0.0.1',), ('93.184.216.34', '10.0.0.1')]:
                with patch.object(resolver.resolver, 'resolve', AsyncMock(return_value=[
                        {'host': value, 'port': 443, 'family': socket.AF_INET} for value in addresses])):
                    with self.assertRaises(OSError):
                        await resolver.resolve('example.org')
        finally:
            await resolver.close()

    async def test_empty_malformed_or_scoped_address_dns_is_rejected(self):
        resolver = PublicResolver()
        try:
            for rows in ([],[{'host':'not-an-address'}],[{}],[{'host':'2001:4860:4860::8888%eth0'}]):
                with patch.object(resolver.resolver,'resolve',AsyncMock(return_value=rows)):
                    with self.assertRaises(OSError):
                        await resolver.resolve('example.org')
        finally:
            await resolver.close()

    async def test_vetted_addresses_are_returned_without_a_second_lookup(self):
        resolver = PublicResolver()
        rows = [{'hostname': 'example.org', 'host': '93.184.216.34', 'port': 443,
                 'family': socket.AF_INET, 'proto': 0, 'flags': 0}]
        try:
            with patch.object(resolver.resolver, 'resolve', AsyncMock(return_value=rows)) as lookup:
                self.assertIs(await resolver.resolve('example.org'), rows)
                lookup.assert_awaited_once()
        finally:
            await resolver.close()

    async def test_unconfigured_source_never_creates_a_client(self):
        reader = PublicReader(['https://example.org/a'])
        with patch('hermes_social.reading.aiohttp.ClientSession') as client:
            with self.assertRaises(ValueError):
                await reader('https://example.org/b')
            client.assert_not_called()


class LocalResolver(aiohttp.abc.AbstractResolver):
    """Test-only connect seam: logical public HTTPS URL, local trusted TLS peer."""
    def __init__(self,port):
        self.port = port

    async def resolve(self,host,port=443,family=socket.AF_UNSPEC):
        if host != 'example.org' or port != 443:
            raise AssertionError('unexpected test authority')
        return [dict(hostname=host,host='127.0.0.1',port=self.port,
                     family=socket.AF_INET,proto=0,flags=socket.AI_NUMERICHOST)]

    async def close(self):
        pass


class PublicHTTPTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which('openssl') is None:
            raise unittest.SkipTest('local TLS boundary fixtures require openssl')
        cls.cert_dir = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        cls.cert = Path(cls.cert_dir.name)/'certificate.pem'
        cls.key = Path(cls.cert_dir.name)/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                        '-subj','/CN=example.org','-addext','subjectAltName=DNS:example.org',
                        '-keyout',str(cls.key),'-out',str(cls.cert)],check=True,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

    @classmethod
    def tearDownClass(cls):
        cls.cert_dir.cleanup()

    async def asyncSetUp(self):
        self.requests, self.connectors, self.resolvers, self.constructor_args = [],[],[],[]
        self.started, self.release = asyncio.Event(),asyncio.Event()
        self.expect_tls_reset = False
        self.loop = asyncio.get_running_loop()
        self.previous_exception_handler = self.loop.get_exception_handler()
        def exception_handler(loop,context):
            if (self.expect_tls_reset and context.get('message') == 'Error on transport creation for incoming connection'
                    and isinstance(context.get('exception'),ConnectionResetError)):
                return  # Expected local peer reset when the client rejects our self-signed certificate.
            if self.previous_exception_handler:
                self.previous_exception_handler(loop,context)
            else:
                loop.default_exception_handler(context)
        self.loop.set_exception_handler(exception_handler)
        self.handler = self.success
        async def route(request):
            self.requests.append((request.path,dict(request.headers)))
            return await self.handler(request)
        app = web.Application()
        app.router.add_route('*','/{tail:.*}',route)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_tls.load_cert_chain(self.cert,self.key)
        self.site = web.TCPSite(self.runner,'127.0.0.1',0,ssl_context=server_tls)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.client_tls = ssl.create_default_context(cafile=str(self.cert))
        self.patches = [patch('hermes_social.reading.PublicResolver',self.make_resolver),
                        patch('hermes_social.reading.aiohttp.TCPConnector',self.make_connector)]
        for entry in self.patches:
            entry.start()

    async def asyncTearDown(self):
        self.release.set()
        for connector in self.connectors:
            if not connector.closed:
                await connector.close()
        await self.runner.cleanup()
        self.loop.set_exception_handler(self.previous_exception_handler)
        for entry in reversed(self.patches):
            entry.stop()

    def make_resolver(self):
        resolver = REAL_RESOLVER()
        resolver.close = AsyncMock(wraps=resolver.close)
        self.resolvers.append(resolver)
        return resolver

    def make_connector(self,**kwargs):
        self.constructor_args.append(kwargs.copy())
        # Do not alter production PublicResolver. Only this test's connector
        # maps the already-validated URL to the ephemeral local HTTPS fixture.
        kwargs['resolver'] = LocalResolver(self.port)
        kwargs['ssl'] = self.client_tls
        connector = REAL_CONNECTOR(**kwargs)
        self.connectors.append(connector)
        return connector

    async def success(self,request):
        return web.Response(text='<title>Public &amp; title</title><main>事实一 <b>事实二</b></main>'
                            '<script>HIDDEN_SCRIPT</script><div hidden>HIDDEN_ELEMENT</div>',content_type='text/html')

    def reader(self,**kwargs):
        return PublicReader(['https://example.org/public'],**kwargs)

    def assert_closed(self):
        self.assertTrue(self.connectors)
        self.assertTrue(all(connector.closed for connector in self.connectors))
        for resolver in self.resolvers:
            resolver.close.assert_awaited_once()

    async def test_successful_200_returns_real_visible_text_and_source(self):
        import time
        before = time.time()
        document = await self.reader()('https://example.org/public')
        self.assertEqual(document.requested_url,'https://example.org/public')
        self.assertEqual(document.url,document.requested_url)
        self.assertEqual(document.title,'Public & title')
        self.assertEqual(document.text,'事实一 事实二')
        self.assertEqual(document.status_code,200)
        self.assertLessEqual(before,document.fetched_at)
        self.assertLessEqual(document.fetched_at,time.time())
        self.assertEqual(self.constructor_args[0]['use_dns_cache'],False)
        self.assertNotIn('ssl',self.constructor_args[0])
        self.assertEqual(self.requests[0][1]['Accept-Encoding'],'identity')
        self.assert_closed()

    async def test_plaintext_charset_and_character_limit(self):
        async def plain(request):
            return web.Response(body='公开资料甲乙丙'.encode('gb18030'),
                                headers={'Content-Type':'text/plain; charset=gb18030'})
        self.handler = plain
        document = await self.reader(max_chars=4)('https://example.org/public')
        self.assertEqual(document.text,'公开资料')
        self.assertEqual(document.title,'example.org')
        self.assert_closed()

    async def test_redirect_statuses_are_rejected_without_following(self):
        for status in (301,302,303,307,308):
            async def redirect(request):
                return web.Response(status=status,headers={'Location':'https://example.org/redirected'})
            self.handler = redirect
            with self.subTest(status=status), self.assertRaises(ValueError):
                await self.reader()('https://example.org/public')
        self.assertEqual(len(self.requests),5)
        self.assertEqual({path for path,_ in self.requests},{'/public'})
        self.assert_closed()

    async def test_encoded_and_unsupported_responses_are_rejected(self):
        cases = [({'Content-Encoding':'gzip','Content-Type':'text/plain'},gzip.compress(b'published text')),
                 ({'Content-Encoding':'br','Content-Type':'text/plain'},b'not-brotli'),
                 ({'Content-Type':'application/json'},b'{"public":"data"}'),
                 ({'Content-Type':'application/pdf'},b'%PDF fake'),
                 ({'Content-Type':'image/png'},b'fake png'),
                 ({'Content-Type':'text/plain; charset=utf-16'},'text'.encode('utf-16'))]
        for headers,body in cases:
            async def unsupported(request):
                return web.Response(body=body,headers=headers)
            self.handler = unsupported
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                await self.reader()('https://example.org/public')
        self.assert_closed()

    async def test_declared_oversize_is_rejected(self):
        async def large(request):
            return web.Response(body=b'x'*129,content_type='text/plain')
        self.handler = large
        with self.assertRaises(ValueError):
            await self.reader(max_bytes=128)('https://example.org/public')
        self.assert_closed()

    async def test_chunked_oversize_is_rejected_without_content_length(self):
        async def chunks(request):
            response = web.StreamResponse(headers={'Content-Type':'text/plain'})
            response.enable_chunked_encoding()
            await response.prepare(request)
            try:
                await response.write(b'x'*64)
                await asyncio.sleep(0)
                await response.write(b'y'*65)
                await response.write_eof()
            except ConnectionError:
                pass
            return response
        self.handler = chunks
        with self.assertRaises(ValueError):
            await self.reader(max_bytes=128)('https://example.org/public')
        self.assert_closed()

    async def test_non_200_and_empty_text_are_not_documents(self):
        for status,body in ((404,b'missing'),(500,b'failure'),(200,b'<script>hidden</script>')):
            async def bad(request):
                return web.Response(status=status,body=body,content_type='text/html')
            self.handler = bad
            with self.subTest(status=status), self.assertRaises(ValueError):
                await self.reader()('https://example.org/public')
        self.assertEqual(len(self.requests),3)
        self.assert_closed()

    async def test_proxy_netrc_and_response_cookies_are_not_forwarded(self):
        async def cookie(request):
            response = web.Response(text='公开资料')
            response.set_cookie('session','SYNTHETIC_COOKIE_CANARY')
            return response
        self.handler = cookie
        netrc = Path(self.cert_dir.name)/'test.netrc'
        netrc.write_text('machine example.org login SYNTHETIC_USER password SYNTHETIC_SECRET\n')
        with patch.dict(os.environ,{'HTTPS_PROXY':'http://127.0.0.1:1','NETRC':str(netrc)}):
            for _ in range(2):
                await self.reader()('https://example.org/public')
        for _,headers in self.requests:
            self.assertNotIn('Authorization',headers)
            self.assertNotIn('Proxy-Authorization',headers)
            self.assertNotIn('Cookie',headers)
        self.assert_closed()

    async def streaming(self,request):
        response = web.StreamResponse(headers={'Content-Type':'text/plain'})
        await response.prepare(request)
        await response.write(b'published start')
        self.started.set()
        await self.release.wait()
        try:
            await response.write(b' published finish')
            await response.write_eof()
        except ConnectionError:
            pass
        return response

    async def test_cancellation_closes_resources_and_releases_slot(self):
        self.handler = self.streaming
        reader = self.reader()
        task = asyncio.create_task(reader('https://example.org/public'))
        await asyncio.wait_for(self.started.wait(),2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_closed()
        self.release.set()
        self.handler = self.success
        document = await asyncio.wait_for(reader('https://example.org/public'),2)
        self.assertEqual(document.text,'事实一 事实二')

    async def test_queued_cancellation_does_not_create_a_second_client(self):
        self.handler = self.streaming
        reader = self.reader()
        first = asyncio.create_task(reader('https://example.org/public'))
        await asyncio.wait_for(self.started.wait(),2)
        second = asyncio.create_task(reader('https://example.org/public'))
        await asyncio.sleep(0)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        self.assertEqual(len(self.resolvers),1)
        self.release.set()
        await asyncio.wait_for(first,2)
        self.assert_closed()

    async def test_timeout_closes_resources(self):
        self.handler = self.streaming
        timeout_class = aiohttp.ClientTimeout
        with patch('hermes_social.reading.aiohttp.ClientTimeout',
                   side_effect=lambda **kwargs:timeout_class(total=0.1,connect=0.05,sock_read=0.05)):
            with self.assertRaises(TimeoutError):
                await self.reader()('https://example.org/public')
        self.assert_closed()

    async def test_tls_certificate_verification_remains_enabled(self):
        self.expect_tls_reset = True
        self.client_tls = ssl.create_default_context()
        with self.assertRaises(aiohttp.ClientConnectorCertificateError):
            await self.reader()('https://example.org/public')
        self.assert_closed()

    async def test_connector_constructor_failure_still_closes_resolver(self):
        with patch('hermes_social.reading.aiohttp.TCPConnector',side_effect=RuntimeError('synthetic constructor failure')):
            with self.assertRaises(RuntimeError):
                await self.reader()('https://example.org/public')
        self.assertEqual(len(self.resolvers),1)
        self.resolvers[0].close.assert_awaited_once()

    async def test_session_constructor_failure_closes_connector_and_resolver(self):
        with patch('hermes_social.reading.aiohttp.ClientSession',side_effect=RuntimeError('synthetic session failure')):
            with self.assertRaises(RuntimeError):
                await self.reader()('https://example.org/public')
        self.assert_closed()
