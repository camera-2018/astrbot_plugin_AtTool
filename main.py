# SPDX-License-Identifier: AGPL-3.0-only
# Derived from nuomicici/astrbot_plugin_AtTool, by 糯米茨 and contributors.
# Modified by camera-2018 on 2026-10-23: fixed prompt prefix, temporary
# per-request permissions, compact member lookup and simpler At conversion.
"""OneBot group mentions with a stable system prompt and enforced permissions."""

import json
import re

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Star
from astrbot.core.agent.message import TextPart
from astrbot.core.message.components import At, BaseMessageComponent, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


AT_INSTRUCTION = (
    "\n【群聊 @ 用法】\n"
    "提及群成员时输出 [at:QQ号]，程序会转换为真正的 @。"
    "只使用当前会话中已确认或 get_group_members 查到的 QQ号；不确定时先查询，禁止编造。\n"
    "只有用户明确要求、确有通知必要且本轮权限允许时，才可输出 [at:all]。"
    "本轮权限不允许时用自然语言说明，发送前程序仍会校验。\n"
)
AT_PATTERN = re.compile(r"\[at:([0-9]+|all)\]")


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _convert_text(text: str, allow_all: bool) -> list[BaseMessageComponent]:
    chain: list[BaseMessageComponent] = []
    offset = 0
    for match in AT_PATTERN.finditer(text):
        if match.start() > offset:
            chain.append(Plain(text[offset:match.start()]))
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

    async def _at_all_permission(self, event: AiocqhttpMessageEvent) -> tuple[bool, str]:
        cached = event.get_extra(self.PERMISSION_CACHE_KEY)
        if cached is not None:
            return cached

        result = (False, "无法确认操作者权限")
        if not event.get_group_id():
            return False, "仅限群聊"
        try:
            if event.is_admin():
                result = (True, "Bot 管理员")
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
        if not isinstance(event, AiocqhttpMessageEvent) or not event.get_group_id():
            return
        # The same bytes are added for every sender and permission outcome.
        req.system_prompt = (req.system_prompt or "") + AT_INSTRUCTION
        allowed, reason = await self._at_all_permission(event)
        status = "允许" if allowed else "禁止"
        req.extra_user_content_parts.append(
            TextPart(text=f"【本轮 @ 权限】{status}使用 [at:all]。原因：{reason}。")
            .mark_as_temp()
        )

    @filter.llm_tool(name="get_group_members")
    async def get_group_members(self, event: AstrMessageEvent, keyword: str = "") -> str:
        """查询当前群成员的 QQ号、昵称、群名片和角色，供 @ 人时确认目标。

        Args:
            keyword(string): 按昵称、群名片或QQ号筛选；空字符串返回全员。
        """
        if not isinstance(event, AiocqhttpMessageEvent) or not event.get_group_id():
            return _json({"status": "error", "message": "仅支持 OneBot 群聊"})
        try:
            members = await event.bot.api.call_action(
                "get_group_member_list", group_id=event.get_group_id()
            )
            selected = []
            for member in members or []:
                uid = str(member.get("user_id", ""))
                nickname = member.get("nickname") or ""
                card = member.get("card") or ""
                if keyword and not any(keyword in value for value in (uid, nickname, card)):
                    continue
                selected.append({
                    "user_id": uid,
                    "nickname": nickname,
                    "group_card": card,
                    "role": member.get("role", "member"),
                })
            return _json({"status": "success", "count": len(selected), "members": selected})
        except Exception as exc:
            self.logger.warning("查询群成员失败 (%s)", type(exc).__name__)
            return _json({"status": "error", "message": "群成员查询失败，请稍后重试"})

    @filter.on_decorating_result(priority=2)
    async def process_at_tags(self, event: AstrMessageEvent) -> None:
        if not isinstance(event, AiocqhttpMessageEvent) or not event.get_group_id():
            return
        result = event.get_result()
        if not result or not result.chain:
            return
        has_all = any(
            (isinstance(part, Plain) and "[at:all]" in part.text)
            or (isinstance(part, At) and str(part.qq) == "all")
            for part in result.chain
        )
        allow_all = False
        if has_all:
            allow_all, _ = await self._at_all_permission(event)
        chain: list[BaseMessageComponent] = []
        for part in result.chain:
            if isinstance(part, Plain) and AT_PATTERN.search(part.text):
                chain.extend(_convert_text(part.text, allow_all))
            elif isinstance(part, At) and str(part.qq) == "all" and not allow_all:
                chain.append(Plain("@全体成员"))
            else:
                chain.append(part)
        result.chain = chain
