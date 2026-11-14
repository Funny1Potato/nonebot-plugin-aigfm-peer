# 离线验证

这些脚本从原本机临时目录归档而来，保留行为断言，仅调整加载路径、隔离目录、退出码与当前 mock 接口。没有启动机器人或连接真实平台。

## 安装与运行

Python >=3.10。在仓库根目录创建自己的虚拟环境，然后执行：

```powershell
python -m pip install -e . -r tests/requirements.txt
python tests/run_offline.py
```

本机可直接使用 `D:/DSH-workspace/aigf-manager/.venv/Scripts/python.exe`（3.10）或 `.venv314/Scripts/python.exe`（3.14）。这些环境可复用，不是仓库必须存在的目录。

- 默认加载当前仓库源码，每个脚本使用独立子进程。
- `--source <包的父目录>` 可加载另一份源码；入口使用绝对路径，工作目录不影响目标选择。
- `--suite v_ma_test.py` 选择单个脚本，可重复指定。默认每脚本超时 180 秒，用 `--timeout` 调整。
- 每次运行将 stdout/stderr 与退出码保存到 `tests/.results/<时间>/`，包括 summary.json；失败、超时或进程异常退出均返回非零。
- 各脚本使用唯一的 `tests/.runs/` 子目录，屏蔽继承的 AIGFM 配置并使用测试凭据；不加载工作区 `.env`，不写真实记忆、缓存或预设。输出留存便于排查，这两个目录均被 Git 忽略。
- 所有 API 业务行为使用 mock；这些是离线行为测试，不代表真实平台、真实模型或当前外部服务可用。
- 不要把全部脚本 import 到同一个 pytest 进程：NoneBot 注册表与事件循环属于进程状态。

## 套件

`v_peer_ma_test.py`：会话、消息解析、命令扫描、捕获推送、复制真实事件调用，含 Console 场景。

选择单套件时使用 `--suite v_peer_ma_test.py`。本仓库另有 `v_event_copy_test.py`，验证 Satori/Kaiheila 事件复制、@ 段、缓存与目标 matcher 命中；没有附加矩阵套件。

本次验证结果：[BASELINE.md](BASELINE.md)。


## b5 后新增检查

`v_distribution_test.py` 验证发布门槛会拒绝 tag/版本不符、缺失源码和混入私有数据。质量工作流还会实际构建 wheel/sdist 并分别在干净环境安装、检查依赖和加载插件。Python 3.10 需要测试依赖中的 tomli；干净安装工具为 `python tools/check_clean_install.py --dist dist`，结果保存在 tests/.results/install-*。

`v_request_context_test.py` 覆盖并发请求、后台输出、跨会话主动发送、异常清理和合成事件保护。3.14 完整适配器依赖见 requirements-matrix.txt。
