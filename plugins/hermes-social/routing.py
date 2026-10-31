"""Bound public opportunities by transport facts, never by topic keywords.

The model decides whether and how to participate with a public snapshot. This
module supplies recipient evidence and bounded context; it grants no authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import time

from .context import ContextRecord

WINDOW_SECONDS = 600
FOLLOWUP_SECONDS = 180
MAX_RELEVANT = 12


@dataclass(frozen=True)
class RoutingDecision:
    target: str
    anchor: str
    rows: tuple[ContextRecord, ...] = ()
    exchange: tuple[ContextRecord, ...] = ()

    @property
    def participate(self) -> bool:
        return self.target in {'group', 'speaker', 'bot'}


def public_rows(history, message_id, *, now=None):
    """Include interleaved public discussion, even messages to other members."""
    now = time.time() if now is None else now
    return tuple(row for row in history if row.message_id != message_id
                 and 0 <= now-row.observed_at <= WINDOW_SECONDS)[-MAX_RELEVANT:]


def accepts_update(original, incoming):
    """Ordinary public activity does not by itself cancel an in-flight reply.

    The channel separately bounds count/time and retains the exact source,
    route, connection and deadline. Explicit requests/control supersede it.
    Topic interpretation belongs to the next model turn, not a word list.
    """
    return (incoming.source.chat_id == original.source.chat_id
            and incoming.reply_expected is False
            and not incoming.text.lstrip().startswith('/')
            and not getattr(original, '_napcat_optional_media', False))


def waiting_exchange(history, sender, *, now):
    for answer in reversed(history):
        if answer.role != 'self' or not 0 <= now-answer.observed_at <= FOLLOWUP_SECONDS:
            continue
        request = next((row for row in history if row.role == 'member'
                        and row.message_id == answer.reply_to_message_id
                        and row.sender_id == sender
                        and row.observed_at <= answer.observed_at), None)
        if request is not None:
            return request, answer
    return None, None


def decide(event, history: tuple[ContextRecord, ...], *, now=None) -> RoutingDecision:
    now = time.time() if now is None else now
    text = event.text.strip()
    targets = tuple(getattr(event, 'at_targets', ()))
    bot_id = str(getattr(event, 'bot_id', ''))
    if bot_id and bot_id in targets:
        return RoutingDecision('bot', 'mention')
    if targets:
        return RoutingDecision('member', 'mention')
    author = getattr(event, 'verified_reply_author_id', None)
    if author is not None:
        return RoutingDecision('bot' if str(author) == bot_id else 'member', 'reply')
    if getattr(event, 'reply_to_message_id', None) is not None:
        return RoutingDecision('unknown', 'unverified_reply')
    if not text or text.startswith('/'):
        return RoutingDecision('unknown', 'no_text')
    rows = public_rows(history, event.message_id, now=now)
    if getattr(event, '_napcat_optional_media', False):
        return RoutingDecision('speaker', 'media_request', rows)
    # Pure media is observed by the media pipeline. Arbitrarily short natural
    # utterances (city names, corrections, jokes) still deserve model judgement.
    if text.startswith(('[attachment:', '[unsupported:')) and '\n' not in text:
        return RoutingDecision('unknown', 'media_observation')
    if not any(char.isalnum() for char in text):
        return RoutingDecision('unknown', 'no_text')
    request, answer = waiting_exchange(history, str(event.user_id), now=now)
    if request is not None:
        # Preserve the real pair without claiming the new text answers it.
        pair = (request, answer)
        background = tuple(row for row in rows if row not in pair)
        evidence = sorted((*background[-(MAX_RELEVANT-2):], *pair), key=lambda row: row.observed_at)
        # Immediate turn-taking is stronger evidence than a past exchange.
        # Interleaving weakens the cue, never suppresses the model opportunity.
        immediate = bool(rows and rows[-1] is answer)
        return RoutingDecision('speaker' if immediate else 'group',
                               'recent_exchange' if immediate else 'public_turn', tuple(evidence), pair)
    return RoutingDecision('group', 'public_turn', rows)
