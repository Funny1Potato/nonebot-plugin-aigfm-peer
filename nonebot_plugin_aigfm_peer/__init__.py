"""nonebot-plugin-aigfm-peer — 跨 bot 通信代理（安装在其它 bot 上）

职责：
1. 捕获本 bot 所有插件输出（文本/图片），通过 HTTP 推送给 Bot A（aigf_manager）
2. 提供 POST /peer/invoke 端点，接收 Bot A 的远程命令调用，用「复制真实事件」的方式执行本地插件

0.4.0 起收发与会话都走通用层：消息用 nonebot_plugin_alconna 的 uniseg、
会话用 nonebot_plugin_uninfo（回落 alconna Target），因此适配器被这两个库支持时即可工作。
推送/调用协议向后兼容旧的仅 OneBot 形式（仍带 group_id 字段）。
"""

import asyncio
import base64
import json
import re
from collections.abc import MutableMapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime

import anyio
import httpx
from nonebot import Bot, get_bot, get_driver, logger, require
from nonebot.internal.matcher import current_event, current_matcher
from nonebot.matcher import matchers
from nonebot.message import event_preprocessor, handle_event
from nonebot.plugin import PluginMetadata, inherit_supported_adapters
from nonebot.rule import CommandRule

require("nonebot_plugin_alconna")
require("nonebot_plugin_uninfo")
from nonebot_plugin_alconna import (  # noqa: E402
    At, Hyper, Image, Other, Reference, Text, UniMessage, get_message_id, get_target,
)
from nonebot_plugin_uninfo import get_session  # noqa: E402

from .config import PluginConfig, plugin_config  # noqa: E402

__plugin_name__ = "nonebot-plugin-aigfm-peer"

__plugin_meta__ = PluginMetadata(
    name="aigfm-peer", description="跨 bot 通信代理插件：捕获本 bot 插件输出推送给主 bot，并支持远程插件调用",
    usage="安装到其它 bot 后自动工作；配置 AIGFM_PEER_PUSH_PORT / AIGFM_PEER_TOKEN / AIGFM_PEER_BOT_NAME",
    type="application",
    config=PluginConfig,
    # 捕获与会话走 alconna/uninfo，因此只声明两者共同支持的适配器
    # （仅「没有可复制事件时的兜底调用」额外需要 OneBot，属可选能力）
    supported_adapters=inherit_supported_adapters(
        "nonebot_plugin_alconna",
        "nonebot_plugin_uninfo",
    ),
    homepage="https://github.com/Funny1Potato/nonebot-plugin-aigfm-peer",
    extra={"author": "Funny1Potato"},
)

_invoke_tasks: set = set()
_commands_cache: list[dict] = []
_command_plugin_map: dict[str, str] = {}

SELF_PLUGIN_NAMES = ("nonebot_plugin_aigfm_peer", "nonebot-plugin-aigfm-peer")

# 消息体可能出现的 api 参数名（不同适配器的发送接口用不同字段名）
_PAYLOAD_KEYS = ("message", "messages", "content", "text", "msg")

# 合成事件标记（与 Bot A 同一约定，便于排查）
SYNTHETIC_FLAG = "aigf_synthetic"

_ADAPTER_ALIASES = {"OneBot V11": "onebot11", "OneBot V12": "onebot12"}


# ========== 会话（适配器:会话id） ==========

def _adapter_name_of(value) -> str:
    """适配器名归一：SupportAdapter 枚举在 3.10 下 str() 是 'SupportAdapter.xxx'，要取 .value"""
    if value is None:
        return ""
    return str(getattr(value, "value", value) or "")


def _adapter_slug(name) -> str:
    name = _adapter_name_of(name).strip()
    if name in _ADAPTER_ALIASES:
        return _ADAPTER_ALIASES[name]
    return re.sub(r"[^0-9a-z]+", "", name.lower()) or "unknown"


@dataclass
class _Session:
    key: str
    slug: str
    native_chat_id: str
    adapter: str
    is_private: bool = False


def _cheap_session(bot: Bot, event) -> _Session | None:
    """不发 API 请求地推断会话（alconna Target）；解析不出返回 None"""
    try:
        target = get_target(event, bot)
    except Exception as e:
        logger.debug(f"[PeerAgent] 无法解析会话: {e}")
        return None
    slug = _adapter_slug(bot.adapter.get_name())
    path = f"{target.parent_id}_{target.id}" if (target.channel and target.parent_id) else target.id
    return _Session(key=f"{slug}:{path}", slug=slug, native_chat_id=str(target.id),
                    adapter=bot.adapter.get_name(), is_private=bool(target.private))


async def _resolve_session(bot: Bot, event) -> _Session | None:
    """优先 uninfo（场景路径更准确），回落 alconna Target"""
    try:
        sess = await get_session(bot, event)
    except Exception as e:
        logger.debug(f"[PeerAgent] uninfo 解析失败: {e}")
        sess = None
    if sess is not None:
        try:
            slug = _adapter_slug(sess.adapter)
            return _Session(key=f"{slug}:{sess.scene_path}", slug=slug,
                            native_chat_id=str(sess.scene.id), adapter=bot.adapter.get_name(),
                            is_private=sess.scene.is_private)
        except Exception as e:
            logger.debug(f"[PeerAgent] uninfo 会话结构异常: {e}")
    return _cheap_session(bot, event)


def _session_from_api_data(bot: Bot, data: dict) -> _Session | None:
    """没有事件上下文时从 API 参数拼会话（群/频道优先，其次私聊）"""
    slug = _adapter_slug(bot.adapter.get_name())
    chat_id = data.get("group_id") or data.get("channel_id") or data.get("chat_id")
    if chat_id:
        guild_id = data.get("guild_id")
        path = f"{guild_id}_{chat_id}" if guild_id else str(chat_id)
        return _Session(key=f"{slug}:{path}", slug=slug, native_chat_id=str(chat_id),
                        adapter=bot.adapter.get_name())
    user_id = data.get("user_id")
    if user_id:
        return _Session(key=f"{slug}:{user_id}", slug=slug, native_chat_id=str(user_id),
                        adapter=bot.adapter.get_name(), is_private=True)
    return None


# ========== 命令扫描（on_command + alconna） ==========

def _command_head(command: str) -> str:
    """去掉命令前缀并取主命令名（用于调用核对）"""
    command = command.strip()
    try:
        command_start = get_driver().config.command_start
    except Exception:
        command_start = set()
    for prefix in sorted((p for p in command_start if p), key=len, reverse=True):
        if command.startswith(prefix):
            command = command[len(prefix):]
            break
    return command.split(None, 1)[0] if command else command


def _alconna_names(matcher_cls) -> list[str]:
    """alconna 响应器的命令名与别名

    别名在 alconna 里注册成 shortcut，`get_shortcuts()` 返回形如 `"'别名 ...args'"` 的字符串，
    因此取第一段再去掉引号；旧版本/无该方法时回落 `command.shortcuts` 的键。
    """
    try:
        cmd = matcher_cls.command()
    except Exception:
        return []
    if cmd is None:
        return []
    names: list[str] = []
    main = getattr(cmd, "command", None) or getattr(cmd, "name", None)
    if main:
        names.append(str(main))
    raw = None
    try:
        getter = getattr(cmd, "get_shortcuts", None)
        raw = getter() if callable(getter) else getattr(cmd, "shortcuts", None)
    except Exception:
        raw = getattr(cmd, "shortcuts", None)
    keys: list[str] = []
    if isinstance(raw, dict):
        keys = [str(k) for k in raw]
    elif isinstance(raw, (list, tuple, set)):
        keys = [str(item).split()[0] for item in raw if str(item).strip()]
    for key in keys:
        key = key.strip().strip("'\"").strip()
        if key and key not in names:
            names.append(key)
    return names


def _scan_commands() -> list[dict]:
    """全量扫描本 bot 的命令（on_command 与 alconna 都支持）

    填充 _command_plugin_map（调用核对用），返回 [{name, plugin, description}]
    """
    _command_plugin_map.clear()
    commands: list[dict] = []
    seen = set()
    for priority, matcher_list in matchers.items():
        for matcher_cls in matcher_list:
            try:
                names: list[str] = []
                try:
                    rule = matcher_cls.rule
                    for checker in (rule.checkers if rule else []):
                        if isinstance(checker.call, CommandRule) and checker.call.cmds:
                            main_cmd = checker.call.cmds[0]
                            if main_cmd and main_cmd[0]:
                                names.append(main_cmd[0])
                            for cmd_tuple in checker.call.cmds[1:]:
                                if cmd_tuple and cmd_tuple[0]:
                                    names.append(cmd_tuple[0])
                            break
                except Exception:
                    pass
                if not names:
                    names = _alconna_names(matcher_cls)
                if not names:
                    continue
                plugin_name = matcher_cls.plugin_name or "unknown"
                description = ""
                if matcher_cls.plugin and hasattr(matcher_cls.plugin, "metadata"):
                    meta = matcher_cls.plugin.metadata
                    if meta:
                        description = meta.description or ""
                # 主名与别名各上报一条：nonebot 的 on_command 把 {主名} | {别名} 放进 set，
                # cmds 顺序不保证，只取第一个会让「上报的命令名」随机变成某个别名
                for name in names:
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    commands.append({"name": name, "plugin": plugin_name, "description": description})
                    _command_plugin_map.setdefault(name, plugin_name)
            except Exception:
                continue
    return commands


# ========== 消息解析（通用段） ==========

_SEG_LABELS = {
    "rps": "猜拳", "shake": "窗口抖动", "anonymous": "匿名消息", "node": "合并聊天记录节点",
}


def _file_uri_to_path(uri: str) -> str:
    """file:///D:/a/b.png → D:/a/b.png；file:///tmp/a.png → /tmp/a.png"""
    path = uri[len("file://"):]
    if path.startswith("/") and len(path) > 3 and path[2] == ":":
        path = path[1:]          # Windows 的 /D:/... 形式
    return path


def _extract_json_desc(json_str: str) -> str:
    """从 JSON 消息中提取小程序/卡片的 title 和 desc"""
    try:
        data = json.loads(json_str) if isinstance(json_str, str) else json_str
        if isinstance(data, dict):
            title = data.get("title", "")
            desc = data.get("desc", "")
            if not title and "meta" in data:
                meta = data["meta"]
                if isinstance(meta, dict):
                    for v in meta.values():
                        if isinstance(v, dict):
                            title = v.get("title", title) or title
                            desc = v.get("desc", desc) or desc
            if title or desc:
                return f"[小程序/卡片: {', '.join(p for p in (title, desc) if p)}] "
        return "[收到一条JSON消息] "
    except (json.JSONDecodeError, TypeError):
        return "[收到一条JSON消息] "


def _extract_image_data(data: dict) -> dict:
    """从图片段落的 data 中提取图片数据，处理 base64://、file:// 与 http(s) url 前缀"""
    file_value = data.get("file", "") or ""
    url = data.get("url", "") or ""
    b64 = data.get("base64", "") or ""

    if file_value.startswith("base64://"):
        b64 = file_value[9:]
        file_value = ""
    elif file_value.startswith("file://"):
        file_value = _file_uri_to_path(file_value)
    elif file_value.startswith(("http://", "https://")) and not url:
        # MessageSegment.image(url) 时 url 会被 OneBot 适配器放在 file 字段
        url = file_value
        file_value = ""

    return {"type": "image", "url": url, "file": file_value, "base64": b64}


def _classify_image_value(value: str) -> dict:
    text = (value or "").strip()
    if not text:
        return {}
    if text.startswith("base64://"):
        return {"base64": text[9:]}
    if text.startswith("data:"):
        return {"base64": text.partition(",")[2]}
    if text.startswith(("http://", "https://")):
        return {"url": text}
    if text.startswith("file://"):
        return {"file": _file_uri_to_path(text)}
    return {"file": text}


def _demangle_url(value: str) -> str:
    """uniseg 会给没有 hostname 的 url 强补 https://（本地路径也会被补），这里还原"""
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            rest = value[len(prefix):]
            if "\\" in rest or rest.startswith("/") or rest.startswith("file://") or (
                len(rest) > 2 and rest[1] == ":"
            ):
                return rest
    return value


def _image_data(seg) -> dict | None:
    """Image 段 / 落进 Other 的图片类原始段 → {"url","file","base64"}"""
    origin = getattr(seg, "origin", None)
    if getattr(origin, "type", None) in ("image", "emoji"):
        extracted = _extract_image_data(getattr(origin, "data", {}) or {})
        if any(extracted[key] for key in ("url", "file", "base64")):
            return extracted
    if not isinstance(seg, Image):
        return None
    out = {"url": "", "file": "", "base64": ""}

    def apply(value: str):
        for key, item in _classify_image_value(_demangle_url(value)).items():
            if not out.get(key):
                out[key] = item

    if seg.url:
        apply(str(seg.url))
    if seg.path:
        out["file"] = out["file"] or str(seg.path)
    raw = getattr(seg, "raw", None)
    if raw is not None:
        if hasattr(raw, "getvalue"):
            raw = raw.getvalue()
        if raw:
            out["base64"] = out["base64"] or base64.b64encode(bytes(raw)).decode()
    if seg.id:
        apply(str(seg.id))
    return out if any(out.values()) else None


def _extra_text(seg) -> str:
    """alconna 未映射段（Other）→ 文本；沿用主插件与 1.x 的既有文案"""
    origin = getattr(seg, "origin", None)
    seg_type = getattr(origin, "type", None)
    data = getattr(origin, "data", {}) or {}
    if seg_type == "share":
        parts = [p for p in (data.get("title", ""), data.get("url", ""), data.get("content")) if p]
        return f"[分享链接: {' | '.join(parts)}] " if parts else "[分享链接] "
    if seg_type == "contact":
        kind = "群" if data.get("type") == "group" else "好友"
        return f"[推荐{kind}: {data.get('id', '')}] " if data.get("id") else f"[推荐{kind}] "
    if seg_type == "location":
        parts = [data["title"]] if data.get("title") else []
        if data.get("lat") and data.get("lon"):
            parts.append(f"{data['lat']},{data['lon']}")
        if data.get("content"):
            parts.append(data["content"])
        return f"[位置: {' | '.join(parts)}] " if parts else "[位置] "
    if seg_type == "music":
        return f"[音乐分享: {data['title']}] " if data.get("title") else f"[音乐分享 {data.get('id', '')}] "
    if seg_type == "dice":
        return f"[骰子 {data['result']}] " if data.get("result") else "[骰子] "
    if seg_type in _SEG_LABELS:
        return f"[{_SEG_LABELS[seg_type]}] "
    if seg_type == "file":
        name = data.get("file_name") or data.get("name")
        return f"[群文件: {name}] " if name else "[群文件] "
    if seg_type:
        return f"[其他消息段: {seg_type}] "
    return f"[{type(seg).__name__}] "


async def _segments(bot: Bot, message) -> list[dict]:
    """消息 → [{"type":"text","text":…} / {"type":"image","url":…,"file":…,"base64":…}]"""
    try:
        unimsg = UniMessage.of(message, bot=bot)
    except Exception as e:
        logger.debug(f"[PeerAgent] 消息通用化失败: {e}")
        text = message if isinstance(message, str) else str(message)
        return [{"type": "text", "text": text}] if text else []

    result: list[dict] = []
    for seg in unimsg:
        if isinstance(seg, Text):
            if seg.text:
                result.append({"type": "text", "text": seg.text})
        elif isinstance(seg, Image) or (isinstance(seg, Other) and _image_data(seg) is not None):
            data = _image_data(seg)
            if data:
                result.append({"type": "image", **data})
        elif isinstance(seg, At):
            result.append({"type": "text", "text": f"@{seg.target} "})
        elif isinstance(seg, Hyper):
            desc = _extract_json_desc(seg.raw or "") if seg.format == "json" else "[收到一条XML消息]"
            result.append({"type": "text", "text": desc})
        elif isinstance(seg, Reference):
            result.append({"type": "text", "text": "[收到一条合并聊天记录]"})
        elif type(seg).__name__ == "Emoji":
            name = (getattr(seg, "name", "") or "").strip()
            data = getattr(getattr(seg, "origin", None), "data", {}) or {}
            name = name or str(data.get("text") or data.get("id") or "")
            result.append({"type": "text", "text": f"[QQ表情 {name}] " if name else "[QQ表情] "})
        elif type(seg).__name__ in ("Voice", "Audio"):
            result.append({"type": "text", "text": "[收到一条语音消息]"})
        elif type(seg).__name__ == "Video":
            result.append({"type": "text", "text": "[收到一条视频消息]"})
        elif type(seg).__name__ == "File":
            name = getattr(seg, "name", "") or ""
            result.append({"type": "text", "text": f"[群文件: {name}] " if name else "[群文件] "})
        elif isinstance(seg, Other):
            result.append({"type": "text", "text": _extra_text(seg)})
    return result


def _message_payload(bot: Bot, data: dict):
    """从 API 参数里取出消息体；取不到返回 None（该调用不是发消息）"""
    message_class = None
    try:
        message_class = bot.adapter.get_message_class()
    except Exception:
        pass
    for key in _PAYLOAD_KEYS:
        value = data.get(key)
        if not value:
            continue
        if message_class is not None and isinstance(value, message_class):
            return value
        if isinstance(value, (str, list, tuple)):
            return value
    return None


# ========== 推送 ==========

async def _push(plugin: str, session: _Session, text: str = "",
                image_url: str = "", image_base64: str = ""):
    """将捕获到的插件输出推送给 Bot A 的 /peer/capture"""
    if not plugin_config.aigfm_peer_push_port or not plugin_config.aigfm_peer_token:
        return
    native = session.native_chat_id
    payload = {
        "bot_name": plugin_config.aigfm_peer_bot_name,
        "plugin": plugin,
        # 0.4.0：会话键（多适配器），Bot A 0.4.0+ 优先用它定位会话
        "session": session.key,
        "adapter": session.adapter,
        # 兼容字段：老版本 Bot A 只认数字 group_id（仅 OneBot）
        "group_id": int(native) if native.isdigit() else 0,
        "text": text,
        "image_url": image_url,
        "image_base64": image_base64,
        "commands": _commands_cache,
    }
    headers = {"Authorization": f"Bearer {plugin_config.aigfm_peer_token}"}
    url = f"http://127.0.0.1:{plugin_config.aigfm_peer_push_port}/peer/capture"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        logger.success(f"[PeerAgent] 推送成功: [{plugin}] → {plugin_config.aigfm_peer_bot_name}")
        logger.info(f"[PeerAgent] 推送内容: plugin={plugin}, session={session.key}, "
                    f"text={text[:100] if text else ''}, "
                    f"image_url={image_url[:100] if image_url else ''}, "
                    f"image_base64={'有' if image_base64 else '无'}")
    except Exception as e:
        logger.exception(f"[PeerAgent] 推送失败: {e}")


# ========== 钩子：捕获本 bot 插件输出 ==========

@Bot.on_calling_api
async def capture_outgoing(bot: Bot, api: str, data: dict):
    """拦截本 bot 插件发出的 API 调用，推送给 Bot A（所有适配器通用）"""
    if not plugin_config.aigfm_peer_push_port:
        return

    try:
        matcher = current_matcher.get()
        source = matcher.plugin_name or "unknown"
    except LookupError:
        return

    # 防死循环：跳过自身
    if source in SELF_PLUGIN_NAMES:
        return

    # 白名单过滤
    if plugin_config.aigfm_peer_capture_plugins and source not in plugin_config.aigfm_peer_capture_plugins:
        return

    # 会话：优先当前事件（响应器上下文里一定有），回落 API 参数
    session = None
    try:
        event = current_event.get()
    except LookupError:
        event = None
    if event is not None:
        session = await _resolve_session(bot, event)
    if session is None:
        session = _session_from_api_data(bot, data)
    if session is None:
        return

    message = _message_payload(bot, data)
    if message is None:
        return

    logger.debug(f"[PeerAgent] 捕获 outgoing: source={source}, session={session.key}, api={api}")
    for seg in await _segments(bot, message):
        if seg["type"] == "text" and seg["text"]:
            await _push(source, session, text=seg["text"])
        elif seg["type"] == "image":
            image_url = seg.get("url", "")
            image_base64 = seg.get("base64", "")
            # 本地文件图片：本机与插件同进程，直接读文件转 base64 推送，否则 Bot A 收不到图
            if not image_url and not image_base64 and seg.get("file"):
                try:
                    async with await anyio.open_file(seg["file"], "rb") as f:
                        image_base64 = base64.b64encode(await f.read()).decode()
                except Exception as e:
                    logger.error(f"[PeerAgent] 本地图片读取失败: {e}")
            await _push(source, session, image_url=image_url, image_base64=image_base64)


# ========== 记住最近一条真实事件（供远程调用复制） ==========

_last_events: dict[str, object] = {}
_MAX_TEMPLATES = 32


@event_preprocessor
async def _remember_event(bot: Bot, event, state):
    """按会话记住最近一条真实消息事件（不发 API 请求：用 alconna Target 推会话键）"""
    if not callable(getattr(event, "get_message", None)):
        return
    try:
        if event.get_type() != "message":
            return
    except Exception:
        pass
    session = _cheap_session(bot, event)
    if session is None:
        return
    _last_events[session.key] = event
    if len(_last_events) > _MAX_TEMPLATES:
        for key in list(_last_events)[:_MAX_TEMPLATES // 2]:
            _last_events.pop(key, None)


def _find_template(session_key: str):
    return _last_events.get(session_key)


# ========== 远程调用：复制真实事件（无模板时回落 OneBot 手搓事件） ==========

def _apply_command_prefix(command: str) -> str:
    """按本 bot 配置的命令前缀（COMMAND_START）补充到命令前"""
    try:
        command_start = get_driver().config.command_start
        if command_start:
            prefix = next(iter(command_start), "")
            if prefix and not command.startswith(prefix):
                command = prefix + command
    except Exception:
        pass
    return command


def _compose_unimsg(command: str, parts: list | None) -> UniMessage:
    """命令 + 参数段（与 Bot A 的回复/调用同结构，条目之间只插一个空格）"""
    unimsg = UniMessage.text(command)
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "at" and part.get("target"):
            unimsg.append(Text(" "))
            unimsg.append(At("user", str(part["target"])))
        elif ptype == "text" and (part.get("content") or "").strip():
            # 与 Bot A 同一规则：文本段首尾空白去掉，避免双空格破坏参数解析
            unimsg.append(Text(" "))
            unimsg.append(Text(str(part["content"]).strip()))
    return unimsg


def _plain_text(unimsg: UniMessage) -> str:
    return "".join(seg.text for seg in unimsg if isinstance(seg, Text))


def _coerce_like(template_value, value):
    if isinstance(template_value, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return template_value
    return str(value)


def _refresh_uniseg_cache(event, bot) -> None:
    """刷新 alconna 按 message_id 缓存的消息

    合成事件复用了原消息的 message_id，而 alconna 会把收到的消息按 message_id 缓存
    （`extension.py` 的 unimsg_cache，默认开启），命中缓存时响应器读到的是**原来那条消息**，
    于是 alconna 写的目标插件永远匹配不到我们投递的命令。
    """
    try:
        from nonebot_plugin_alconna.extension import unimsg_cache, unimsg_origin_cache
    except Exception:
        return
    try:
        msg_id = get_message_id(event, bot)
        unimsg = UniMessage.of(event.get_message(), bot=bot)
    except Exception as e:
        logger.debug(f"[PeerAgent] 刷新 alconna 消息缓存失败: {e}")
        return
    for cache in (unimsg_cache, unimsg_origin_cache):
        try:
            cache[msg_id] = unimsg
        except Exception:
            pass


def _can_hold_message(template, message, expected_text: str) -> bool:
    """事件的 `message` 字段能否直接放本适配器的 Message（实测；Satori 这类字段是结构体，不能放）

    塞进副本后能取到非空 get_message() 就认，不行则改由 `_patch_text_fields` 替换文本字段。
    """
    try:
        probe = template.model_copy(update={"message": message})
        text = str(probe.get_message())
    except Exception as e:
        logger.debug(f"[PeerAgent] message 字段不接受统一消息，改用文本替换: {e}")
        return False
    if not text.strip():
        return False
    return expected_text.strip() in text or str(message) in text


def _plain_incoming(bot, event) -> str:
    """取事件里原来的纯文本（用 alconna 通用层，拿不到再退回适配器自己的实现；Satori 的会返回空串）"""
    try:
        unimsg = UniMessage.of(event.get_message(), bot=bot)
        text = unimsg.extract_plain_text().strip()
        if not text:
            text = "".join(str(getattr(seg, "text", "") or "") for seg in unimsg).strip()
        if text:
            return text
    except Exception as e:
        logger.debug(f"[PeerAgent] 通用层取原文失败: {e}")
    try:
        return event.get_message().extract_plain_text().strip()
    except Exception:
        return ""


def _is_patchable(value) -> bool:
    """能继续往里找文本字段的容器：映射 / 列表 / pydantic 模型"""
    return (isinstance(value, (MutableMapping, list))
            or bool(getattr(type(value), "model_fields", None)))


def _patch_text_fields(event, old_text: str, new_text: str) -> None:
    """把事件里出现的旧消息文本替换成新文本（递归进嵌套模型/字典/列表，先复制再改）

    部分适配器不把消息放在统一的 `message` 字段里（discord 用 content、dodo 用 message_body、
    feishu 在嵌套的 event.event.message.content、**Satori 的 message 是 {id, content} 结构体**），
    只换 `message` 的话目标插件 `get_message()` 读到的仍是原来那条消息，命令匹配不上。
    """
    if not old_text:
        return

    def visit(parent, name, value, depth: int, setter) -> None:
        if isinstance(value, str):
            if old_text in value:
                try:
                    setter(name, value.replace(old_text, new_text))
                except Exception as e:
                    logger.debug(f"[PeerAgent] 替换文本字段 {name} 失败: {e}")
            return
        if not _is_patchable(value):
            return
        if getattr(type(parent), "model_fields", None):
            try:
                copied = deepcopy(value)
                setter(name, copied)
                value = copied
            except Exception:
                pass
        walk(value, depth + 1)

    def walk(obj, depth: int = 0) -> None:
        if depth > 4:
            return
        fields = list(getattr(type(obj), "model_fields", None) or [])
        if fields:
            for name in fields:
                try:
                    value = getattr(obj, name)
                except Exception:
                    continue
                visit(obj, name, value, depth, lambda n, v, o=obj: setattr(o, n, v))
        elif isinstance(obj, MutableMapping):
            for name in list(obj.keys()):
                visit(obj, name, obj.get(name), depth, lambda n, v: obj.__setitem__(n, v))
        elif isinstance(obj, list):
            for item in obj:
                if _is_patchable(item):
                    walk(item, depth + 1)

    walk(event)


def _reset_message_cache(event) -> None:
    """清掉适配器「懒加载的消息缓存」私有属性（如 Discord 的 `_message` / `_original_message`）

    Discord 的 `get_message()` 会把结果缓存在私有属性里，而 `model_copy` 会把这份缓存带过来；
    不清理的话，即使换掉了消息体，目标插件读到的仍是原来那条消息，命令匹配不上。
    """
    for holder in (getattr(event, "__dict__", None), getattr(event, "__pydantic_private__", None)):
        if not isinstance(holder, dict):
            continue
        stale = [k for k in holder
                 if k.startswith("_") and not k.startswith("__") and "message" in k.lower()]
        for key in stale:
            try:
                holder.pop(key, None)
            except Exception as e:
                logger.debug(f"[PeerAgent] 清理消息缓存 {key} 失败: {e}")


async def _copy_event(bot: Bot, template, command: str, user_id=0,
                      parts: list | None = None, sender_name: str = ""):
    """复制该会话最近一条真实事件并换掉消息体（与 Bot A 的 plugin_invoker 同一做法）"""
    command = _apply_command_prefix(command)
    unimsg = _compose_unimsg(command, parts)
    message = await unimsg.export(bot=bot)

    update: dict = {}
    command_text = _plain_text(unimsg)
    if _can_hold_message(template, message, command_text):
        update["message"] = message
        if hasattr(template, "original_message"):
            update["original_message"] = message
    if hasattr(template, "raw_message"):
        update["raw_message"] = command_text
    if hasattr(template, "reply"):
        update["reply"] = None
    if user_id not in (None, "", 0, "0"):
        update["user_id"] = _coerce_like(getattr(template, "user_id", None), user_id)
        sender = getattr(template, "sender", None)
        if sender is not None:
            sender_update = {"user_id": update["user_id"]}
            if (sender_name or "").strip():
                sender_update["nickname"] = sender_name.strip()
            try:
                update["sender"] = sender.model_copy(update=sender_update)
            except Exception as e:
                logger.debug(f"[PeerAgent] 同步发送者信息失败: {e}")
    event = template.model_copy(update={**update, SYNTHETIC_FLAG: True})
    # 消息文本不总在 `message` 字段里（见 _patch_text_fields），补齐后才对得上目标插件
    _patch_text_fields(event, _plain_incoming(bot, template), _plain_text(unimsg))
    _reset_message_cache(event)
    _refresh_uniseg_cache(event, bot)
    logger.debug(f"[PeerAgent] 复制真实事件: command={command}, user={user_id}, parts={parts}")
    return event


def _create_synthetic_event(bot, group_id: int, command: str, user_id: int = 0,
                            parts: list | None = None, sender_name: str = ""):
    """兜底：手搓 OneBot 群事件（仅当该会话没有可复制事件、且本 bot 是 OneBot V11 时）

    注意：这是 1.x 的老做法，字段不如复制真实事件准确（to_me/群名片/真实 message_id 都缺）。
    """
    if bot is None or bot.adapter.get_name() != "OneBot V11":
        return None
    try:
        from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
    except Exception:
        return None
    command = _apply_command_prefix(command)
    message = Message(MessageSegment.text(command))
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "at" and part.get("target"):
            message.append(MessageSegment.text(" "))
            message.append(MessageSegment.at(int(part["target"])))
        elif ptype == "text" and (part.get("content") or "").strip():
            message.append(MessageSegment.text(" "))
            message.append(MessageSegment.text(str(part["content"]).strip()))
    now = datetime.now()
    return GroupMessageEvent(
        time=int(now.timestamp()),
        self_id=int(bot.self_id) if bot else 0,
        post_type="message",
        sub_type="normal",
        message_type="group",
        user_id=user_id,
        group_id=group_id,
        message_id=0,
        message=message,
        raw_message=command,
        font=0,
        sender={"user_id": user_id,
                "nickname": (sender_name or "").strip() or "aigf_user",
                "role": "member"},
    )


async def _execute_command(bot, session_value: str, native_chat_id: int, command: str,
                           user_id: int = 0, parts: list | None = None, at_qq: int = 0,
                           sender_name: str = ""):
    """执行插件命令，插件响应由 capture_outgoing 钩子自动推送"""
    logger.debug(f"[PeerAgent] 执行远程命令: command={command}, session={session_value}, "
                 f"user={user_id}, parts={parts}, at={at_qq}, sender={sender_name}")
    # 未升级的 Bot A 只传 at_user_id：退化为单个 at 段（与旧行为一致）
    if not parts and at_qq:
        parts = [{"type": "at", "target": at_qq}]
    template = _find_template(session_value) if session_value else None
    if template is not None:
        event = await _copy_event(bot, template, command, user_id, parts, sender_name)
    else:
        event = _create_synthetic_event(bot, native_chat_id, command, user_id, parts, sender_name)
        if event is None:
            logger.warning(f"[PeerAgent] 会话 {session_value} 没有可复制的事件，且本适配器不支持兜底构造，无法执行")
            return
        _refresh_uniseg_cache(event, bot)
    await handle_event(bot, event)


# ========== HTTP 端点：接收 Bot A 的远程调用 ==========

@get_driver().on_startup
async def _on_startup():
    # 启动时全量扫描（填充核对 map），上报命令按白名单过滤（空白名单=不上报）
    all_cmds = _scan_commands()
    _commands_cache.clear()
    if plugin_config.aigfm_peer_capture_plugins:
        _commands_cache.extend([
            {"name": c["name"], "description": c["description"]}
            for c in all_cmds if c["plugin"] in plugin_config.aigfm_peer_capture_plugins
        ])
    logger.info(f"[PeerAgent] 扫描命令: {len(_commands_cache)} 个（含 alconna 响应器）")
    if not plugin_config.aigfm_peer_token:
        logger.warning("[PeerAgent] 未配置 aigfm_peer_token，invoke 端点将拒绝所有请求")
    try:
        import nonebot
        from fastapi import Request
        from fastapi.responses import JSONResponse
        app = nonebot.get_app()

        @app.post("/peer/invoke")
        async def _peer_invoke(request: Request):
            auth = request.headers.get("Authorization", "")
            token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
            if not plugin_config.aigfm_peer_token or token != plugin_config.aigfm_peer_token:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            try:
                data = await request.json()
            except Exception:
                return JSONResponse({"error": "bad json"}, status_code=400)
            command = data.get("command", "")
            session_value = data.get("session", "")
            group_id = data.get("group_id") or 0
            user_id = data.get("user_id", 0)
            at_user_id = data.get("at_user_id", 0) or 0
            parts = data.get("parts") or []
            sender_name = data.get("name", "")
            if not command:
                return JSONResponse({"error": "missing command"}, status_code=400)
            if not session_value and not group_id:
                return JSONResponse({"error": "missing session or group_id"}, status_code=400)
            # 老版本 Bot A 只传 group_id（仅 OneBot）：按 onebot11 会话键映射
            if not session_value:
                session_value = f"onebot11:{group_id}"
            logger.info(f"[PeerAgent] 收到调用: command={command}, session={session_value}, "
                        f"user_id={user_id}, parts={parts}, at={at_user_id}, sender={sender_name}")
            try:
                native_chat_id = int(group_id or 0)
            except (TypeError, ValueError):
                native_chat_id = 0
            try:
                at_uid = int(at_user_id)
            except (TypeError, ValueError):
                at_uid = 0
            # 白名单核对：非空时，命令归属插件必须在白名单内，否则拒绝执行
            if plugin_config.aigfm_peer_capture_plugins:
                main = _command_head(command)
                plugin = _command_plugin_map.get(main)
                if plugin not in plugin_config.aigfm_peer_capture_plugins:
                    logger.info(f"[PeerAgent] 拒绝: command={command}（插件 {plugin} 不在白名单）")
                    return JSONResponse({"error": "plugin not allowed"}, status_code=403)
            try:
                bot = get_bot()
            except ValueError:
                return JSONResponse({"error": "no bot"}, status_code=500)
            # 后台执行插件命令，立即返回（响应由 capture_outgoing 钩子异步推回 Bot A）
            try:
                task = asyncio.create_task(_execute_command(
                    bot, session_value, native_chat_id, command, int(user_id or 0),
                    parts=parts, at_qq=at_uid, sender_name=sender_name))
                _invoke_tasks.add(task)
                task.add_done_callback(_invoke_tasks.discard)
            except Exception as e:
                logger.error(f"[PeerAgent] 命令执行失败: {e}")
                return JSONResponse({"error": "execute failed"}, status_code=500)
            return JSONResponse({"ok": True})

        logger.success(f"[PeerAgent] 启动完成 | 推送: {bool(plugin_config.aigfm_peer_push_port)} | bot 名: {plugin_config.aigfm_peer_bot_name}")
    except Exception as e:
        logger.error(f"[PeerAgent] 注册 invoke 端点失败: {e}")