"""nonebot-plugin-aigfm-peer — 跨 bot 通信代理（安装在其它 bot 上）

职责：
1. 捕获本 bot 所有插件输出（文本/图片），通过 HTTP 推送给 Bot A（aigf_manager）
2. 提供 POST /peer/invoke 端点，接收 Bot A 的远程命令调用，用 synthetic event 执行本地插件
"""

from datetime import datetime

import asyncio
import httpx
from nonebot import Bot, get_bot, get_driver, logger
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.internal.matcher import current_matcher
from nonebot.matcher import matchers
from nonebot.message import handle_event
from nonebot.plugin import PluginMetadata
from nonebot.rule import CommandRule

from .config import PluginConfig, plugin_config

__plugin_name__ = "nonebot-plugin-aigfm-peer"

__plugin_meta__ = PluginMetadata(
    name="aigfm-peer", description="跨 bot 通信代理插件：捕获本 bot 插件输出推送给主 bot，并支持远程插件调用",
    usage="安装到其它 bot 后自动工作；配置 AIGFM_PEER_PUSH_PORT / AIGFM_PEER_TOKEN / AIGFM_PEER_BOT_NAME",
    type="application",
    config=PluginConfig, supported_adapters={"~onebot.v11"},
    homepage="https://github.com/Funny1Potato/nonebot-plugin-aigfm-peer",
    extra={"author": "Funny1Potato"},
)

_invoke_tasks: set = set()
_commands_cache: list[dict] = []
_command_plugin_map: dict[str, str] = {}


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


def _scan_commands() -> list[dict]:
    """全量扫描本 bot 的 on_command 命令：填充 _command_plugin_map（核对用），返回 [{name, plugin, description}]"""
    _command_plugin_map.clear()
    commands: list[dict] = []
    seen = set()
    for priority, matcher_list in matchers.items():
        for matcher_cls in matcher_list:
            try:
                rule = matcher_cls.rule
                if not rule or not rule.checkers:
                    continue
                for checker in rule.checkers:
                    if isinstance(checker.call, CommandRule):
                        cmd_rule = checker.call
                        if not cmd_rule.cmds:
                            continue
                        main_cmd = cmd_rule.cmds[0]
                        name = main_cmd[0] if main_cmd else ""
                        if not name or name in seen:
                            continue
                        seen.add(name)
                        plugin_name = matcher_cls.plugin_name or "unknown"
                        description = ""
                        if matcher_cls.plugin and hasattr(matcher_cls.plugin, "metadata"):
                            meta = matcher_cls.plugin.metadata
                            if meta:
                                description = meta.description or ""
                        commands.append({"name": name, "plugin": plugin_name, "description": description})
                        # 全量映射：主名 + 别名 → plugin，供调用核对
                        _command_plugin_map[name] = plugin_name
                        for cmd_tuple in cmd_rule.cmds[1:]:
                            if cmd_tuple and cmd_tuple[0]:
                                _command_plugin_map[cmd_tuple[0]] = plugin_name
                        break
            except Exception:
                continue
    return commands


# ========== 消息解析（从 aigf_manager api_hooks 抽取，不包含 VLM 描述） ==========

def _parse_segments(message) -> list[dict]:
    """将 OneBot v11 消息统一解析为段落列表（文本 + 图片 url/base64）"""
    if isinstance(message, str):
        try:
            msg = Message(message)
            return _parse_segments(msg)
        except Exception:
            return [{"type": "text", "text": message}]

    if isinstance(message, Message):
        result = []
        try:
            for seg in message:
                seg_type = getattr(seg, "type", None)
                seg_data = getattr(seg, "data", {})
                if seg_type == "text":
                    text = seg_data.get("text", "") if isinstance(seg_data, dict) else ""
                    if text:
                        result.append({"type": "text", "text": text})
                elif seg_type == "image":
                    data = seg_data if isinstance(seg_data, dict) else {}
                    result.append(_extract_image_data(data))
        except Exception as e:
            logger.error(f"[PeerAgent] Message 迭代失败: {e}")
        return result

    if isinstance(message, list):
        result = []
        for seg in message:
            if isinstance(seg, dict):
                if seg.get("type") == "text":
                    result.append({"type": "text", "text": seg.get("data", {}).get("text", "")})
                elif seg.get("type") == "image":
                    result.append(_extract_image_data(seg.get("data", {})))
        return result

    return []


def _extract_image_data(data: dict) -> dict:
    """从图片段落的 data 中提取图片数据，处理 base64:// 前缀"""
    file_value = data.get("file", "")
    url = data.get("url", "")
    b64 = data.get("base64", "")

    if file_value.startswith("base64://"):
        b64 = file_value[9:]
        file_value = ""

    return {"type": "image", "url": url, "file": file_value, "base64": b64}


# ========== 推送 ==========

async def _push(plugin: str, group_id: int, text: str = "", image_url: str = "", image_base64: str = ""):
    """将捕获到的插件输出推送给 Bot A 的 /peer/capture"""
    if not plugin_config.aigfm_peer_push_port or not plugin_config.aigfm_peer_token:
        return
    payload = {
        "bot_name": plugin_config.aigfm_peer_bot_name,
        "plugin": plugin,
        "group_id": group_id,
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
        logger.info(f"[PeerAgent] 推送内容: plugin={plugin}, group={group_id}, "
                    f"text={text[:100] if text else ''}, "
                    f"image_url={image_url[:100] if image_url else ''}, "
                    f"image_base64={'有' if image_base64 else '无'}")
    except Exception as e:
        logger.exception(f"[PeerAgent] 推送失败: {e}")


# ========== 钩子：捕获本 bot 插件输出 ==========

@Bot.on_calling_api
async def capture_outgoing(bot: Bot, api: str, data: dict):
    """拦截本 bot 插件发出的 API 调用，推送给 Bot A"""
    if api not in ("send_msg", "send_group_msg", "send_private_msg"):
        return
    if not plugin_config.aigfm_peer_push_port:
        return

    group_id = data.get("group_id")
    if not group_id:
        return

    try:
        matcher = current_matcher.get()
        source = matcher.plugin_name or "unknown"
    except LookupError:
        return

    # 防死循环：跳过自身
    if source in ("nonebot_plugin_aigfm_peer", "nonebot-plugin-aigfm-peer"):
        return

    # 白名单过滤
    if plugin_config.aigfm_peer_capture_plugins and source not in plugin_config.aigfm_peer_capture_plugins:
        return

    message = data.get("message", "")
    segments = _parse_segments(message)
    for seg in segments:
        if seg["type"] == "text" and seg["text"]:
            await _push(source, group_id, text=seg["text"])
        elif seg["type"] == "image":
            await _push(source, group_id, image_url=seg.get("url", ""), image_base64=seg.get("base64", ""))


# ========== synthetic event 执行（从 aigf_manager plugin_invoker 抽取） ==========

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


def _create_synthetic_event(bot, group_id: int, command: str, user_id: int = 0) -> GroupMessageEvent:
    """创建模拟的群消息事件"""
    # LLM 传无前缀命令，这里按本 bot 配置的命令前缀补充
    command = _apply_command_prefix(command)

    message = Message(MessageSegment.text(command))
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
        sender={"user_id": user_id, "nickname": "aigf_user", "role": "member"},
    )


async def _execute_command(bot, group_id: int, command: str, user_id: int = 0):
    """执行插件命令，插件响应由 capture_outgoing 钩子自动推送"""
    logger.debug(f"[PeerAgent] 执行远程命令: command={command}, group={group_id}, user={user_id}")
    event = _create_synthetic_event(bot, group_id, command, user_id)
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
    logger.info(f"[PeerAgent] 扫描命令: {len(_commands_cache)} 个")
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
            group_id = data.get("group_id")
            user_id = data.get("user_id", 0)
            if not command or not group_id:
                return JSONResponse({"error": "missing command or group_id"}, status_code=400)
            logger.info(f"[PeerAgent] 收到调用: command={command}, group={group_id}, user_id={user_id}")
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
                task = asyncio.create_task(_execute_command(bot, int(group_id), command, int(user_id or 0)))
                _invoke_tasks.add(task)
                task.add_done_callback(_invoke_tasks.discard)
            except Exception as e:
                logger.error(f"[PeerAgent] 命令执行失败: {e}")
                return JSONResponse({"error": "execute failed"}, status_code=500)
            return JSONResponse({"ok": True})

        logger.success(f"[PeerAgent] 启动完成 | 推送: {bool(plugin_config.aigfm_peer_push_port)} | bot 名: {plugin_config.aigfm_peer_bot_name}")
    except Exception as e:
        logger.error(f"[PeerAgent] 注册 invoke 端点失败: {e}")
