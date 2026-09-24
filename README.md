# nonebot-plugin-aigfm-peer

跨 bot 通信代理插件，安装到其它 bot 上，配合主插件 [nonebot-plugin-aigf-master](https://github.com/Funny1Potato/nonebot-plugin-aigf-master) 使用。

> [!WARNING]
> **这是 beta 分支（0.4.0 开发中）：跨适配器改造版**
> - 消息解析走 [nonebot-plugin-alconna](https://github.com/nonebot/plugin-alconna) 的 uniseg、会话走 [nonebot-plugin-uninfo](https://github.com/nonebot/plugin-uninfo)，非 OneBot 适配器也能捕获输出
> - 推送/调用新增 `session`（`适配器:会话id`）字段；**旧的 `group_id` 字段保留**（主插件 2.0.0-beta 起支持 `session`，1.6.x 只认 `group_id`，仅 OneBot）
> - 远程调用改为**复制该会话最近一条真实事件**投递（字段更准确）；仅当没有可复制事件时才回落到老的手搓 OneBot 群事件
> - 新增依赖 `nonebot-plugin-alconna` / `nonebot-plugin-uninfo`，`nonebot2` 提到 `>=2.5.0`
>
> 稳定版请用 main 分支（0.3.x）。

## 📖 介绍

- **捕获输出并推送**：拦截本 bot 所有插件的输出（文本/图片等），通过 HTTP 推送给主 bot（aigf-master），进入其 LLM 上下文（标注 `[bot名]`）
- **远程插件调用**：提供 `/peer/invoke` 端点，接收主 bot 的远程命令调用，用「复制真实事件」的方式执行本 bot 的本地插件
- **命令推送**：启动时扫描本 bot 已注册的命令（`on_command` 与 alconna 响应器都支持，主名与别名各上报一条），随推送告知主 bot，让 LLM 了解可调用命令（受白名单控制）

## 💿 安装

```bash
pip install nonebot-plugin-aigfm-peer
```

在 `pyproject.toml` 中添加：

```toml
[tool.nonebot]
plugins = ["nonebot-plugin-aigfm-peer"]
```

从 0.4 起还会自动装上 `nonebot-plugin-alconna` 与 `nonebot-plugin-uninfo`。

## 配置

在 `.env` 中添加：

```env
# 主插件（Bot A）的 HTTP 端口
AIGFM_PEER_PUSH_PORT=14514
# 共享 token（与主插件 AIGFM_PEER_BOTS 中对应项一致）
AIGFM_PEER_TOKEN="shared_secret"
# 本 bot 名称（与主插件 AIGFM_PEER_BOTS 的 name 对应）
AIGFM_PEER_BOT_NAME="botB"
# 插件白名单：捕获输出 + 命令上报 + 调用核对 共用
# 非空=只捕获/上报这些插件的命令且只允许执行它们；空=捕获所有插件输出但不上报命令、调用不核对
AIGFM_PEER_CAPTURE_PLUGINS=[]
```

## 与主插件配合

1. 在主插件 `.env` 配置 `AIGFM_PEER_BOTS`（含本 bot 的 name/port/token）
2. 两端 token 必须一致
3. 主插件与 peer 插件的前缀（`COMMAND_START`）各自独立配置，远程调用时按各 bot 的前缀自动适配
4. 同一 bot 对同一会话（群/频道/私聊）才能被远程调用：peer 侧要"见过"该会话的一条真实用户消息（用来复制事件）；没见过的会话只能在 OneBot V11 上用老的兜底事件

## 多适配器支持

- **捕获**：`Bot.on_calling_api` 是 NoneBot 基类钩子（所有适配器都会触发），消息体用 uniseg 解析（文本/图片/@/合并转发/小程序/XML，以及 onebot 的分享/位置/音乐/骰子/群文件等未映射段）。会话取自当前事件（uninfo 优先，回落 alconna Target），因此推送里的 `session` 是 `适配器:会话id`
- **远程调用**：按主 bot 传的 `session` 找到本侧记住的真实事件，`model_copy` 换消息体后分发（发送者、适配器字段都来自那条真实消息）。**没有可复制事件时**：OneBot V11 用手搓群事件兜底（老行为），其它适配器则记一条告警后跳过（不会假装执行成功）
- **本项目自身的边界**：两个 bot 之间的 HTTP 通道与 `session` 互通需要主插件 2.0.0-beta+；主插件的群系统通知（戳一戳/禁言等）仍是 OneBot 专属

## 可捕获的消息类型

peer 会拦截本 bot 插件的 `send_msg` / `send_group_msg` / `send_private_msg`（不同适配器的发送接口参数名不同，实际按「参数里带消息体」判断），捕获以下内容推送给主 bot：

| 类型 | 推送方式 |
|------|---------|
| 文本（含 @、QQ 表情、语音/视频/群文件占位文案） | `text` 字段 |
| 图片（`url` 字段，或 `file` 字段为 `http://` / `https://`） | 以 url 形式推送（`image_url`） |
| 图片（`base64://` 前缀或 `base64` 字段） | 直接 base64 推送（`image_base64`） |
| 图片（`file://` 本地路径，如 `MessageSegment.image(Path(...))`） | 本地读取文件转 base64 后推送（`image_base64`） |
| 合并转发 / 小程序卡片 / XML | 渲染成文本占位（`text` 字段） |

> 受 `AIGFM_PEER_CAPTURE_PLUGINS` 白名单过滤；主 bot 侧再按 `AIGFM_CAPTURE_IMAGES` 决定是否对图片做 VLM 描述。

## License

MIT
