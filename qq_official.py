# SPDX-License-Identifier: AGPL-3.0-only
"""QQ Official mention compatibility, installed and removed with this plugin."""

from __future__ import annotations

import functools
import re
import time
from collections import OrderedDict

from astrbot.core.message.components import (
    At,
    AtAll,
    File,
    Image,
    Plain,
    Record,
    Reply,
    Video,
)


OPENID = re.compile(r"[0-9a-fA-F]{32}\Z")
MARKUP = (
    r"<@!?(?P<legacy>[A-Za-z0-9_-]{1,128})>"
    r"|<qqbot-at-user\s+id=[\"\'](?P<xml>[A-Za-z0-9_-]{1,128})[\"\']\s*/>"
)
INCOMING = re.compile(MARKUP)
OUTGOING = re.compile(
    r"\[at:(?P<model>[A-Za-z0-9_-]{1,128})\]|"
    + MARKUP
    + r"|(?P<everyone><qqbot-at-everyone\s*/>)"
)
MEDIA = (Image, Record, Video, File)


def field(value, name, default=None):
    return (
        value.get(name, default)
        if isinstance(value, dict)
        else getattr(value, name, default)
    )


def mention_target(match):
    groups = match.groupdict()
    return next(
        (groups.get(k) for k in ("model", "legacy", "xml") if groups.get(k)), "all"
    )


def valid_target(target: str, known=()) -> bool:
    return bool(
        target and target != "all" and (OPENID.fullmatch(target) or target in known)
    )


def convert_text(text: str, *, allow_all: bool, known=()):
    """Only turn official OpenIDs (or observed opaque IDs) into mentions."""
    chain, offset = [], 0
    for match in OUTGOING.finditer(text):
        target = mention_target(match)
        if target != "all" and not valid_target(target, known):
            continue
        if match.start() > offset:
            chain.append(Plain(text[offset : match.start()]))
        chain.append(
            At(qq=target) if target != "all" or allow_all else Plain("@全体成员")
        )
        offset = match.end()
    if offset < len(text):
        chain.append(Plain(text[offset:]))
    return chain


def restore_mentions(message):
    """Restore top-level user mentions; never interpret mentions inside quotes."""
    raw = message.raw_message
    if not field(raw, "group_openid"):
        return
    mentions = field(raw, "mentions", None)
    if mentions is None:
        mentions = field(field(raw, "raw_data", {}), "mentions", [])
    people = {str(field(m, "id")): m for m in (mentions or []) if field(m, "id")}
    bots = {uid for uid, m in people.items() if field(m, "is_you", False) is True}
    bots.add(str(message.self_id))
    seen = {str(c.qq) for c in message.message if isinstance(c, At)}
    quoted_ids = set()
    for c in message.message:
        if isinstance(c, Reply):
            quoted_ids.update(
                str(item.qq) for item in (c.chain or []) if isinstance(item, At)
            )
            quoted_ids.update(
                mention_target(m)
                for m in INCOMING.finditer(c.message_str or c.text or "")
            )
    restored = []
    for component in message.message:
        if not isinstance(component, Plain):
            restored.append(component)
            continue
        offset = 0
        for match in INCOMING.finditer(component.text):
            target = mention_target(match)
            if target in bots or not valid_target(target, people):
                continue
            if match.start() > offset:
                restored.append(Plain(component.text[offset : match.start()]))
            restored.append(
                At(qq=target, name=field(people.get(target), "username", "") or "")
            )
            seen.add(target)
            offset = match.end()
        if offset < len(component.text):
            restored.append(Plain(component.text[offset:]))
    # Some event variants retain mentions metadata after removing inline markup.
    missing = [
        At(qq=uid, name=field(m, "username", "") or "")
        for uid, m in people.items()
        if uid not in bots
        and uid not in seen
        and uid not in quoted_ids
        and valid_target(uid, people)
    ]
    insert = next(
        (i for i, c in enumerate(restored) if isinstance(c, Plain)), len(restored)
    )
    restored[insert:insert] = missing
    message.message = restored
    message.message_str = "".join(
        c.text if isinstance(c, Plain) else " " if isinstance(c, At) else ""
        for c in restored
    ).strip()


class KnownMembers:
    """Bounded, volatile directory scoped by stable platform ID and group OpenID."""

    def __init__(
        self, clock=time.monotonic, max_rooms=128, max_members=2048, ttl=7 * 86400
    ):
        self.clock, self.max_rooms, self.max_members, self.ttl = (
            clock,
            max_rooms,
            max_members,
            ttl,
        )
        self.rooms = OrderedDict()

    def observe(self, event):
        key = (event.get_platform_id(), event.get_group_id())
        if not all(key):
            return
        room = self.rooms.setdefault(key, OrderedDict())
        self.rooms.move_to_end(key)
        raw = event.message_obj.raw_message
        people = [(event.get_sender_id(), event.get_sender_name())]
        bot_ids = {str(event.get_self_id()), "qq_official"}
        for member in field(raw, "mentions", []) or []:
            uid = str(field(member, "id", "") or "")
            if field(member, "is_you", False):
                bot_ids.add(uid)
            else:
                people.append((uid, field(member, "username", "") or ""))
        people.extend(
            (str(c.qq), c.name or "") for c in event.get_messages() if isinstance(c, At)
        )
        now = self.clock()
        for uid, name in people:
            if not uid or uid in bot_ids or uid == "all":
                continue
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", uid):
                continue
            previous = room.get(uid)
            room[uid] = {
                "user_id": uid,
                "nickname": str(name or (previous or {}).get("nickname", ""))[:100],
                "group_card": "",
                "role": "unknown",
                "seen": now,
            }
            room.move_to_end(uid)
        while len(room) > self.max_members:
            room.popitem(last=False)
        while len(self.rooms) > self.max_rooms:
            self.rooms.popitem(last=False)

    def lookup(self, event, keyword="", limit=200):
        room = self.rooms.get((event.get_platform_id(), event.get_group_id()), {})
        cutoff = self.clock() - self.ttl
        for uid in [uid for uid, member in room.items() if member["seen"] < cutoff]:
            del room[uid]
        found = [
            {k: v for k, v in m.items() if k != "seen"}
            for m in room.values()
            if not keyword
            or keyword.casefold() in (m["user_id"] + " " + m["nickname"]).casefold()
        ]
        return found[:limit], len(found), len(room)


def group_chunks(message):
    """Separate mentions from media, preserving order and message-chain metadata."""
    normalized = []
    for c in message.chain:
        normalized.extend(
            convert_text(c.text, allow_all=True) if isinstance(c, Plain) else [c]
        )
    chunks, current = [], []
    has_media = has_mention = False
    for c in normalized:
        media = isinstance(c, MEDIA)
        mention = isinstance(c, At) and bool(c.qq)
        if current and (
            (media and (has_media or has_mention)) or (mention and has_media)
        ):
            chunks.append(message.derive(current))
            current, has_media, has_mention = [], False, False
        current.append(c)
        has_media |= media
        has_mention |= mention
    if current or not message.chain:
        chunks.append(message.derive(current))
    for chunk in chunks:
        if any(isinstance(c, At) and c.qq for c in chunk.chain):
            chunk.use_markdown_ = True
            # The core drops AtAll; QQ now supports its own text-chain tag.
            chunk.chain = [
                Plain("<qqbot-at-everyone />")
                if isinstance(c, AtAll) or (isinstance(c, At) and str(c.qq) == "all")
                else c
                for c in chunk.chain
            ]
    return chunks


class QQOfficialCompat:
    """Wrap existing adapter methods; no edits to AstrBot or to its send/retry logic."""

    def __init__(self):
        self.enabled = False
        self.patches = []

    def _patch(self, cls, name, replacement, *, static=False):
        original = cls.__dict__[name]
        setattr(cls, name, staticmethod(replacement) if static else replacement)
        self.patches.append((cls, name, original, replacement))

    def install(self):
        if self.enabled:
            return
        from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
            QQOfficialPlatformAdapter,
        )
        from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
            QQOfficialMessageEvent,
        )
        from astrbot.core.platform.message_type import MessageType

        self.enabled = True
        parse = QQOfficialPlatformAdapter._parse_from_qqofficial
        post = QQOfficialMessageEvent._post_send_one
        send_session = QQOfficialPlatformAdapter._send_by_session_common

        @functools.wraps(parse)
        async def parse_mentions(*args, **kwargs):
            message = await parse(*args, **kwargs)
            if self.enabled:
                restore_mentions(message)
            return message

        @functools.wraps(post)
        async def post_group(event, message, stream=None):
            raw = event.message_obj.raw_message
            is_group = bool(field(raw, "group_openid"))
            if not self.enabled or not is_group:
                return await post(event, message, stream)
            chunks = group_chunks(message)
            result = None
            for chunk in chunks:
                previous_buffer = event.send_buffer
                event.send_buffer = chunk
                try:
                    result = await post(
                        event, chunk, stream if len(chunks) == 1 else None
                    )
                finally:
                    if event.send_buffer is chunk:
                        event.send_buffer = previous_buffer
            return result

        @functools.wraps(send_session)
        async def send_group(adapter, session, message):
            sid = session.session_id.rsplit("_", 1)[-1]
            if (
                not self.enabled
                or session.message_type != MessageType.GROUP_MESSAGE
                or adapter._session_scene.get(sid) != "group"
            ):
                return await send_session(adapter, session, message)
            for chunk in group_chunks(message):
                await send_session(adapter, session, chunk)

        try:
            self._patch(
                QQOfficialPlatformAdapter,
                "_parse_from_qqofficial",
                parse_mentions,
                static=True,
            )
            self._patch(QQOfficialMessageEvent, "_post_send_one", post_group)
            self._patch(
                QQOfficialPlatformAdapter, "_send_by_session_common", send_group
            )
        except BaseException:
            self.uninstall()
            raise

    def uninstall(self):
        self.enabled = False
        for cls, name, original, replacement in reversed(self.patches):
            # Do not overwrite a newer wrapper installed by another plugin.
            if getattr(cls, name) is replacement:
                setattr(cls, name, original)
        self.patches.clear()
