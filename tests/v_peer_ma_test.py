"""离线验证：peer 插件跨适配器（会话 / 通用段 / 命令扫描 / 捕获推送 / 复制事件调用）"""
from _bootstrap import REPO, SOURCE, TMP, close_loop


import asyncio
import base64
import os
import shutil
import sys
from pathlib import Path

_LOOP = asyncio.new_event_loop()          # Python 3.14 起 get_event_loop() 不再自动建 loop
asyncio.set_event_loop(_LOOP)
close_loop(_LOOP)

if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
os.chdir(TMP)

import nonebot  # noqa: E402

nonebot.init(
    driver="~none",
    localstore_use_cwd=True,
    log_level="CRITICAL",
    command_start={""},
    superusers={"945126014"},
    aigfm_peer_push_port=14514,
    aigfm_peer_token="shared_secret_123",
    aigfm_peer_bot_name="botB",
    aigfm_peer_capture_plugins=[],
)
peers = nonebot.load_plugin("nonebot_plugin_aigfm_peer").module

from nonebot.adapters.onebot.v11 import (  # noqa: E402
    Bot as OB11Bot, Message as OB11Message, MessageSegment as OB11Segment,
    GroupMessageEvent, PrivateMessageEvent,
)
from nonebot.message import handle_event  # noqa: E402
from nonebot.internal.matcher import current_event, current_matcher  # noqa: E402
from nonebot_plugin_alconna import Alconna, Args, CommandResult, MultiVar, on_alconna  # noqa: E402
from nonebot import on_command  # noqa: E402
from nonebot.adapters import Event  # noqa: E402
from nonebot_plugin_aigfm_peer.config import plugin_config  # noqa: E402

FAILED = []
PASSED = 0


def check(name, actual, expected):
    global PASSED
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'} | {name} | got={actual!r}" + ("" if ok else f" want={expected!r}"))
    if ok:
        PASSED += 1
    else:
        FAILED.append(name)


def run(coro):
    return _LOOP.run_until_complete(coro)


# ========== 桩 ==========

class FakeResp:
    def raise_for_status(self):
        pass


class FakeClient:
    """记录推送给 Bot A 的请求体"""
    payloads: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        FakeClient.payloads.append({"url": url, "json": json, "headers": headers})
        return FakeResp()


peers.httpx.AsyncClient = FakeClient


class MockAdapter:
    def __init__(self, name="OneBot V11", message_class=OB11Message):
        self._name = name
        self._message_class = message_class
        self.config = nonebot.get_driver().config

    def get_name(self):
        return self._name

    def get_message_class(self):
        return self._message_class

    async def _call_api(self, bot, api, **data):
        return {}


class MockBot(OB11Bot):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = []

    async def call_api(self, api, **data):
        self.calls.append((api, data))
        if api == "get_group_info":
            return {"group_id": int(data.get("group_id") or 0), "group_name": "测试群"}
        if api == "get_group_member_info":
            uid = int(data.get("user_id") or 0)
            return {"user_id": uid, "nickname": f"用户{uid}", "card": "", "role": "member", "sex": "male"}
        return {}


def make_bot(name="OneBot V11", message_class=OB11Message) -> MockBot:
    return MockBot(adapter=MockAdapter(name, message_class), self_id="10000")


_seq = iter(range(1000, 100000))


def group_event(text="hi", user_id=20001, group_id=617770183, message=None, message_id=None):
    msg = message if message is not None else OB11Message(text)
    return GroupMessageEvent(
        time=1700000000, self_id=10000, post_type="message", sub_type="normal",
        message_type="group", user_id=user_id, group_id=group_id,
        message_id=message_id if message_id is not None else next(_seq),
        message=msg, raw_message=msg.extract_plain_text(), font=0,
        sender={"user_id": user_id, "nickname": "张三", "role": "member"},
    )


PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/"
           "q842iQAAAABJRU5ErkJggg==")
PNG_BYTES = base64.b64decode(PNG_B64)
LOCAL_PNG = TMP / "local.png"
LOCAL_PNG.write_bytes(PNG_BYTES)

# ========== A. 会话 ==========

print("\n=== A. 会话 ===")
check("A1 slug", peers._adapter_slug("OneBot V11"), "onebot11")
check("A2 slug 其它适配器", peers._adapter_slug("Satori"), "satori")
check("A3 枚举取 value", peers._adapter_slug(__import__("nonebot_plugin_uninfo", fromlist=["SupportAdapter"]).SupportAdapter.onebot11), "onebot11")

bot = make_bot()
ev = group_event()
sess = run(peers._resolve_session(bot, ev))
check("A4 群会话键", sess.key, "onebot11:617770183")
check("A5 原生 id", sess.native_chat_id, "617770183")
check("A6 适配器名", sess.adapter, "OneBot V11")
check("A7 非私聊", sess.is_private, False)

priv = PrivateMessageEvent(
    time=1700000000, self_id=10000, post_type="message", sub_type="friend",
    message_type="private", user_id=20001, message_id=next(_seq), message=OB11Message("你好"),
    raw_message="你好", font=0, sender={"user_id": 20001, "nickname": "张三", "role": "member"},
)
sess_p = run(peers._resolve_session(make_bot(), priv))
check("A8 私聊会话键", sess_p.key, "onebot11:20001")
check("A9 私聊标记", sess_p.is_private, True)
from_api = peers._session_from_api_data(make_bot(), {"group_id": 617770183})
check("A10 从 API 参数拼会话", from_api.key, "onebot11:617770183")
check("A11 从 API 参数拼私聊", peers._session_from_api_data(make_bot(), {"user_id": 123}).key, "onebot11:123")

# ========== B. 通用段解析 ==========

print("\n=== B. 通用段解析 ===")


def segs(onebot_message, bot=None):
    return run(peers._segments(bot or make_bot(), onebot_message))


check("B1 纯文本", segs(OB11Message("插件输出")), [{"type": "text", "text": "插件输出"}])
check("B2 @ 渲染", segs(OB11Message("你好 [CQ:at,qq=20002]")),
      [{"type": "text", "text": "你好 "}, {"type": "text", "text": "@20002 "}])
img = segs(OB11Message(f"[CQ:image,file=base64://{PNG_B64}]"))
check("B3 图片段", (img[0]["type"], img[0]["base64"] == PNG_B64), ("image", True))
check("B4 合并转发", segs(OB11Message("[CQ:forward,id=f1]")), [{"type": "text", "text": "[收到一条合并聊天记录]"}])
check("B5 xml", segs(OB11Message("[CQ:xml,data=abc]")), [{"type": "text", "text": "[收到一条XML消息]"}])
check("B6 json", segs(OB11Message([OB11Segment.json('{"title":"标题","desc":"描述"}')])),
      [{"type": "text", "text": "[小程序/卡片: 标题, 描述] "}])
check("B7 QQ表情", segs(OB11Message("[CQ:face,id=14,text=微笑]")), [{"type": "text", "text": "[QQ表情 微笑] "}])
check("B8 语音", segs(OB11Message("[CQ:record,file=a.mp3]")), [{"type": "text", "text": "[收到一条语音消息]"}])
check("B9 群文件", segs(OB11Message("[CQ:file,file_id=x,file_name=a.txt]")), [{"type": "text", "text": "[群文件: a.txt] "}])
check("B10 分享链接",
      segs(OB11Message("[CQ:share,title=标题,url=http://a.b,content=说明]")),
      [{"type": "text", "text": "[分享链接: 标题 | http://a.b | 说明] "}])
check("B11 骰子", segs(OB11Message("[CQ:dice,result=3]")), [{"type": "text", "text": "[骰子 3] "}])
check("B12 位置", segs(OB11Message("[CQ:location,title=家,lat=1,lon=2]")), [{"type": "text", "text": "[位置: 家 | 1,2] "}])
check("B13 本地路径图片",
      Path(segs(OB11Message([OB11Segment.image(LOCAL_PNG)]))[0]["file"]) == LOCAL_PNG, True)
# 无支持适配器的兜底
check("B14 字符串消息", segs("纯文本消息"), [{"type": "text", "text": "纯文本消息"}])

# ========== C. 命令扫描（on_command + alconna） ==========

print("\n=== C. 命令扫描 ===")


@on_command("peercmd", aliases={"pc双"}).handle()
async def _c1(bot, event):
    pass


peercmd_alc = on_alconna(Alconna("peeralc", Args["rest", MultiVar(str, "*")]), aliases={"pa双"})


@peercmd_alc.handle()
async def _(event: Event, result: CommandResult):
    pass


scanned = peers._scan_commands()
names = {c["name"]: c["plugin"] for c in scanned}
check("C1 on_command 被发现", "peercmd" in names, True)
check("C2 alconna 命令被发现", "peeralc" in names, True)
# 脚本里创建的 matcher 没有插件上下文，plugin_name 为 None → 记成 unknown（线上会是真实插件名）
check("C3 归属别名映射（on_command）", "pc双" in peers._command_plugin_map, True)
check("C4 归属映射（alconna 主名）", "peeralc" in peers._command_plugin_map, True)
check("C5 归属映射（alconna 别名）", "pa双" in peers._command_plugin_map, True)
nonebot.get_driver().config.command_start = {"/"}
check("C6 命令头去前缀", peers._command_head("/peercmd 参数"), "peercmd")
nonebot.get_driver().config.command_start = {""}

# ========== D. 捕获推送 ==========

print("\n=== D. 捕获推送 ===")


async def fire_hooks(bot, api, data):
    from nonebot.adapters import Bot
    for hook in list(Bot._calling_api_hook):
        await hook(bot, api, data)


class FakeMatcher:
    def __init__(self, name):
        self.plugin_name = name


def capture(data=None, source="other_plugin", event=None, bot=None):
    FakeClient.payloads.clear()
    bot = bot or make_bot()
    tok_e = current_event.set(event if event is not None else group_event())
    tok_m = current_matcher.set(FakeMatcher(source))
    try:
        run(fire_hooks(bot, "send_msg", data if data is not None else {
            "group_id": 617770183, "message": OB11Message("插件输出")}))
    finally:
        current_event.reset(tok_e)
        current_matcher.reset(tok_m)
    return FakeClient.payloads


payloads = capture()
check("D1 推送的 URL", payloads[0]["url"], "http://127.0.0.1:14514/peer/capture")
check("D2 会话键进入 payload", payloads[0]["json"]["session"], "onebot11:617770183")
check("D3 兼容字段 group_id", payloads[0]["json"]["group_id"], 617770183)
check("D4 适配器字段", payloads[0]["json"]["adapter"], "OneBot V11")
check("D5 文本内容", payloads[0]["json"]["text"], "插件输出")
check("D6 认证头", payloads[0]["headers"]["Authorization"], "Bearer shared_secret_123")
check("D7 带 bot 名", payloads[0]["json"]["bot_name"], "botB")
check("D8 带命令清单字段", "commands" in payloads[0]["json"], True)

check("D9 跳过自身", capture(source="nonebot_plugin_aigfm_peer"), [])
plugin_config.aigfm_peer_capture_plugins = ["allowed"]
check("D10 白名单外不推", capture(source="not_allowed"), [])
check("D11 白名单内推送", len(capture(source="allowed")), 1)
plugin_config.aigfm_peer_capture_plugins = []

check("D12 非发消息调用不推", capture(data={"group_id": 617770183, "user_id": 2}), [])
# 本地文件图片 → base64 后推送
payloads = capture(data={"group_id": 617770183, "message": OB11Message([OB11Segment.image(LOCAL_PNG)])})
check("D13 本地图片转 base64", payloads[0]["json"]["image_base64"] == PNG_B64, True)
check("D14 图片推送类型", payloads[0]["json"]["text"], "")
# 其它适配器（无 group_id，靠事件解析）
console_image = None
payloads = capture(data={"channel_id": "console-chat", "message": OB11Message("控制台插件输出")},
                   event=None)
check("D15 事件上下文优先（群参数 + 事件 → 群会话）", payloads[0]["json"]["session"], "onebot11:617770183")

# ========== E. 记住事件 + 远程调用（复制真实事件） ==========

print("\n=== E. 远程调用 ===")

seen = {}


@on_command("dummy", priority=50).handle()
async def _dummy(bot, event):
    seen.update({
        "message": str(event.get_message()),
        "raw": getattr(event, "raw_message", None),
        "user_id": event.get_user_id(),
        "nickname": getattr(getattr(event, "sender", None), "nickname", None),
        "message_id": getattr(event, "message_id", None),
        "reply": getattr(event, "reply", "MISSING"),
        "event_class": type(event).__name__,
        "chat_id": getattr(getattr(event, "channel", None), "id", None),
        "synthetic": bool(getattr(event, "__pydantic_extra__", {}).get("aigf_synthetic", False))
        if isinstance(getattr(event, "__pydantic_extra__", None), dict) else False,
    })


dummy_bot = make_bot()
dummy_event = group_event(text="原始消息", message_id=777)
run(handle_event(dummy_bot, dummy_event))
check("E1 真实事件被记住", "onebot11:617770183" in peers._last_events, True)

seen.clear()
run(peers._execute_command(dummy_bot, "onebot11:617770183", 617770183, "dummy", 20001,
                           parts=[{"type": "at", "target": "20002"}, {"type": "text", "content": "10"}],
                           sender_name="小助手"))
check("E2 目标插件被调用", seen.get("message"), "dummy [CQ:at,qq=20002] 10")
check("E3 raw_message", seen.get("raw"), "dummy  10")
check("E4 身份", seen.get("user_id"), "20001")
check("E5 发送者昵称用 Bot A 预设名", seen.get("nickname"), "小助手")
check("E6 保留真实 message_id", seen.get("message_id"), 777)
check("E7 清空 reply", seen.get("reply"), None)
check("E8 合成标记", seen.get("synthetic"), True)
check("E9 原事件未被改动", dummy_event.get_message().extract_plain_text(), "原始消息")

# alconna 目标插件（验证 message_id 缓存被刷新）
alc_seen = []
alc_target = on_alconna(Alconna("alcdummy", Args["rest", MultiVar(str, "*")]), priority=50, block=True)


@alc_target.handle()
async def _(event: Event, result: CommandResult):
    alc_seen.append((str(event.get_message()), dict(result.result.all_matched_args)))


alc_bot = make_bot()
alc_event = group_event(text="这是原消息")
run(handle_event(alc_bot, alc_event))
alc_seen.clear()
run(peers._execute_command(alc_bot, "onebot11:617770183", 617770183, "alcdummy", 20001,
                           parts=[{"type": "text", "content": "参数"}]))
check("E10 alconna 目标读到合成命令", alc_seen[-1] if alc_seen else None,
      ("alcdummy 参数", {"rest": ("参数",)}))

# 无模板事件 + OneBot → 手搓兜底
peers._last_events.clear()
seen.clear()
run(peers._execute_command(dummy_bot, "onebot11:999", 999, "dummy", 20001, sender_name="小助手"))
check("E11 无模板时 OneBot 兜底可用", seen.get("message"), "dummy")
check("E12 兜底事件的 message_id 为 0", seen.get("message_id"), 0)

# 无模板 + 非 OneBot → 不执行（有明确告警）
try:
    from nonebot.adapters.console import Message as ConsoleMessage, MessageEvent as ConsoleMessageEvent
    console_ok = True
except Exception as e:
    console_ok = False
    print("      console 适配器不可用:", e)

if console_ok:
    c_bot = make_bot(name="Console", message_class=ConsoleMessage)
    c_event = ConsoleMessageEvent(
        time=1700000000, self_id="console", type="message", detail_type="group",
        message_id="c1", message=ConsoleMessage("跨适配器你好"),
        user={"id": "u1", "nickname": "控制台用户"},
        channel={"id": "console-chat", "name": "控制台群"},
    )
    c_sess = run(peers._resolve_session(c_bot, c_event))
    check("E13 console 会话键", c_sess.key, "console:console-chat")
    check("E14 console 段解析", run(peers._segments(c_bot, c_event.get_message())),
          [{"type": "text", "text": "跨适配器你好"}])
    # console 事件也能被记住，并作为模板复制执行
    run(handle_event(c_bot, c_event))
    check("E15 console 事件被记住", "console:console-chat" in peers._last_events, True)
    seen.clear()
    run(peers._execute_command(c_bot, "console:console-chat", 0, "dummy", 20001))
    check("E16 console 上复制事件执行命令", seen.get("message"), "dummy")
    check("E17 console 事件类型与会话被保留", (seen.get("event_class"), seen.get("chat_id")),
          ("MessageEvent", "console-chat"))
    check("E18 无模板 + 非 OneBot 不执行（无告警崩溃）", run(peers._execute_command(
        c_bot, "console:other", 0, "dummy", 20001)) is None, True)

# ========== F. 启动扫描 ==========

print("\n=== F. 启动 ===")
try:
    run(peers._on_startup())
    check("F1 启动不抛异常", True, True)
except Exception as e:
    check("F1 启动不抛异常", f"{type(e).__name__}: {e}", True)
check("F2 启动扫描到命令（空白名单不上报）", peers._commands_cache, [])
plugin_config.aigfm_peer_capture_plugins = ["unknown"]      # 脚本里 matcher 的 plugin_name 为 None → unknown
run(peers._on_startup())
check("F3 白名单内命令被上报",
      sorted(c["name"] for c in peers._commands_cache if c["name"] in ("peercmd", "peeralc")),
      ["peeralc", "peercmd"])
plugin_config.aigfm_peer_capture_plugins = []

# ========== 结果 ==========

print("\n====================")
print(f"PASS {PASSED} | FAIL {len(FAILED)}")
if FAILED:
    print("失败项:")
    for name in FAILED:
        print("  -", name)
print("====================")
sys.exit(1 if FAILED else 0)
