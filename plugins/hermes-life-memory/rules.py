"""Conservative, exact-span self-statements; no model, network or name resolution."""
import re

_RULES = (
    ('interest', re.compile(r'^我(?:最近|现在|一直)?(?:很)?喜欢(?P<value>[^。！？!?\n]{1,120})[。！!]?$', re.DOTALL)),
    ('interest', re.compile(r'^我对(?P<value>[^。！？!?\n]{1,120})感兴趣[。！!]?$', re.DOTALL)),
    ('preference', re.compile(r'^我希望你(?P<value>[^。！？!?\n]{1,120})[。！!]?$', re.DOTALL)),
    ('boundary', re.compile(r'^我不希望(?P<value>[^。！？!?\n]{1,120})[。！!]?$', re.DOTALL)),
)
_AMBIGUOUS = re.compile(r'["“”「」『』]|如果|假如|假设|他说|她说|别人|可能|也许|开玩笑|不喜欢|不再|但是|不过|并不|[吗呢][。！!]?$')


def self_statement(text):
    if not isinstance(text, str) or len(text.encode()) > 2000 or _AMBIGUOUS.search(text):
        return None
    for field, rule in _RULES:
        match = rule.fullmatch(text)
        if match is not None:
            value = match['value']
            if value.strip() != value or not value or len(value.encode()) > 480:
                return None
            return field, value, match.start('value')
    return None


_REMEMBER = re.compile(r'^(?:(?:请|请你|帮我|麻烦你|麻烦帮我)\s*)?(?:记住|记下|记一下)[：:,，\s]*(?P<statement>我[^\n]{1,600})$')


def explicit_request(text):
    """Explicit first-person storage intent only; ordinary chat is never opted in."""
    if not isinstance(text, str) or len(text.encode()) > 2000 or _AMBIGUOUS.search(text):
        return None
    return _REMEMBER.fullmatch(text.strip())


def explicit_candidate(text):
    match = explicit_request(text)
    if match is None:
        return None
    candidate = self_statement(match['statement'])
    if candidate is None:
        return None
    field, value, start = candidate
    return dict(field=field, value=value, start=match.start('statement')+start)
