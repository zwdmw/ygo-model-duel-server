# 游戏王对战管理平台与赛事房间

同一仓库提供两个可以分别部署的应用：

| 应用 | 功能 | 起步文档 |
|---|---|---|
| **赛事房间** | 双人连续系列赛、3–32 人循环赛、真人 BO7、原生联机、实时积分、判罚、重连、观战与录像 | [赛事复现指南](docs/REPRODUCE_TOURNAMENT.zh-CN.md) |
| **对战管理平台** | 登录/TOTP、模型登记与切换、卡组管理、自建人机房间、外服连接、多个独立对局和策略进程管理 | [平台双机部署](docs/DEPLOYMENT.zh-CN.md) |

赛事的真人原生对局不需要 AI 模型。管理平台执行 AI 推理需要自行提供兼容权重及训练资源；
仓库不包含原服务器的模型、账号、密钥、历史赛事和业务数据。

## 快速启动赛事网页

Python 3.11 或 3.12；Windows、Linux、macOS 均可运行手工记分模式：

```bash
git clone https://github.com/zwdmw/ygo-model-duel-server.git
cd ygo-model-duel-server
python -m venv .venv
```

Linux / macOS：

```bash
.venv/bin/python -m pip install -r tournament/requirements.txt
.venv/bin/python tools/run_tournament.py --mode manual
```

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m pip install -r tournament/requirements.txt
.\.venv\Scripts\python.exe tools/run_tournament.py --mode manual
```

打开 <http://127.0.0.1:8787/>。管理员密钥自动生成在
`data/local-tournament/private/service-keys.json` 的 `TOURNAMENT_ADMIN_KEY` 字段。
创建房间时选择 **管理员手工记分**。`Ctrl+C` 停止本次启动的进程，数据保留。

此模式可以复现网页、赛程、记分、选手入口和实时更新；实际客户端打牌需要下面的原生引擎。

## 原生赛事：MDPro3 / YGOPro2 联机

使用 Linux x86-64、Python 3.11/3.12、Node.js **24.14.0**（含 corepack），以及 C++ 编译工具和 liblzma：

```bash
sudo apt-get update
sudo apt-get install -y python3-venv build-essential pkg-config liblzma-dev
python3 -m venv .venv
.venv/bin/python -m pip install -r tournament/requirements.txt -r pvp/requirements.txt
.venv/bin/python tools/fetch_runtime.py tournament
bash tools/setup_tournament_engine.sh
.venv/bin/python tools/run_tournament.py --mode native
```

固定版本的引擎、卡库、Lua 脚本和 pnpm 锁文件从 [Release](https://github.com/zwdmw/ygo-model-duel-server/releases/tag/reproducible-20261114)
安装，下载前后按 [资产锁文件](deploy/runtime-assets.lock.json) 验证 SHA-256。
默认只接收本机客户端，游戏端口为 `6009`。局域网/公网访问、TLS、自动启动与验收见
[完整赛事复现指南](docs/REPRODUCE_TOURNAMENT.zh-CN.md)。

## 对战管理平台

```text
MDPro3 / YGOPro2 -- TCP :6008 --> 公网机：管理面板 + TCP bridge
                                       |
                                 HTTPS / WSS 鉴权
                                       |
                                算力机：网关 + 管理 API
                                       |
                       已登记模型 + 原生规则引擎 / 外服客户端
```

按照 [双机部署](docs/DEPLOYMENT.zh-CN.md) 在新目录安装，运行
`tools/deploy.py register-model`、`preflight --warmup`、`init-compute` 和 `init-panel`。
普通推理使用 Linux x86-64、CPython **3.11** 与 CPU JAX；GPU 共享策略是可选高级配置。
模型架构、sidecar、卡号索引与卡库必须匹配，详见 [模型契约](docs/MODELS.zh-CN.md)。

平台的原生运行资产另见 `runtime-20260919` Release，可用
`.venv/bin/python tools/fetch_runtime.py model` 安装。
它不包含权重，也不代表能复现原作者训练出的胜率；corrected Top5 需要额外的匹配引擎与评测产物。

## 代码与验证

| 目录 | 内容 |
|---|---|
| `tournament/` | 赛程、积分、权限、回调、网页与 WebSocket |
| `pvp/` | 原生 TCP 网关、固定卡组、座位、重连、结果与录像 |
| `deploy/evolution-patch/` | 固定版本 Evolution 引擎的本地改动 |
| `panel/` | 对战管理面板与 TCP bridge |
| `room/` | 模型房间、登记目录、管理 API、外服协议与共享策略 |
| `tools/` | 资产校验、启动、部署、模型登记与原生验收工具 |
| `tests/` | 源码与功能回归测试；CI 另运行 `pvp/`、bridge 与 outbound 测试 |

```bash
python -m pip install -r requirements-test.txt
python tools/check_source.py
python -m unittest discover -s tests -v
python -m unittest discover -s pvp -p 'test_*.py' -v
python -m unittest discover -s panel/ygo-tcp-bridge/app -p 'test_*.py' -v
PYTHONPATH=room:room/admin:room/app-305m-admin-20260915 \
  python -m unittest outbound.test_outbound outbound.test_pool_refresh outbound.test_multiroom -v
```

测试不连接公共匹配或改动生产服务。复现范围、第三方来源与发布验证记录见
[源码与验证](docs/SOURCE_AND_TESTS.zh-CN.md)、[第三方说明](THIRD_PARTY.md)。
GitHub Actions 的 `Native Reproduction` 工作流可手动运行，自动下载固定资产并验证一个真实 BO2。
原创应用代码使用 [MIT License](LICENSE)，第三方代码与游戏资源保留各自许可。
