# SPDX-License-Identifier: AGPL-3.0-only
# Derived from nuomicici/astrbot_plugin_AtTool, by 糯米茨 and contributors.
# Modified by camera-2018 on 2026-10-23: fixed prompt prefix, temporary
# per-request permissions, compact member lookup and simpler At conversion.
# Updated on 2026-11-09: QQ Official OpenIDs, incoming mentions and media delivery.
"""OneBot and QQ Official mentions with a stable prompt and enforced permissions."""

import json
import re

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Star
from astrbot.core.agent.message import TextPart
from astrbot.core.message.components import At, AtAll, BaseMessageComponent, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

if __package__:
    from .qq_official import (
        KnownMembers,
        OUTGOING,
        QQOfficialCompat,
        convert_text,
        mention_target,
    )
else:  # Standalone SDK tests.
    from qq_official import (
        KnownMembers,
        OUTGOING,
        QQOfficialCompat,
        convert_text,
        mention_target,
    )


AT_INSTRUCTION = (
    "\n【群聊 @ 用法】\n"
    "提及群成员时输出 [at:QQ号]，程序会转换为真正的 @。"
    "只使用当前会话中已确认或 get_group_members 查到的 QQ号；不确定时先查询，禁止编造。\n"
    "只有用户明确要求、确有通知必要且本轮权限允许时，才可输出 [at:all]。"
    "本轮权限不允许时用自然语言说明，发送前程序仍会校验。\n"
)
OFFICIAL_AT_INSTRUCTION = (
    "\n【QQ 官方群聊 @ 用法】\n"
    "需要真正 @ 群成员时，调用内置 send_message_to_user 工具，messages 中加入 "
    '{"type":"mention_user","mention_user_id":"已确认的member_openid"}，'
    '并把正文作为 {"type":"plain","text":"回复正文"} 放在同一次调用中。'
    "当前群的 session 参数留空。发送成功后不要重复输出已发送的正文。"
    "普通不需要 @ 的聊天直接回复即可。\n"
    "成员ID必须使用本群已确认的 member_openid，不要使用或猜测普通QQ号。"
    "不确定目标时先用 get_group_members 查询；官方平台只返回本群已见成员，不代表全员名单。"
    "只有用户明确要求、确有通知必要且本轮权限允许时，才可通过 mention_user_id=all 请求 @全体。"
    "本轮不允许时用自然语言说明，发送前程序仍会校验。\n"
)
AT_PATTERN = re.compile(r"\[at:([0-9]+|all)\]", re.ASCII)


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _convert_text(text: str, allow_all: bool) -> list[BaseMessageComponent]:
    chain: list[BaseMessageComponent] = []
    offset = 0
    for match in AT_PATTERN.finditer(text):
        if match.start() > offset:
            chain.append(Plain(text[offset : match.start()]))
        target = match.group(1)
        chain.append(
            Plain("@全体成员") if target == "all" and not allow_all else At(qq=target)
        )
        offset = match.end()
    if offset < len(text):
        chain.append(Plain(text[offset:]))
    return chain


class LLMAtToolPlugin(Star):
    """Keep operator policy in code and out of the stable prompt prefix."""

    PERMISSION_CACHE_KEY = "_attool_v3_permission"

    def __init__(self, context):
        super().__init__(context)
        self.members = KnownMembers()
        self.qq_compat = QQOfficialCompat()

    async def initialize(self):
        self.qq_compat.install()
        self.logger.info("@工具 v3.1.1：OneBot 使用标签，QQ 官方使用内置发送工具")

    async def terminate(self):
        self.qq_compat.uninstall()
        self.members.rooms.clear()

    @staticmethod
    def _official(event):
        metadata = getattr(event, "platform_meta", None)
        return getattr(metadata, "name", "") in {"qq_official", "qq_official_webhook"}

    def _supported(self, event):
        return isinstance(event, AiocqhttpMessageEvent) or self._official(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=20000)
    async def observe_members(self, event: AstrMessageEvent) -> None:
        if self._official(event) and event.get_group_id() and not event.is_stopped():
            self.members.observe(event)

    async def _at_all_permission(self, event: AstrMessageEvent) -> tuple[bool, str]:
        cached = event.get_extra(self.PERMISSION_CACHE_KEY)
        if cached is not None:
            return cached

        result = (False, "无法确认操作者权限")
        if not event.get_group_id():
            return False, "仅限群聊"
        try:
            if event.is_admin():
                result = (True, "Bot 管理员")
            elif self._official(event):
                result = (
                    False,
                    "官方平台无法确认操作者群角色，仅允许 Bot 管理员发起 @全体",
                )
            elif event.get_sender_id():
                member = await event.bot.api.call_action(
                    "get_group_member_info",
                    group_id=event.get_group_id(),
                    user_id=event.get_sender_id(),
                )
                if member.get("role") in {"owner", "admin"}:
                    result = (True, "群主或群管理员")
                else:
                    result = (False, "操作者不是群主、群管理员或 Bot 管理员")
        except Exception as exc:
            self.logger.warning("查询 @全体权限失败 (%s)", type(exc).__name__)
            result = (False, "权限查询失败")
        event.set_extra(self.PERMISSION_CACHE_KEY, result)
        return result

    @filter.on_llm_request()
    async def inject_at_instruction(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        if not self._supported(event) or not event.get_group_id():
            return
        if self._official(event):
            self.members.observe(event)
        # Each platform has a fixed prefix, independent of sender/permissions.
        official = self._official(event)
        req.system_prompt = (req.system_prompt or "") + (
            OFFICIAL_AT_INSTRUCTION if official else AT_INSTRUCTION
        )
        allowed, reason = await self._at_all_permission(event)
        status = "允许" if allowed else "禁止"
        action = '@全体（mention_user_id="all"）' if official else "[at:all]"
        req.extra_user_content_parts.append(
            TextPart(
                text=f"【本轮 @ 权限】{status}使用 {action}。原因：{reason}。"
            ).mark_as_temp()
        )

    @filter.on_using_llm_tool()
    async def guard_official_tool(self, event, tool, tool_args) -> None:
        """Native sends bypass result decoration; retain the existing @all policy."""
        if (
            not self._official(event)
            or getattr(tool, "name", "") != "send_message_to_user"
            or not isinstance(tool_args, dict)
        ):
            return
        messages = tool_args.get("messages")
        if not isinstance(messages, list):
            return

        def targets_all(message):
            if not isinstance(message, dict):
                return False
            kind = str(message.get("type", "")).lower()
            if kind == "mention_user":
                return message.get("mention_user_id") == "all"
            return kind == "plain" and any(
                mention_target(match) == "all"
                for match in OUTGOING.finditer(str(message.get("text", "")))
            )

        if not any(targets_all(message) for message in messages):
            return
        allowed, _ = await self._at_all_permission(event)
        if allowed:
            return
        for index, message in enumerate(messages):
            if not targets_all(message):
                continue
            if str(message.get("type", "")).lower() == "mention_user":
                messages[index] = {"type": "plain", "text": "@全体成员"}
            else:
                message["text"] = OUTGOING.sub(
                    lambda match: "@全体成员"
                    if mention_target(match) == "all"
                    else match.group(),
                    str(message.get("text", "")),
                )

    @filter.llm_tool(name="get_group_members")
    async def get_group_members(
        self, event: AstrMessageEvent, keyword: str = ""
    ) -> str:
        """查询当前群成员ID和昵称，供 @ 人时确认目标。官方平台仅返回本群已见成员OpenID，不代表全员；OneBot可查全员及群角色。

        Args:
            keyword(string): 按昵称、群名片或成员ID筛选；空字符串返回可查询名单。
        """
        if not self._supported(event) or not event.get_group_id():
            return _json({"status": "error", "message": "仅支持 OneBot 或 QQ 官方群聊"})
        if self._official(event):
            self.members.observe(event)
            selected, total, seen = self.members.lookup(event, keyword)
            return _json(
                {
                    "status": "success",
                    "id_type": "member_openid",
                    "source": "observed_messages",
                    "complete": False,
                    "total_seen": seen,
                    "count": len(selected),
                    "truncated": total > len(selected),
                    "members": selected,
                }
            )
        try:
            members = await event.bot.api.call_action(
                "get_group_member_list", group_id=event.get_group_id()
            )
            selected = []
            for member in members or []:
                uid = str(member.get("user_id", ""))
                nickname = member.get("nickname") or ""
                card = member.get("card") or ""
                if keyword and not any(
                    keyword in value for value in (uid, nickname, card)
                ):
                    continue
                selected.append(
                    {
                        "user_id": uid,
                        "nickname": nickname,
                        "group_card": card,
                        "role": member.get("role", "member"),
                    }
                )
            return _json(
                {"status": "success", "count": len(selected), "members": selected}
            )
        except Exception as exc:
            self.logger.warning("查询群成员失败 (%s)", type(exc).__name__)
            return _json({"status": "error", "message": "群成员查询失败，请稍后重试"})

    @filter.on_decorating_result(priority=2)
    async def process_at_tags(self, event: AstrMessageEvent) -> None:
        if not self._supported(event) or not event.get_group_id():
            return
        result = event.get_result()
        if not result or not result.chain:
            return
        has_all = any(
            (isinstance(part, Plain) and "[at:all]" in part.text)
            or isinstance(part, AtAll)
            or (isinstance(part, At) and str(part.qq) == "all")
            or (
                self._official(event)
                and isinstance(part, Plain)
                and "<qqbot-at-everyone" in part.text
            )
            for part in result.chain
        )
        allow_all = False
        if has_all:
            allow_all, _ = await self._at_all_permission(event)
        chain: list[BaseMessageComponent] = []
        official = self._official(event)
        known = ()
        if official:
            self.members.observe(event)
            known = self.members.rooms.get(
                (event.get_platform_id(), event.get_group_id()), {}
            )
        for part in result.chain:
            if official and isinstance(part, Plain) and OUTGOING.search(part.text):
                chain.extend(convert_text(part.text, allow_all=allow_all, known=known))
            elif isinstance(part, Plain) and AT_PATTERN.search(part.text):
                chain.extend(_convert_text(part.text, allow_all))
            elif (
                isinstance(part, AtAll)
                or (isinstance(part, At) and str(part.qq) == "all")
            ) and not allow_all:
                chain.append(Plain("@全体成员"))
            else:
                chain.append(part)
        result.chain = chain
        if official and any(isinstance(part, (At, AtAll)) for part in chain):
            result.use_markdown_ = True
