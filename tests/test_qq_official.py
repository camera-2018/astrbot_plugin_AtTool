"""Run the real QQ adapter with mocked upload/send endpoints, never a live QQ call."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.provider import ProviderRequest
from astrbot.core.message.components import At, AtAll, Image, Plain, Reply, Video
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
    QQOfficialMessageEvent,
)
from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
    MessageSesion,
    Platform,
    PlatformMetadata,
    QQOfficialPlatformAdapter,
    botpy,
)

from main import LLMAtToolPlugin
from qq_official import KnownMembers, QQOfficialCompat, group_chunks

BOT, ALICE, BOB, GROUP = "A" * 32, "B" * 32, "C" * 32, "D" * 32
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jB9kAAAAASUVORK5CYII="


async def make_event(
    content="你好",
    mentions=(),
    *,
    group=GROUP,
    account="account-a",
    sender=ALICE,
    admin=False,
):
    raw = botpy.message.GroupMessage(
        None,
        "offline",
        {
            "id": "offline-message",
            "group_openid": group,
            "content": content,
            "author": {"member_openid": sender},
            "attachments": [],
        },
    )
    raw.author.username = "小明" if sender == ALICE else "小红"
    raw.mentions = [
        SimpleNamespace(id=uid, is_you=is_bot, username="睦头" if is_bot else "小红")
        for uid, is_bot in mentions
    ]
    message = await QQOfficialPlatformAdapter._parse_from_qqofficial(
        raw, MessageType.GROUP_MESSAGE
    )
    event = QQOfficialMessageEvent(
        message.message_str,
        message,
        PlatformMetadata("qq_official", "test", account),
        group,
        SimpleNamespace(
            api=SimpleNamespace(
                post_group_message=AsyncMock(return_value={"id": "sent"})
            )
        ),
    )
    event.role = "admin" if admin else "member"
    return event


class OfficialTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Host metrics otherwise open a real SQLite connection in short-lived test loops.
        metrics = patch(
            "astrbot.core.platform.astr_message_event.Metric.upload", AsyncMock()
        )
        metrics.start()
        self.addCleanup(metrics.stop)
        self.plugin = LLMAtToolPlugin(SimpleNamespace())
        self.compat = QQOfficialCompat()
        self.compat.install()

    def tearDown(self):
        self.compat.uninstall()

    async def test_bot_and_other_mentions_are_distinct_and_ordered(self):
        event = await make_event(
            f"<@{BOT}> 问问<@!{BOB}>好吗", [(BOT, True), (BOB, False)]
        )
        parts = event.get_messages()
        self.assertEqual([str(c.qq) for c in parts if isinstance(c, At)], [BOT, BOB])
        self.assertEqual(event.get_self_id(), BOT)
        self.assertIn("问问", event.message_str)
        self.assertNotIn("<@", event.message_str)
        self.assertIn(f"<@!{BOB}>", event.message_obj.raw_message.content)

    async def test_peer_mention_does_not_become_a_bot_mention(self):
        event = await make_event(f'前<qqbot-at-user id="{BOB}" />后', [(BOB, False)])
        self.assertEqual(
            [str(c.qq) for c in event.get_messages() if isinstance(c, At)], [BOB]
        )
        self.assertEqual(event.get_self_id(), "qq_official")
        self.assertNotEqual(event.get_messages()[1].qq, event.get_self_id())

    async def test_inline_openid_without_metadata_is_restored(self):
        event = await make_event(f"<@{BOB}> 你怎么看")
        self.assertEqual(event.get_messages()[0].qq, BOB)

    async def test_metadata_only_mention_is_restored_once(self):
        event = await make_event("你好", [(BOB, False), (BOB, False)])
        self.assertEqual(
            [str(c.qq) for c in event.get_messages() if isinstance(c, At)], [BOB]
        )

    async def test_quote_mentions_stay_inside_quote(self):
        event = await make_event("当前正文")
        quote = Reply(
            id="REFIDX_QUOTE",
            chain=[At(qq=BOB), Plain("旧正文")],
            message_str=f"<@{BOB}>旧正文",
        )
        event.message_obj.message.insert(0, quote)
        from qq_official import restore_mentions

        restore_mentions(event.message_obj)
        self.assertIs(event.get_messages()[0], quote)
        self.assertFalse(any(isinstance(c, At) for c in event.get_messages()))

    async def test_quote_only_metadata_does_not_create_top_level_mention(self):
        event = await make_event("当前正文")
        event.message_obj.raw_message.mentions = [
            SimpleNamespace(id=BOB, is_you=False, username="小红")
        ]
        event.message_obj.message.insert(
            0,
            Reply(id="REFIDX_QUOTE", chain=[At(qq=BOB)], message_str=f"<@{BOB}>旧正文"),
        )
        from qq_official import restore_mentions

        restore_mentions(event.message_obj)
        self.assertFalse(any(isinstance(c, At) for c in event.get_messages()))

    async def test_real_openid_model_tag_and_legacy_markup(self):
        event = await make_event()
        event.set_result(
            MessageEventResult(chain=[Plain(f"你好[at:{BOB}]，<@{ALICE}>")])
        )
        await self.plugin.process_at_tags(event)
        self.assertEqual(
            [str(c.qq) for c in event.get_result().chain if isinstance(c, At)],
            [BOB, ALICE],
        )
        self.assertTrue(event.get_result().use_markdown_)

    async def test_old_qq_number_is_not_treated_as_official_openid(self):
        event = await make_event()
        event.set_result(MessageEventResult(chain=[Plain("[at:12345678] [at:madeup]")]))
        await self.plugin.process_at_tags(event)
        self.assertFalse(any(isinstance(c, At) for c in event.get_result().chain))

    async def test_official_member_directory_is_scoped_and_incomplete(self):
        first = await make_event(f"<@{BOB}>", [(BOB, False)])
        await self.plugin.observe_members(first)
        data = json.loads(await self.plugin.get_group_members(first, "小红"))
        self.assertFalse(data["complete"])
        self.assertEqual(data["id_type"], "member_openid")
        self.assertEqual(data["members"][0]["user_id"], BOB)
        for other in [
            await make_event(group="E" * 32),
            await make_event(account="account-b"),
        ]:
            data = json.loads(await self.plugin.get_group_members(other, BOB))
            self.assertEqual(data["members"], [])
        first.bot.api.post_group_message.assert_not_awaited()

    async def test_member_directory_uses_stable_account_not_varying_self_id(self):
        first = await make_event(f"<@{BOT}>", [(BOT, True)])
        await self.plugin.observe_members(first)
        other = await make_event(sender=BOB)
        self.assertNotEqual(first.get_self_id(), other.get_self_id())
        data = json.loads(await self.plugin.get_group_members(other, ALICE))
        self.assertEqual(data["members"][0]["user_id"], ALICE)

    async def test_official_prefix_is_stable_and_permission_is_temporary(self):
        requests = []
        for admin, sender in [(False, ALICE), (True, BOB)]:
            event = await make_event(admin=admin, sender=sender)
            req = ProviderRequest(system_prompt="固定人格", prompt="问题")
            await self.plugin.inject_at_instruction(event, req)
            requests.append(req)
            self.assertNotIn(sender, req.system_prompt)
            assembled = await req.assemble_context()
            self.assertTrue(assembled["content"][-1]["_no_save"])
        self.assertEqual(requests[0].system_prompt, requests[1].system_prompt)
        self.assertIn("禁止", requests[0].extra_user_content_parts[-1].text)
        self.assertIn("允许", requests[1].extra_user_content_parts[-1].text)

    async def test_official_at_all_requires_bot_admin_including_raw_markup(self):
        for part in [
            AtAll(),
            At(qq="all"),
            Plain("[at:all]"),
            Plain("<qqbot-at-everyone />"),
        ]:
            event = await make_event()
            event.set_result(MessageEventResult(chain=[part]))
            await self.plugin.process_at_tags(event)
            self.assertEqual(event.get_result().chain[0].text, "@全体成员")

    async def test_quote_at_and_two_images_use_correct_real_send_payloads(self):
        event = await make_event()
        image1, image2 = Image.fromBase64(PNG), Image.fromBase64(PNG)
        chain = MessageChain(
            [Reply(id="REFIDX_TEST"), At(qq=BOB), Plain(" 看看这两张"), image1, image2]
        )
        chain.use_markdown_ = False
        original = list(chain.chain)
        with patch.object(
            QQOfficialMessageEvent,
            "upload_group_and_c2c_image",
            AsyncMock(return_value={"file_info": "mock"}),
        ):
            await event.send(chain)
        payloads = [c.kwargs for c in event.bot.api.post_group_message.await_args_list]
        self.assertEqual([x["msg_type"] for x in payloads], [2, 7, 7])
        self.assertIn(BOB, payloads[0]["markdown"]["content"])
        self.assertEqual(
            payloads[0]["message_reference"], {"message_id": "REFIDX_TEST"}
        )
        self.assertFalse(
            any("qqbot-at" in (x.get("content") or "") for x in payloads[1:])
        )
        self.assertEqual(chain.chain, original)
        self.assertFalse(chain.use_markdown_)
        self.assertIsNone(event.send_buffer)

    async def test_at_after_image_preserves_order(self):
        event = await make_event()
        with patch.object(
            QQOfficialMessageEvent,
            "upload_group_and_c2c_image",
            AsyncMock(return_value={"file_info": "mock"}),
        ):
            await event.send(
                MessageChain(
                    [Image.fromBase64(PNG), Plain("图注"), At(qq=BOB), Plain(" 你看")]
                )
            )
        payloads = [c.kwargs for c in event.bot.api.post_group_message.await_args_list]
        self.assertEqual([x["msg_type"] for x in payloads], [7, 2])
        self.assertEqual(payloads[0]["content"], "图注")

    async def test_raw_official_markup_and_video_do_not_leak_into_media_caption(self):
        event = await make_event()
        chain = MessageChain(
            [
                Plain(f'<qqbot-at-user id="{BOB}" /> 看视频'),
                Video(file="https://example.com/mock.mp4"),
            ]
        )
        with patch.object(
            QQOfficialMessageEvent,
            "upload_group_and_c2c_media",
            AsyncMock(return_value={"file_info": "mock"}),
        ):
            await event.send(chain)
        self.assertEqual(
            [
                c.kwargs["msg_type"]
                for c in event.bot.api.post_group_message.await_args_list
            ],
            [2, 7],
        )

    async def test_send_by_session_also_splits_mentions_and_media(self):
        adapter = object.__new__(QQOfficialPlatformAdapter)
        adapter._session_last_message_id = {GROUP: "cached-message"}
        adapter._session_scene = {GROUP: "group"}
        adapter._allow_group_proactive_send = True
        adapter.use_markdown_default = False
        adapter.client = SimpleNamespace(
            api=SimpleNamespace(
                post_group_message=AsyncMock(return_value={"id": "sent"})
            )
        )
        chain = MessageChain([At(qq=BOB), Plain(" 主动推送"), Image.fromBase64(PNG)])
        with (
            patch.object(
                QQOfficialMessageEvent,
                "upload_group_and_c2c_image",
                AsyncMock(return_value={"file_info": "mock"}),
            ),
            patch.object(Platform, "send_by_session", AsyncMock()),
        ):
            await adapter.send_by_session(
                MessageSesion("account-a", MessageType.GROUP_MESSAGE, GROUP), chain
            )
        payloads = [
            c.kwargs for c in adapter.client.api.post_group_message.await_args_list
        ]
        self.assertEqual([x["msg_type"] for x in payloads], [2, 7])
        self.assertNotIn("msg_id", payloads[0])

    async def test_admin_at_all_serializes_to_official_tag(self):
        event = await make_event(admin=True)
        event.set_result(MessageEventResult(chain=[Plain("[at:all] 通知")]))
        await self.plugin.process_at_tags(event)
        await event.send(event.get_result())
        payload = event.bot.api.post_group_message.await_args.kwargs
        self.assertIn("<qqbot-at-everyone />", payload["markdown"]["content"])

    async def test_empty_mention_does_not_separate_image_caption(self):
        image = Image.fromBase64(PNG)
        chain = MessageChain([Plain("图注"), At(qq=""), image])
        chunks = group_chunks(chain)
        self.assertEqual(len(chunks), 1)
        self.assertIs(chunks[0].chain[-1], image)

    async def test_send_failure_restores_original_buffer_and_does_not_send_media(self):
        event = await make_event()
        original = MessageChain([At(qq=BOB), Image.fromBase64(PNG)])
        event.bot.api.post_group_message.side_effect = RuntimeError("offline-failure")
        with patch.object(
            QQOfficialMessageEvent, "upload_group_and_c2c_image", AsyncMock()
        ) as upload:
            with self.assertRaisesRegex(RuntimeError, "offline-failure"):
                await event.send(original)
        self.assertIs(event.send_buffer, original)
        self.assertEqual(event.bot.api.post_group_message.await_count, 1)
        upload.assert_not_awaited()

    async def test_official_private_chat_remains_one_message_and_has_no_group_tools(
        self,
    ):
        raw = botpy.message.C2CMessage(
            None,
            "offline",
            {
                "id": "offline-private",
                "content": "私聊",
                "author": {"user_openid": ALICE},
                "attachments": [],
            },
        )
        message = await QQOfficialPlatformAdapter._parse_from_qqofficial(
            raw, MessageType.FRIEND_MESSAGE
        )
        event = QQOfficialMessageEvent(
            message.message_str,
            message,
            PlatformMetadata("qq_official", "test", "account-a"),
            ALICE,
            SimpleNamespace(),
        )
        req = ProviderRequest(system_prompt="原人格")
        await self.plugin.inject_at_instruction(event, req)
        self.assertEqual(req.system_prompt, "原人格")
        self.assertEqual(
            json.loads(await self.plugin.get_group_members(event))["status"], "error"
        )
        with (
            patch.object(
                QQOfficialMessageEvent,
                "upload_group_and_c2c_image",
                AsyncMock(return_value={"file_info": "mock"}),
            ),
            patch.object(
                QQOfficialMessageEvent,
                "post_c2c_message",
                AsyncMock(return_value={"id": "sent"}),
            ) as send,
        ):
            await event.send(MessageChain([Plain("私聊图注"), Image.fromBase64(PNG)]))
        send.assert_awaited_once()
        self.assertEqual(send.await_args.kwargs["msg_type"], 7)

    async def test_unload_does_not_overwrite_another_plugins_newer_wrapper(self):
        old = QQOfficialPlatformAdapter._parse_from_qqofficial

        async def another(*args, **kwargs):
            return await old(*args, **kwargs)

        QQOfficialPlatformAdapter._parse_from_qqofficial = staticmethod(another)
        try:
            self.compat.uninstall()
            self.assertIs(QQOfficialPlatformAdapter._parse_from_qqofficial, another)
            event = await make_event(f"<@{BOB}>", [(BOB, False)])
            self.assertFalse(any(isinstance(c, At) for c in event.get_messages()))
        finally:
            # Restore the true original, not the inactive wrapper.
            QQOfficialPlatformAdapter._parse_from_qqofficial = staticmethod(
                old.__wrapped__
            )

    async def test_no_mention_keeps_metadata_and_text(self):
        chain = MessageChain([Plain("原文  \n"), Image.fromBase64(PNG)])
        chain.use_markdown_ = False
        chain.use_t2i_ = True
        chunk = group_chunks(chain)[0]
        self.assertFalse(chunk.use_markdown_)
        self.assertTrue(chunk.use_t2i_)
        self.assertEqual(chunk.chain, chain.chain)

    async def test_reinstall_is_idempotent_and_unload_restores_methods(self):
        wrapped = QQOfficialPlatformAdapter._parse_from_qqofficial
        self.compat.install()
        self.assertIs(QQOfficialPlatformAdapter._parse_from_qqofficial, wrapped)
        self.compat.uninstall()
        event = await make_event(f"<@{BOB}>", [(BOB, False)])
        self.assertFalse(any(isinstance(c, At) for c in event.get_messages()))


class DirectoryBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def test_ttl_and_room_bounds(self):
        now = [0]
        directory = KnownMembers(
            clock=lambda: now[0], max_rooms=1, max_members=1, ttl=10
        )
        first = await make_event()
        directory.observe(first)
        now[0] = 11
        self.assertEqual(directory.lookup(first)[0], [])
        other = await make_event(account="other-account")
        directory.observe(other)
        self.assertEqual(len(directory.rooms), 1)


if __name__ == "__main__":
    unittest.main()
