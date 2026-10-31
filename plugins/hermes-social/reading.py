"""Read exact configured public HTTPS documents without ambient credentials."""
from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import ipaddress
import re
import socket
import time
from urllib.parse import urlsplit

import aiohttp
from yarl import URL


def public_address(value):
    if not isinstance(value,str) or '%' in value:
        return False
    address = ipaddress.ip_address(value)
    mapped = getattr(address, 'ipv4_mapped', None)
    if mapped is not None:
        address = mapped
    return address.is_global and not address.is_multicast and not address.is_unspecified


def validate_public_url(url):
    if not isinstance(url, str) or len(url) > 2048 or re.search(r'[\s\x00-\x1f\\]', url):
        raise ValueError('invalid public source URL')
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment or '%' in parsed.netloc
                or parsed.hostname != parsed.hostname.encode('idna').decode('ascii')
                or str(URL(url)) != url):
            raise ValueError('invalid public source authority')
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            if '.' not in parsed.hostname or parsed.hostname.endswith('.'):
                raise ValueError('invalid public source host') from None
        else:
            if not public_address(parsed.hostname):
                raise ValueError('nonpublic source address')
    except (UnicodeError, ValueError):
        raise ValueError('invalid public source URL') from None
    return url


class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self.resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=443, family=socket.AF_UNSPEC):
        rows = await self.resolver.resolve(host, port, family)
        try:
            valid = bool(rows) and all(public_address(row['host']) for row in rows)
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise OSError('public source DNS rejected')
        return rows

    async def close(self):
        await self.resolver.close()


class DocumentText(HTMLParser):
    BLOCKED = frozenset({'script','style','svg','noscript','template','iframe','object','embed','canvas'})
    VOID = frozenset({'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.titles = [], []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = [(name.lower(),value) for name,value in attrs]
        style = ';'.join(value for name,value in attrs if name == 'style' and value)
        hidden = bool(self.stack and self.stack[-1][1]) or tag in self.BLOCKED
        hidden = (hidden or any(name == 'hidden' or name == 'aria-hidden' and (value or '').lower() == 'true'
                                for name,value in attrs)
                  or bool(re.search(r'(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)',
                                    style,re.IGNORECASE)))
        if tag not in self.VOID:
            self.stack.append((tag,hidden))

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1,-1,-1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack and self.stack[-1][1]:
            return
        if any(tag == 'title' for tag,_ in self.stack):
            self.titles.append(data)
        else:
            self.parts.append(data)

    def text(self):
        return re.sub(r'\s+', ' ', ' '.join(self.parts)).strip()


class PublicReader:
    def __init__(self, sources, *, max_bytes=524288, max_chars=20000):
        if (not isinstance(sources,(list,tuple,set,frozenset))
                or type(max_bytes) is not int or not 1 <= max_bytes <= 4194304
                or type(max_chars) is not int or not 1 <= max_chars <= 200000):
            raise ValueError('invalid public reader bounds')
        self.sources = frozenset(validate_public_url(url) for url in sources)
        self.max_bytes, self.max_chars = max_bytes, max_chars
        self._slots = asyncio.Semaphore(1)

    async def __call__(self, url):
        if validate_public_url(url) not in self.sources:
            raise ValueError('source is not configured')
        from .activities import PublishedDocument
        async with self._slots:
            resolver = PublicResolver()
            connector = None
            try:
                connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False, limit=1)
                async with aiohttp.ClientSession(connector=connector, trust_env=False,
                        auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar(),
                        timeout=aiohttp.ClientTimeout(total=20, connect=5, sock_read=10),
                        headers={'Accept': 'text/html,text/plain,application/xhtml+xml',
                                 'Accept-Encoding': 'identity', 'User-Agent': 'HermesCompanion/0.4'}) as client:
                    async with client.get(url, allow_redirects=False) as response:
                        if (response.status != 200 or str(response.url) != url
                                or response.headers.get('Content-Encoding', 'identity').lower() != 'identity'
                                or response.content_type not in {'text/plain', 'text/html', 'application/xhtml+xml'}):
                            raise ValueError('public document response rejected')
                        if response.content_length is not None and response.content_length > self.max_bytes:
                            raise ValueError('public document too large')
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(16384):
                            body.extend(chunk)
                            if len(body) > self.max_bytes:
                                raise ValueError('public document too large')
                        charset = response.charset or 'utf-8'
                        if charset.lower() not in {'utf-8', 'utf8', 'ascii', 'iso-8859-1', 'windows-1252', 'gb18030', 'gbk'}:
                            raise ValueError('unsupported public document encoding')
                        text = body.decode(charset, errors='replace')
                        title = urlsplit(url).hostname
                        if response.content_type != 'text/plain':
                            parser = DocumentText()
                            parser.feed(text)
                            parser.close()
                            text = parser.text()
                            title = re.sub(r'\s+', ' ', ' '.join(parser.titles)).strip()[:200] or title
                        text = text.strip()[:self.max_chars]
                        if not text:
                            raise ValueError('public document has no text')
                        return PublishedDocument(requested_url=url, url=url, title=title,
                                                 text=text, fetched_at=time.time(), status_code=200)
            finally:
                try:
                    if connector is not None and not connector.closed:
                        await connector.close()
                finally:
                    await resolver.close()
