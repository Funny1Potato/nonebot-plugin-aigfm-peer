"""插件配置定义"""

from nonebot import get_plugin_config
from pydantic import BaseModel, Field


class PluginConfig(BaseModel):
    # 推送目标端口（Bot A 的 HTTP 端口）
    aigfm_peer_push_port: int = Field(0, description="Bot A 的 HTTP 端口，如 14514")
    # 共享 token（Bot A 的 aigfm_peer_bots 里对应项必须一致）
    aigfm_peer_token: str = Field("", description="共享密钥，用于推送和 invoke 认证")
    # 本 bot 名称（推送给 Bot A 时作为来源标识）
    aigfm_peer_bot_name: str = Field("", description="本 bot 的名称，与 Bot A 的 aigfm_peer_bots 配置里的 name 对应")
    # 捕获白名单
    aigfm_peer_capture_plugins: list[str] = Field(default_factory=list, description="要捕获输出的插件名列表，为空则捕获所有")


plugin_config: PluginConfig = get_plugin_config(PluginConfig)
