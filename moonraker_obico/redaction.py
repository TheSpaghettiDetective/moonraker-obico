# coding=utf-8
"""Conservative helpers for keeping credentials out of diagnostic logs."""
from __future__ import absolute_import

import logging
import re
import traceback

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover - Python 2 compatibility
    from collections import Mapping

try:
    from urllib.parse import unquote_plus, urlsplit, urlunsplit
except ImportError:  # pragma: no cover - Python 2 compatibility
    from urllib import unquote_plus
    from urlparse import urlsplit, urlunsplit


REDACTED = '<redacted>'

try:
    text_type = unicode
except NameError:  # pragma: no cover - Python 3
    text_type = str

string_types = (text_type, str, bytes)


# These are deliberately narrow. Separator-delimited suffix checks below catch
# names such as database_password and x-amz-security-token without treating
# unrelated names such as session_id, user_id, rotation, or monkey as secrets.
_SENSITIVE_FIELD_NAMES = frozenset((
    'access_token',
    'api_key',
    'auth_token',
    'authorization',
    'client_secret',
    'cookie',
    'cookies',
    'credential',
    'credentials',
    'id_token',
    'jwt',
    'one_time_passcode',
    'one_time_passlink',
    'password',
    'passwd',
    'private_key',
    'proxy_authorization',
    'pwd',
    'refresh_token',
    'secret',
    'set_cookie',
    'sig',
    'signature',
    'token',
    'turn_pwd',
    'turn_user',
    'verification_code',
))

_SENSITIVE_QUERY_NAMES = frozenset((
    'auth',
    'code',
    'credential',
    'jwt',
    'key',
    'password',
    'passwd',
    'pwd',
    'secret',
    'sig',
    'signature',
    'token',
))

_KEY_QUALIFIERS = frozenset(('access', 'api', 'private', 'secret'))
_SENSITIVE_SUFFIX_PARTS = frozenset((
    'authorization',
    'cookie',
    'cookies',
    'credential',
    'credentials',
    'password',
    'passwd',
    'pwd',
    'secret',
    'token',
))
_HTTP_TUNNEL_KEYS = frozenset(('http_tunnel', 'http_tunnelv2'))
_HTTP_BODY_KEYS = frozenset(('body', 'content', 'data'))
_COMPACT_SENSITIVE_FIELD_NAMES = frozenset(
    item.replace('_', '') for item in _SENSITIVE_FIELD_NAMES
)

_URL_RE = re.compile(r'(?i)\b[a-z][a-z0-9+.-]*://[^\s\'"<>\)\]]+')
_URL_QUERY_PAIR_RE = re.compile(
    r'(?P<prefix>[?&;])(?P<name>[^=&#;\s]+)=(?P<value>[^&#;\s]*)'
)
_MALFORMED_USERINFO_RE = re.compile(r'(?i)(://)([^/@\s]+)@')
_HEADER_LINE_RE = re.compile(
    r'(?im)^(?P<prefix>\s*(?P<name>[A-Za-z][A-Za-z0-9_-]*)\s*:\s*)(?P<value>[^\r\n]*)$'
)
_ASSIGNMENT_RE = re.compile(
    r'''(?ix)
    (?P<prefix>
        (?P<quote>["']?)
        (?P<name>[a-z][a-z0-9_.-]*)
        (?P=quote)\s*[:=]\s*
    )
    (?P<value>
        "(?:\\.|[^"])*"
        |
        '(?:\\.|[^'])*'
        |
        (?:bearer|token|basic)\s+[^,;&\#\s}\]]+
        |
        [^,;&\#\s}\]]+
    )
    '''
)


def _safe_text(value):
    try:
        if isinstance(value, bytes):
            return value.decode('utf-8', errors='replace')
        if isinstance(value, text_type):
            return value
        return text_type(value)
    except Exception:
        try:
            return '<unprintable {}>'.format(type(value).__name__)
        except Exception:
            return '<unprintable value>'


def _canonical_name(name):
    try:
        name = _safe_text(name)
        name = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
        name = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', name)
        return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    except Exception:
        return ''


def _name_parts(name):
    canonical = _canonical_name(name)
    return canonical, tuple(part for part in canonical.split('_') if part)


def is_sensitive_field_name(name):
    """Return whether a structured field name strongly indicates a secret."""
    try:
        canonical, parts = _name_parts(name)
        compact = canonical.replace('_', '')
        if canonical in _SENSITIVE_FIELD_NAMES or compact in _COMPACT_SENSITIVE_FIELD_NAMES:
            return True
        if parts and parts[-1] in _SENSITIVE_SUFFIX_PARTS:
            return True
        return len(parts) >= 2 and parts[-1] == 'key' and parts[-2] in _KEY_QUALIFIERS
    except Exception:
        return False


def is_sensitive_header_name(name):
    """Return whether an HTTP header name carries credentials or cookies."""
    try:
        _, parts = _name_parts(name)
        return is_sensitive_field_name(name) or (parts and parts[-1] in ('sig', 'signature'))
    except Exception:
        return False


def is_sensitive_query_name(name):
    """Return whether a URL query parameter name strongly indicates a secret."""
    try:
        canonical, parts = _name_parts(name)
        return (
            canonical in _SENSITIVE_QUERY_NAMES
            or is_sensitive_field_name(name)
            or (parts and parts[-1] in ('sig', 'signature'))
        )
    except Exception:
        return False


def _redact_query(query):
    try:
        components = re.split(r'([&;])', query)
        redacted = []
        for component in components:
            if component in ('&', ';'):
                redacted.append(component)
                continue

            name, separator, value = component.partition('=')
            try:
                decoded_name = unquote_plus(name)
            except Exception:
                decoded_name = name
            if separator and is_sensitive_query_name(decoded_name):
                redacted.append(name + separator + REDACTED)
            else:
                redacted.append(component)
        return ''.join(redacted)
    except Exception:
        return _safe_text(query)


def _redact_userinfo(netloc):
    try:
        if '@' not in netloc:
            return netloc
        userinfo, host = netloc.rsplit('@', 1)
        if ':' in userinfo:
            return '{}:{}@{}'.format(REDACTED, REDACTED, host)
        return '{}@{}'.format(REDACTED, host)
    except Exception:
        return netloc


def _fallback_redact_url(value):
    try:
        value = _MALFORMED_USERINFO_RE.sub(
            lambda match: match.group(1) + REDACTED + '@', value)

        def redact_pair(match):
            try:
                decoded_name = unquote_plus(match.group('name'))
            except Exception:
                decoded_name = match.group('name')
            if is_sensitive_query_name(decoded_name):
                return '{}{}={}'.format(match.group('prefix'), match.group('name'), REDACTED)
            return match.group(0)

        return _URL_QUERY_PAIR_RE.sub(redact_pair, value)
    except Exception:
        return REDACTED


def redact_url(value):
    """Redact credentials and sensitive query values from a URL-like value."""
    value = _safe_text(value)
    try:
        parsed = urlsplit(value)
        netloc = _redact_userinfo(parsed.netloc)
        query = _redact_query(parsed.query)
        fragment = _fallback_redact_url(_redact_query(parsed.fragment))
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, fragment))
    except Exception:
        return _fallback_redact_url(value)


def redact_text(value):
    """Redact credential assignments and URLs in arbitrary log text."""
    value = _safe_text(value)
    try:
        value = _URL_RE.sub(lambda match: redact_url(match.group(0)), value)
        value = _fallback_redact_url(value)

        def redact_assignment(match):
            if is_sensitive_field_name(match.group('name')):
                return match.group('prefix') + REDACTED
            return match.group(0)

        def redact_header(match):
            if is_sensitive_header_name(match.group('name')):
                return match.group('prefix') + REDACTED
            # Exception lines such as "ValueError: password=..." resemble
            # headers. Sanitize assignments inside their otherwise-safe value.
            return match.group('prefix') + _ASSIGNMENT_RE.sub(
                redact_assignment, match.group('value'))

        value = _HEADER_LINE_RE.sub(redact_header, value)
        return _ASSIGNMENT_RE.sub(redact_assignment, value)
    except Exception:
        return REDACTED


def _inside_http_tunnel(path):
    return any(part in _HTTP_TUNNEL_KEYS for part in path)


def redact_sensitive_data(value, _seen=None, _path=()):
    """Return a conservative, redacted copy without modifying the input value."""
    try:
        if _seen is None:
            _seen = set()

        if isinstance(value, string_types):
            return redact_text(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value

        value_id = id(value)
        if value_id in _seen:
            return '<recursive>'

        if isinstance(value, Mapping):
            _seen.add(value_id)
            result = {}
            try:
                for key, item in value.items():
                    safe_key = key if isinstance(key, (text_type, str, int, float, bool)) else _safe_text(key)
                    canonical_key = _canonical_name(key)
                    next_path = _path + (canonical_key,)
                    if _inside_http_tunnel(next_path) and canonical_key in _HTTP_BODY_KEYS:
                        result[safe_key] = REDACTED
                    elif _inside_http_tunnel(next_path) and canonical_key == 'cookies':
                        result[safe_key] = REDACTED
                    elif _path and _path[-1] == 'params' and is_sensitive_query_name(key):
                        result[safe_key] = REDACTED
                    elif is_sensitive_field_name(key):
                        result[safe_key] = REDACTED
                    else:
                        result[safe_key] = redact_sensitive_data(item, _seen, next_path)
            finally:
                _seen.discard(value_id)
            return result

        if isinstance(value, list):
            _seen.add(value_id)
            try:
                return [redact_sensitive_data(item, _seen, _path) for item in value]
            finally:
                _seen.discard(value_id)

        if isinstance(value, tuple):
            _seen.add(value_id)
            try:
                return tuple(redact_sensitive_data(item, _seen, _path) for item in value)
            finally:
                _seen.discard(value_id)

        if isinstance(value, (set, frozenset)):
            _seen.add(value_id)
            try:
                return [redact_sensitive_data(item, _seen, _path) for item in value]
            finally:
                _seen.discard(value_id)

        return redact_text(value)
    except Exception:
        try:
            return '<unprintable {}>'.format(type(value).__name__)
        except Exception:
            return '<unprintable value>'


def format_http_request(request):
    """Format request metadata for logs without ever including the request body."""
    try:
        method = redact_text(getattr(request, 'method', 'UNKNOWN'))
        url = redact_url(getattr(request, 'url', ''))
        headers = redact_sensitive_data(dict(getattr(request, 'headers', {}) or {}))
        return 'HTTP request: {} {} headers={}'.format(method, url, headers)
    except Exception:
        return 'HTTP request: <unavailable>'


def redacted_traceback():
    """Return the current traceback as sanitized text, without raising."""
    try:
        return redact_text(traceback.format_exc())
    except Exception:
        return '<traceback unavailable>'


class RedactingFilter(logging.Filter):
    """Sanitize final log records, including logs emitted by dependencies."""

    def filter(self, record):
        try:
            record.msg = redact_sensitive_data(record.msg)
            record.args = redact_sensitive_data(record.args)
            message = record.getMessage()
            if record.exc_info:
                message = '{}\n{}'.format(
                    message,
                    ''.join(traceback.format_exception(*record.exc_info)),
                )
            elif record.exc_text:
                message = '{}\n{}'.format(message, record.exc_text)

            record.msg = redact_text(message)
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        except Exception:
            record.msg = '<unavailable log message>'
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True
