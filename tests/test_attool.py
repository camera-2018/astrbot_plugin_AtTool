"""Exercise the real AstrBot SDK; mock only platform API calls and event input."""

import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import Message, TextPart, dump_messages_with_checkpoints
from astrbot.core.message.components import At, AtAll, Image, Plain
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

from main import LLMAtToolPlugin


class Event(AiocqhttpMessageEvent):
    def __init__(self, sender="10001", role="member", super_admin=False, group="20001"):
        self.sender = sender
        self.role = role
        self.super_admin = super_admin
        self.group = group
        self.extras = {}
        self.result = MessageEventResult()
        self.bot = SimpleNamespace(api=SimpleNamespace(call_action=AsyncMock(return_value={"role": role})))

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return self.group

    def is_admin(self):
        return self.super_admin

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_result(self):
        return self.result


class AtToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = LLMAtToolPlugin(SimpleNamespace())

    async def test_system_prefix_identical_across_senders_and_roles(self):
        prefixes = []
        for sender, role, admin in [
            ("10001", "member", False), ("10002", "member", False),
            ("10003", "admin", False), ("10004", "owner", False),
            ("10005", "member", True),
        ]:
            req = ProviderRequest(system_prompt="固定人格", prompt="原始问题", contexts=[{"role": "user", "content": "旧消息"}])
            before = copy.deepcopy(req.contexts)
            await self.plugin.inject_at_instruction(Event(sender, role, admin), req)
            prefixes.append(req.system_prompt)
            self.assertEqual(req.prompt, "原始问题")
            self.assertEqual(req.contexts, before)
            self.assertNotIn(sender, req.system_prompt)
        self.assertEqual(len(set(prefixes)), 1)

    async def test_permission_is_at_current_user_tail_and_excluded_from_history(self):
        req = ProviderRequest(system_prompt="固定人格", prompt="问题")
        await self.plugin.inject_at_instruction(Event(), req)
        assembled = await req.assemble_context()
        self.assertEqual(assembled["content"][0]["text"], "问题")
        self.assertIn("禁止", assembled["content"][-1]["text"])
        self.assertTrue(assembled["content"][-1]["_no_save"])
        persisted = dump_messages_with_checkpoints([Message.model_validate(assembled)])
        self.assertEqual(persisted[0]["content"], [{"type": "text", "text": "问题"}])

    async def test_other_extra_parts_unchanged(self):
        req = ProviderRequest(extra_user_content_parts=[TextPart(text="已有记忆")])
        await self.plugin.inject_at_instruction(Event(role="admin"), req)
        self.assertEqual(req.extra_user_content_parts[0].text, "已有记忆")
        self.assertIn("允许", req.extra_user_content_parts[-1].text)

    async def test_prompt_ignores_private_and_other_platforms(self):
        for event in (Event(group=""), SimpleNamespace()):
            req = ProviderRequest(system_prompt="原提示")
            await self.plugin.inject_at_instruction(event, req)
            self.assertEqual(req.system_prompt, "原提示")
            self.assertEqual(req.extra_user_content_parts, [])

    async def test_member_mention_serializes_to_onebot_without_network(self):
        event = Event()
        event.result.chain = [Plain("你好[at:10002]，上线吧")]
        await self.plugin.process_at_tags(event)
        wire = await AiocqhttpMessageEvent._parse_onebot_json(event.result)
        self.assertIn({"type": "at", "data": {"qq": "10002"}}, wire)
        event.bot.api.call_action.assert_not_awaited()

    async def test_preserves_whitespace_images_and_other_components(self):
        event = Event()
        image = Image.fromURL("https://example.com/test.png")
        event.result.chain = [Plain("前  [at:10002] 后\n"), image]
        await self.plugin.process_at_tags(event)
        self.assertEqual(event.result.chain[0].text, "前  ")
        self.assertEqual(event.result.chain[2].text, " 后\n")
        self.assertIs(event.result.chain[3], image)
        self.assertNotIn("\u200b", "".join(p.text for p in event.result.chain if isinstance(p, Plain)))

    async def test_adjacent_tags_and_plain_text_round_trip(self):
        event = Event()
        event.result.chain = [Plain("[at:10002][at:10003]")]
        await self.plugin.process_at_tags(event)
        self.assertEqual([str(x.qq) for x in event.result.chain], ["10002", "10003"])

    async def test_invalid_tags_stay_literal(self):
        event = Event()
        text = "[at:abc] [at:] [at:１２３] [at:alll]"
        event.result.chain = [Plain(text)]
        await self.plugin.process_at_tags(event)
        self.assertEqual(event.result.chain[0].text, text)

    async def test_ordinary_member_cannot_at_all(self):
        event = Event()
        event.result.chain = [Plain("通知[at:all]，谢谢[at:10002]")]
        await self.plugin.process_at_tags(event)
        self.assertFalse(any(isinstance(x, At) and str(x.qq) == "all" for x in event.result.chain))
        self.assertTrue(any(isinstance(x, At) and str(x.qq) == "10002" for x in event.result.chain))
        self.assertIn("@全体成员", "".join(x.text for x in event.result.chain if isinstance(x, Plain)))

    async def test_group_owner_and_admin_can_at_all(self):
        for role in ("owner", "admin"):
            event = Event(role=role)
            event.result.chain = [Plain("[at:all]通知")]
            await self.plugin.process_at_tags(event)
            self.assertEqual(event.result.chain[0].qq, "all")

    async def test_bot_admin_allowed_without_member_api(self):
        event = Event(super_admin=True)
        event.result.chain = [Plain("[at:all]")]
        await self.plugin.process_at_tags(event)
        self.assertEqual(event.result.chain[0].qq, "all")
        event.bot.api.call_action.assert_not_awaited()

    async def test_permission_lookup_failure_blocks_at_all(self):
        event = Event()
        event.bot.api.call_action.side_effect = RuntimeError("private diagnostics")
        event.result.chain = [Plain("[at:all]")]
        await self.plugin.process_at_tags(event)
        self.assertIsInstance(event.result.chain[0], Plain)

    async def test_native_at_all_component_checked(self):
        for component in (At(qq="all"), AtAll()):
            event = Event()
            event.result.chain = [component]
            await self.plugin.process_at_tags(event)
            self.assertIsInstance(event.result.chain[0], Plain)

    async def test_unknown_role_and_missing_sender_denied(self):
        for event in (Event(role="unknown"), Event(sender="")):
            event.result.chain = [Plain("[at:all]")]
            await self.plugin.process_at_tags(event)
            self.assertIsInstance(event.result.chain[0], Plain)

    async def test_permission_cached_only_for_same_event(self):
        event = Event(role="admin")
        await self.plugin.inject_at_instruction(event, ProviderRequest())
        event.result.chain = [Plain("[at:all]")]
        await self.plugin.process_at_tags(event)
        self.assertEqual(event.bot.api.call_action.await_count, 1)
        other = Event(role="member")
        other.result.chain = [Plain("[at:all]")]
        await self.plugin.process_at_tags(other)
        self.assertIsInstance(other.result.chain[0], Plain)

    async def test_get_members_filters_nickname_card_or_id(self):
        members = [
            {"user_id": 10001, "nickname": "张三", "card": "测试卡片", "role": "owner"},
            {"user_id": 10002, "nickname": "李四", "card": "", "role": "member"},
        ]
        for keyword in ("张三", "卡片", "10001", ""):
            event = Event()
            event.bot.api.call_action.return_value = members
            data = json.loads(await self.plugin.get_group_members(event, keyword))
            self.assertEqual(data["count"], 1 if keyword else 2)
            self.assertEqual(data["members"][0]["user_id"], "10001")
            event.bot.api.call_action.assert_awaited_once_with("get_group_member_list", group_id="20001")

    async def test_empty_lookup_is_successful_empty_result(self):
        event = Event()
        event.bot.api.call_action.return_value = []
        data = json.loads(await self.plugin.get_group_members(event, "不存在"))
        self.assertEqual(data, {"status": "success", "count": 0, "members": []})

    async def test_member_query_failure_does_not_leak_diagnostics(self):
        event = Event()
        event.bot.api.call_action.side_effect = RuntimeError("private diagnostics")
        data = await self.plugin.get_group_members(event)
        self.assertEqual(json.loads(data)["status"], "error")
        self.assertNotIn("private diagnostics", data)

    async def test_member_tool_rejects_private_and_foreign_events(self):
        for event in (Event(group=""), SimpleNamespace()):
            data = json.loads(await self.plugin.get_group_members(event))
            self.assertEqual(data["status"], "error")

    async def test_result_hook_ignores_empty_private_and_other_platform(self):
        await self.plugin.process_at_tags(Event())
        await self.plugin.process_at_tags(Event(group=""))
        await self.plugin.process_at_tags(SimpleNamespace())


if __name__ == "__main__":
    unittest.main()
