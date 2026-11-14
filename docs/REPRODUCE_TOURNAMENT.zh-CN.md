# 在新机器复现赛事房间

本指南对应 `reproducible-20261114` Release。原服务器不是依赖项，也不需要原服务器的 SSH、账号或密钥。
赛事源码、PvP 网关源码和引擎补丁均在本仓库；引擎运行资产按 SHA-256 固定。

## 1. 选择运行方式

| 方式 | 能复现的内容 | 环境 |
|---|---|---|
| manual | 网页、联赛/系列赛、手工记分、排名、选手入口和实时状态 | Python 3.11/3.12，Windows/Linux/macOS |
| native | 上述功能及真实联机、引擎结果、续局、重连、原生观战与录像 | Linux x86-64，Python 3.11/3.12，Node 24.14.0，pnpm 11.24.0 |

真人 native 赛事无需模型权重；让 AI 参赛需要另行部署管理平台和兼容模型。

克隆后可检出发布标签，固定本次源码：

```bash
git clone https://github.com/zwdmw/ygo-model-duel-server.git
cd ygo-model-duel-server
git checkout reproducible-20261114
python3 -m venv .venv
.venv/bin/python -m pip install -r tournament/requirements.txt -r pvp/requirements.txt
```

Windows 将 `.venv/bin/python` 换成 `.\.venv\Scripts\python.exe`。

## 2. 先运行手工赛事

```bash
.venv/bin/python tools/run_tournament.py --mode manual
```

访问 <http://127.0.0.1:8787/>；读取
`data/local-tournament/private/service-keys.json` 中自动生成的管理员密钥。
创建双人系列赛或 3 人联赛，选择 `manual-admin`，开始后录入结果即可观察赛程、积分与实时更新。
管理员和选手令牌互相独立。网页在线状态依据请求与 WebSocket 的实际结果更新。

启动器只监听本机 HTTP；不安装系统服务。再次启动同一数据目录会继续使用原数据和本地密钥。
使用 `--data-dir data/another-instance` 创建独立实例；不同实例还须选择不同端口。

## 3. 安装固定原生引擎

在 Debian/Ubuntu 系统安装构建依赖：

```bash
sudo apt-get update
sudo apt-get install -y python3-venv build-essential pkg-config liblzma-dev curl xz-utils
```

安装 Node 官方 Linux x64 24.14.0，并把 `bin` 加到 PATH；例如将其解压到 `$HOME/.local/node-v24.14.0-linux-x64`。
使用 Node 官方同版本的 `SHASUMS256.txt` 校验下载文件。安装后验证：

```bash
node --version
corepack --version
.venv/bin/python tools/fetch_runtime.py tournament
bash tools/setup_tournament_engine.sh
```

`node --version` 必须是 `v24.14.0`。安装脚本根据包内 `package.json` 选择 pnpm **11.24.0**，
用 `--frozen-lockfile` 安装依赖和编译 SQLite/LZMA 等绑定。
如网络需要镜像，可以在本机配置 npm/pnpm registry；不要改动依赖版本与锁文件。

`fetch_runtime.py` 验证压缩包 SHA-256、拒绝目录穿越和链接，安装到 `runtime/evolution/`。
已有不同内容时会拒绝覆盖，升级请使用新的安装目录。离线安装可以先在能联网的机器下载 Release 资产，再传入：

```bash
.venv/bin/python tools/fetch_runtime.py tournament --archive /path/to/ygo-tournament-engine-linux-x86_64-node24-20261114.tar.gz
```

运行资产包含固定编译产物、精确卡库与脚本、pnpm 锁文件；不包含 `node_modules`、生产 `.env`、账号数据库和对局。
上游源码 commit 和子模块 commit 记录在 `deploy/runtime-assets.lock.json`。
重新编译上游后必须应用 `deploy/evolution-patch/` 中的对应修改；直接运行最新版上游不能替代本发布引擎。

## 4. 启动真实联机赛事

```bash
.venv/bin/python tools/run_tournament.py --mode native
```

启动器依次启动引擎、PvP 网关与赛事服务，等待健康检查后显示网页地址。
私钥、provisioning、日志、业务 JSON 和引擎临时数据库均放在指定 `--data-dir` 下；安装的运行资产保持独立。

1. 管理员在网页创建 `native-pvp` 双人系列赛，先选择 1 组 BO2。
2. 将两个独立选手入口分别交给双方；双方进入网页，点击准备。
3. 两个 MDPro3/YGOPro2 客户端使用各自页面返回的服务器、端口、连接码进入。
4. 使用页面给出的实际固定卡组；网关会核对/替换为仓库 `decks/SkyStriker.ydk`。
5. 对局结果由引擎回调记分，检查换位续局、观战和录像。

默认客户端只能从本机连接 `127.0.0.1:6009`。允许受控局域网/公网客户端时：

```bash
.venv/bin/python tools/run_tournament.py --mode native \
  --tcp-host 0.0.0.0 --public-host game.example.com
```

对外只开放所需的游戏 TCP **6009** 和经 HTTPS 代理的网页 **443**。
引擎有辅助监听端口，须由主机防火墙限制为本机；不要在云安全组开放 `7711`、`8790` 或辅助端口。
网页服务始终监听回环地址，通过 `deploy/nginx-tournament.conf.example` 或
`nginx-tournament-location.conf.example` 配置反向代理和 TLS。
`--public-host` 是客户端能够解析且访问的主机名，不是 HTTPS URL。

启动器默认的辅助端口范围是 **17911–17915**。
与现有服务隔离验证可选择：

```bash
.venv/bin/python tools/run_tournament.py --mode native --data-dir data/isolated \
  --port 18787 --pvp-port 18790 --tcp-port 16009 --engine-port 17711 --aux-port-base 18911
```

## 5. 原生验收

在另一个终端运行，以下路径和端口应与本次启动一致：

```bash
export TOURNAMENT_ADMIN_KEY="$(.venv/bin/python -c 'import json; print(json.load(open("data/local-tournament/private/service-keys.json"))["TOURNAMENT_ADMIN_KEY"])')"
export TOURNAMENT_URL=http://127.0.0.1:8787
export PVP_HOST=127.0.0.1
export PVP_PORT=6009
export CARDS_CDB="$PWD/runtime/evolution/resources/current/ygopro/base/cards.cdb"
.venv/bin/python -m tools.native_series_e2e
```

脚本创建一个 BO2 专用测试赛事，在同一个原生连接内完成两局，验证真实引擎终局、座位映射、种子、回调与赛事推进。
测试客户端在引擎开局后投降，以核对接入、终局、续局和记分链路；它不衡量 AI 胜率。
生产验收还应由实际客户端完成正常牌局，检查断线重连和录像播放。
`tools/tournament_native_e2e.py` 保留的是旧版逐局重连验收流程；连续系列赛使用上面的 `native_series_e2e`。

## 6. 长期部署与排错

可以让 systemd 托管完整启动器，设置仓库根目录为工作目录、专用服务用户、绝对数据路径和 Python 路径；
也可参考 `deploy/ygo-tournament.service.example`、`ygo-pvp.service.example` 和 `ygo-evolution.service.example`
分别托管三项服务。示例中的 `/opt` 路径、用户和密钥文件应替换成新安装的配置。
`deploy/aliyun-*.sh` 是旧服务器升级记录，含特定基线哈希，不是新机器安装器。

| 症状 | 排查 |
|---|---|
| 引擎启动即退出 | 查看数据目录 `engine.log`，确认 Node 版本、依赖安装和资源路径 |
| SQLite/LZMA 模块缺失 | 安装编译依赖，重新运行 `setup_tournament_engine.sh` |
| 网页正常但无法联机 | 检查 native 模式、选手准备状态、6009 防火墙与公开地址 |
| 创建接口 401/403 | 使用本次生成的管理员密钥，检查 Nginx 认证与 Host |
| 引擎有终局但未记分 | 查看 `pvp.log`、`tournament.log` 和两服务内部密钥 |
| 仍显示旧版“离线” | 强制刷新，确认加载带版本参数的 `static/app.js` |

停止用 `Ctrl+C`；启动器回收自己启动的进程，不停止同机其他服务。
备份本次数据目录及私钥后再升级，不要把真实数据提交到 Git。
