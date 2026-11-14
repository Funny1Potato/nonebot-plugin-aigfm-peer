> 最新结果见文末「适配器缺口修复」；早先失败和通过记录按时间保留。

# 离线验证基线：2026-11-12

代码基线：peer beta `c826f55`（0.4.0b1），本轮未修改业务源码。

Windows 下复用运行工作区的 Python 3.10 与 3.14 环境，两者运行 `tests/run_offline.py` 均为 1/1 套件通过；`v_peer_ma_test.py` 为 67 个断言通过、0 失败，进程退出码为 0。
测试覆盖会话、消息解析、命令扫描、捕获推送与事件投递，使用 mock bot/HTTP，不是跨 bot 真实部署联调。

本地 wheel 构建成功，确认不包含 tests/docs；运行副本 2 个 Python 文件与仓库一致（忽略行尾）。未联网验证 GitHub/PyPI，未启动或重启 bot。详细日志位于本机 tests/.results，不纳入版本控制。

依赖版本快照：[Python 3.10](baselines/2026-11-12-env-py310.json)、[Python 3.14](baselines/2026-11-12-env-py314.json)。快照用于复查版本，不替代可安装的锁文件。

## 同日后续：适配器缺口修复

改动仍为本地未发布版本，已同步运行副本，没有真实平台联调。

本体：Python 3.14 下默认套件加适配器矩阵共 13/13 通过（默认 11 个），Python 3.10 默认 11/11 通过。事件复制套件在两种环境追加并通过真实 alconna 目标响应器命中验证，共 3 个 unittest；通知回落套件 3 个 unittest 通过。
peer：默认 2 个套件在两种环境通过（既有 67 个断言；新事件复制套件 3 个 unittest）。

消息/调用矩阵 187 个检查点、0 失败；通知矩阵 17 项中 15 OK、2 N/A、0 失败。测试纠正：强引用单个 Alconna 探针；动态 API 使用配置返回值，保留同步方法；补齐 EFChat 配置和 Kaiheila 频道模型字段。生产修复：两侧复制逻辑更新 Satori eager cache 与 Kaiheila 原生 Message 槽位；本体按精确字段回落 Feishu/Kritor/bilibili Live 通知会话。

矩阵通过只说明已构造场景的离线路径；Discord/Kaiheila 的服务器事件不会自动进入子频道，昵称、权限、平台实际发送与双 bot HTTP 仍需联调。

## 2026-11-13：实际双实例 HTTP 联调

实际启动 peer FastAPI/OneBot 实例，与本体通过 /peer/invoke、/peer/capture 通信；平台由外部 WebSocket 模拟器提供，模型及其它服务为实服务。鉴权、白名单、远端 echo matcher 命中、命令清单与输出回传通过。修复无消息体的查询 API 在捕获钩子中进入会话解析的重入风险，事件复制/捕获套件增至 4 个 unittest。3.10、3.14 各 2/2 套件通过；3.14 验证运行副本，2 模块 wheel 核对通过。

完整记录见本体仓库 [LIVE_SERVICES.md](https://github.com/Funny1Potato/nonebot-plugin-aigf-master/blob/beta/docs/LIVE_SERVICES.md)。未发布，未连接真实聊天平台。


## 2026-11-13：会话灰度、诊断、发布门槛与 peer 请求关联

peer 3.10 仓库源码、3.14 运行副本各 4/4 套件通过，包含并发与后台 request_id、跨会话输出、取消/异常和合成模板保护。
最终源码构建的 wheel/sdist 均通过完整包内容核对，分别在全新 3.10 虚拟环境安装、pip check 和隔离目录插件加载通过。公开脱敏摘要：[2026-11-13-operations.json](baselines/2026-11-13-operations.json)。

GitHub Actions 仅完成工作流实现与本地 YAML 解析，未提交推送执行；没有发布、修改生产配置或重启现有机器人。真实聊天平台与长期灰度仍未验证。中途测试失败和修复日志保留在 .results；最终通过结果见上述摘要。
