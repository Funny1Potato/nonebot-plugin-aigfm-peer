# nonebot-plugin-aigfm-peer

跨 bot 通信代理插件，安装到其它 bot 上，配合主插件 [nonebot-plugin-aigf-master](https://github.com/Funny1Potato/nonebot-plugin-aigf-master) 使用。

## 📖 介绍

- **捕获输出并推送**：拦截本 bot 所有插件的输出（文本/图片），通过 HTTP 推送给主 bot（aigf-master），进入其 LLM 上下文（标注 `[bot名]`）
- **远程插件调用**：提供 `/peer/invoke` 端点，接收主 bot 的远程命令调用，用 synthetic event 执行本 bot 的本地插件
- **命令推送**：启动时扫描本 bot 已注册的 `on_command` 命令，随推送告知主 bot，让 LLM 了解可调用命令（受白名单控制）

## 💿 安装

```bash
pip install nonebot-plugin-aigfm-peer
```

在 `pyproject.toml` 中添加：

```toml
[tool.nonebot]
plugins = ["nonebot-plugin-aigfm-peer"]
```

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

## License

MIT
