"""Exact, in-memory C# resource-key patching for proven literal load spans."""
import json

from .errors import fail
from .references import MAX_TEXT_BYTES, _literal_loads


def _key(value, field, location):
    if not isinstance(value, str):
        fail('LOADING_KEY_PATCH', location, field+' must be a string key')
    try:
        value.encode('utf-8', errors='strict')
    except UnicodeEncodeError:
        fail('LOADING_KEY_PATCH', location, field+' contains an invalid Unicode surrogate')


def _token_value(token, location):
    try:
        value = json.loads(token)
    except (json.JSONDecodeError, TypeError, ValueError):
        fail('LOADING_KEY_ENCODING', location, 'Literal token is outside the JSON-safe C# string escape subset')
    if not isinstance(value, str):
        fail('LOADING_KEY_ENCODING', location, 'Literal token must decode to a string')
    return value


def patch_script(raw_bytes, patches):
    """Return patched UTF-8 C# bytes without writing files.

    Each patch uses Unicode code-point offsets matching references._literal_loads.
    Only exact ordinary string arguments proven by that scanner are editable.
    """
    if not isinstance(raw_bytes, bytes):
        fail('LOADING_KEY_SOURCE', 'source', 'Expected source bytes')
    if len(raw_bytes) > MAX_TEXT_BYTES:
        fail('LOADING_KEY_BUDGET', 'source', 'C# source exceeds the bounded literal-scanner byte limit')
    if not isinstance(patches, list):
        fail('LOADING_KEY_PATCH', 'patches', 'Expected an ordered patch list')
    bom = raw_bytes.startswith(b'\xef\xbb\xbf')
    source_bytes = raw_bytes[3:] if bom else raw_bytes
    try:
        source = source_bytes.decode('utf-8', errors='strict')
    except UnicodeDecodeError as error:
        fail('LOADING_KEY_UTF8', 'source', 'C# source is not valid UTF-8 at byte '+str(error.start))
    if not patches:
        return raw_bytes

    found, _unknown = _literal_loads('<memory>', source)
    proven = {}
    for row in found:
        span = row.get('span')
        if isinstance(span, list) and len(span) == 2 and all(type(item) is int for item in span):
            proven.setdefault(tuple(span), []).append(row)

    normalized = []
    previous_start = -1
    previous_end = -1
    seen = set()
    for index, item in enumerate(patches):
        location = 'patches['+str(index)+']'
        if not isinstance(item, dict) or set(item) != {'start', 'end', 'before', 'after'}:
            fail('LOADING_KEY_PATCH', location, 'Patch must contain exactly start, end, before, after')
        start, end = item['start'], item['end']
        before, after = item['before'], item['after']
        if type(start) is not int or type(end) is not int or start < 0 or end <= start or end > len(source):
            fail('LOADING_KEY_SPAN', location, 'Span must be a non-empty in-range Unicode character interval')
        _key(before, 'before', location)
        _key(after, 'after', location)
        span = (start, end)
        if span in seen:
            fail('LOADING_KEY_DUPLICATE', location, 'Repeated source span')
        if start < previous_start:
            fail('LOADING_KEY_ORDER', location, 'Patches must be ordered by ascending source offset')
        if start < previous_end:
            fail('LOADING_KEY_OVERLAP', location, 'Patch spans overlap')
        rows = proven.get(span, [])
        if len(rows) != 1:
            fail('LOADING_KEY_NOT_LITERAL_LOAD', location, 'Span is not one exact Resources.Load or Addressables.LoadAssetAsync string argument')
        token = source[start:end]
        if not token.startswith('"') or not token.endswith('"') or len(token) < 2:
            fail('LOADING_KEY_ENCODING', location, 'Only ordinary C# string tokens are supported')
        if _token_value(token, location) != before:
            fail('LOADING_KEY_BEFORE_MISMATCH', location, 'Decoded source string does not equal the expected before key')
        normalized.append((start, end, after))
        seen.add(span)
        previous_start, previous_end = start, end

    result = source
    for start, end, after in reversed(normalized):
        result = result[:start] + json.dumps(after, ensure_ascii=True, separators=(',', ':')) + result[end:]
    encoded = result.encode('utf-8', errors='strict')
    return (b'\xef\xbb\xbf' if bom else b'') + encoded
