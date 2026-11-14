"""Concurrent remote commands propagate only their own request and session."""
from _bootstrap import TMP

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import nonebot

nonebot.init(driver="~none", localstore_use_cwd=True, log_level="CRITICAL")
mod = nonebot.load_plugin("nonebot_plugin_aigfm_peer").module


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_and_background_output_context(self):
        mod.plugin_config.aigfm_peer_push_port = 1
        mod.plugin_config.aigfm_peer_token = "test-only"
        payloads = []
        real_client = httpx.AsyncClient
        sessions = [mod._Session("onebot11:123", "onebot11", "123", "OneBot V11"),
                    mod._Session("onebot11:124", "onebot11", "124", "OneBot V11")]
        jobs = []

        async def execute(bot, key, *args):
            session = next(s for s in sessions if s.key == key)
            await asyncio.sleep(0)
            await mod._push("echo", session, text=key)
            jobs.append(asyncio.create_task(mod._push("echo", session, text="background")))
            # Proactive output to another group must never inherit this request ID.
            await mod._push("echo", next(s for s in sessions if s.key != key), text="other")

        def transport(request):
            import json
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})

        with patch.object(mod, "_execute_command_inner", execute), patch.object(mod.httpx, "AsyncClient",
                side_effect=lambda **kw: real_client(transport=httpx.MockTransport(transport), **kw)):
            await asyncio.gather(*(mod._execute_command(object(), s.key, int(s.native_chat_id), "echo", request_id="req-" + s.native_chat_id)
                                   for s in sessions))
            await asyncio.gather(*jobs)
            await mod._push("echo", sessions[0], text="unsolicited")
        self.assertIsNone(mod._request_context.get())
        for payload in payloads:
            if payload["text"] in {"other", "unsolicited"}:
                self.assertNotIn("request_id", payload)
            else:
                self.assertEqual(payload["request_id"], "req-" + payload["session"].split(":")[1])

    async def test_exception_resets_context_and_synthetic_does_not_replace_template(self):
        with patch.object(mod, "_execute_command_inner", AsyncMock(side_effect=RuntimeError("test"))):
            with self.assertRaises(RuntimeError):
                await mod._execute_command(object(), "onebot11:123", 123, "echo", request_id="failed")
        self.assertIsNone(mod._request_context.get())
        event = SimpleNamespace(aigf_synthetic=True, get_message=lambda: "echo")
        bot = SimpleNamespace(self_id="10000", adapter=SimpleNamespace(get_name=lambda: "OneBot V11"))
        fallback = mod._create_synthetic_event(bot, 123, "echo", user_id=111)
        self.assertTrue(getattr(fallback, mod.SYNTHETIC_FLAG))
        with patch.object(mod, "_cheap_session", side_effect=AssertionError("synthetic remembered")):
            await mod._remember_event(object(), event, {})
            await mod._remember_event(bot, fallback, {})

    async def test_capture_respects_actual_destination(self):
        from nonebot.internal.matcher import current_matcher, current_event
        mod.plugin_config.aigfm_peer_push_port = 1
        mod.plugin_config.aigfm_peer_capture_plugins = []
        bot = SimpleNamespace(adapter=SimpleNamespace(get_name=lambda: "OneBot V11"))
        original = mod._Session("onebot11:123", "onebot11", "123", "OneBot V11")
        matcher = current_matcher.set(SimpleNamespace(plugin_name="echo"))
        event = current_event.set(object())
        try:
            push = AsyncMock()
            with patch.object(mod, "_message_payload", return_value="message"), \
                 patch.object(mod, "_resolve_session", AsyncMock(return_value=original)), \
                 patch.object(mod, "_segments", AsyncMock(return_value=[{"type": "text", "text": "other group"}])), \
                 patch.object(mod, "_push", push):
                await mod.capture_outgoing(bot, "send_group_msg", {"group_id": 124, "message": "other group"})
            self.assertEqual(push.await_args.args[1].key, "onebot11:124")
        finally:
            current_event.reset(event)
            current_matcher.reset(matcher)


if __name__ == "__main__":
    unittest.main()
