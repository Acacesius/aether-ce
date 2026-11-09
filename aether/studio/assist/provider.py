"""Draft provider seam. ONE implementation: an chat-completions (Ollama or llama.cpp style) endpoint on a loopback address.

* The URL must be ``http://`` with a literal loopback address or a name; every address the name resolves to is
  re-checked at connect time and the socket is opened to the checked address (no resolve/connect race).
* Redirects are refused, the request and response are size- and time-bounded, no credentials are sent or read.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
from urllib.parse import urlsplit

URL_ENV = 'AETHER_STUDIO_ASSIST_URL'
MODEL_ENV = 'AETHER_STUDIO_ASSIST_MODEL'
TIMEOUT_SECONDS = 60.0
MAX_RESPONSE_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 48 * 1024


class ProviderError(Exception):
    """A provider failure with a stable short code and a one-line message that never echoes response content."""

    def __init__(self, code: str, detail: str):
        super().__init__(f'{code}: {detail}')
        self.code = code
        self.detail = detail


class DraftProvider:
    """Interface: turn a chat prompt into raw model text. Implementations must not execute or interpret the text."""
    name = 'abstract'
    model = ''

    def draft(self, text: str, context: dict) -> str:
        """``text`` is the user message, ``context['system']`` the system prompt, optional ``context['history']``
        earlier ``{"role", "content"}`` turns (re-ask). Returns the model's raw text."""
        raise NotImplementedError


def is_loopback(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split('%', 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def system_resolver(host: str, port: int) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


def checked_addresses(host: str, port: int, resolver) -> list[str]:
    addresses = list(resolver(host, port))
    if not addresses:
        raise ProviderError('host_unresolved', 'assist host did not resolve')
    bad = [a for a in addresses if not is_loopback(a)]
    if bad:
        raise ProviderError('not_loopback', 'assist host resolves to a non-loopback address; only 127.0.0.0/8, ::1 and localhost are allowed')
    return addresses


class _LoopbackConnection(http.client.HTTPConnection):
    def __init__(self, host, port, resolver, timeout):
        super().__init__(host, port, timeout=timeout)
        self._resolver = resolver

    def connect(self):
        last = None
        for address in checked_addresses(self.host, self.port, self._resolver):
            assert is_loopback(address)
            try:
                self.sock = socket.create_connection((address, self.port), self.timeout)
                return
            except OSError as error:
                last = error
        raise ProviderError('connect_failed', f'assist endpoint unreachable ({type(last).__name__})')


def parse_url(url: str):
    """Validate the configured URL; returns (host, port, path). Literal non-loopback IPs are refused here."""
    parts = urlsplit(url)
    if parts.scheme != 'http':
        raise ProviderError('bad_url', 'assist URL must use http:// (loopback only)')
    if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
        raise ProviderError('bad_url', 'assist URL must be http://HOST[:PORT]/PATH without credentials or query')
    try:
        port = parts.port or 80
    except ValueError:
        raise ProviderError('bad_url', 'assist URL has an invalid port') from None
    host = parts.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not is_loopback(host):
            raise ProviderError('not_loopback', 'assist URL host must be loopback (127.0.0.0/8, ::1 or localhost)')
    return host, port, parts.path or '/'


class LoopbackChatProvider(DraftProvider):
    name = 'loopback-chat'

    def __init__(self, url: str, model: str, *, resolver=system_resolver, timeout: float = TIMEOUT_SECONDS):
        if not isinstance(model, str) or not 1 <= len(model) <= 120 or not model.isprintable():
            raise ProviderError('bad_model', f'{MODEL_ENV} must be 1..120 printable characters')
        self.host, self.port, self.path = parse_url(url)
        self.model = model
        self.resolver = resolver
        self.timeout = timeout

    def draft(self, text: str, context: dict) -> str:
        messages = [{'role': 'system', 'content': context['system']}, *context.get('history', []),
                    {'role': 'user', 'content': text}]
        body = json.dumps({'model': self.model, 'messages': messages, 'stream': False, 'temperature': 0}).encode()
        if len(body) > MAX_REQUEST_BYTES:
            raise ProviderError('request_too_large', 'assist request exceeds the size limit')
        connection = _LoopbackConnection(self.host, self.port, self.resolver, self.timeout)
        try:
            connection.request('POST', self.path, body=body, headers={'Content-Type': 'application/json',
                                                                      'Accept': 'application/json'})
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise ProviderError('redirect_refused', 'assist endpoint answered with a redirect; redirects are refused')
            if response.status != 200:
                raise ProviderError('http_error', f'assist endpoint answered HTTP {response.status}')
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
                raise ProviderError('response_too_large', 'assist response exceeds the size limit')
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        except ProviderError:
            raise
        except (OSError, http.client.HTTPException) as error:
            raise ProviderError('transport_error', f'assist request failed ({type(error).__name__})') from None
        finally:
            connection.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError('response_too_large', 'assist response exceeds the size limit')
        return _content(raw)


def _content(raw: bytes) -> str:
    try:
        payload = json.loads(raw)
        message = payload['choices'][0]['message'] if 'choices' in payload else payload['message']
        content = message['content']
    except (ValueError, KeyError, IndexError, TypeError):
        raise ProviderError('bad_response', 'assist response is not a chat completion') from None
    if not isinstance(content, str):
        raise ProviderError('bad_response', 'assist response content is not text')
    return content


def from_env(env, *, resolver=system_resolver):
    """(provider, reason). Absent configuration => (None, 'no provider configured'); bad configuration => the
    reason says why (never silently ignored). Only URL and model are read: no keys, no tokens."""
    url, model = env.get(URL_ENV, ''), env.get(MODEL_ENV, '')
    if not url and not model:
        return None, 'no provider configured'
    if not url or not model:
        return None, f'misconfigured: set both {URL_ENV} and {MODEL_ENV}'
    try:
        return LoopbackChatProvider(url, model, resolver=resolver), ''
    except ProviderError as error:
        return None, f'misconfigured: {error.detail}'
