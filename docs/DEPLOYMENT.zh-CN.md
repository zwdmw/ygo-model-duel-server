# 双机部署

以下是新机器部署流程，不要直接覆盖运行中的旧实例。部署工具只生成文件，不会自动开启端口、
安装 systemd 单元或重启服务。代码中的运行路径由仓库位置生成，不再要求原作者的目录结构。

## 1. 准备两台主机

- **公网机**：阿里云 ECS 或其他有真实公网 TCP 入站的 Linux 主机，提供游戏端口与管理面板。
- **算力机**：Linux x86-64，安装 CPython 3.11 和 venv；模型默认在 CPU 上推理，不要求 GPU。
- 准备公网机面板域名，以及算力机网关的可信 HTTPS 地址。租赁平台的 HTTPS 映射可映射到算力机 `6006`。
- 算力机应至少留出 4 个 CPU 核与足够内存。现有监管器的整个房间进程树上限是 4 GiB；
  更大模型需先重新评估内存与并发，不能仅按训练步数估算。
- SSH 凭据只保存在部署机器的 SSH 配置/密钥代理中。

在两台机器上分别以部署用户执行，以下假设仓库位于 `/srv/ygo-model-duel-server`：

```bash
sudo install -d -o "$USER" -g "$(id -gn)" /srv/ygo-model-duel-server
git clone https://github.com/zwdmw/ygo-model-duel-server.git /srv/ygo-model-duel-server
cd /srv/ygo-model-duel-server
```

仓库公开可读，不需要原作者账号。部署时可以 `git checkout reproducible-20261114` 固定本次源码。

## 2. 算力机：依赖与原生资产

```bash
cd /srv/ygo-model-duel-server
python3.11 -m venv room/venv
room/venv/bin/python -m pip install \
  -r room/requirements-model.txt -r room/requirements-gateway.txt
```

从 Release 取配套资产，推荐使用固定 SHA-256 的安装工具：

```bash
room/venv/bin/python tools/fetch_runtime.py model
room/venv/bin/python tools/deploy.py prepare-runtime
```

也可以手动下载、检查 SHA-256 并解压：

```bash
mkdir -p /tmp/ygo-runtime-assets
gh release download runtime-20260919 \
  --repo zwdmw/ygo-model-duel-server \
  --pattern 'ygo-runtime-assets-linux-x86_64-cp311-20260919.tar.gz' \
  --pattern 'SHA256SUMS' --dir /tmp/ygo-runtime-assets
cd /tmp/ygo-runtime-assets
sha256sum -c SHA256SUMS
cd /srv/ygo-model-duel-server
tar -xzf /tmp/ygo-runtime-assets/ygo-runtime-assets-linux-x86_64-cp311-20260919.tar.gz \
  --strip-components=1 -C .
room/venv/bin/python tools/deploy.py prepare-runtime
```

资产包不是跨平台引擎：它要求 Linux x86-64、CPython 3.11 和兼容的系统 C/C++ 运行库。
可用 `ldd room/app-305m-admin-20260915/project/ygoenv/ygopro/*.so` 检查缺失动态库。
本仓库没有将 `.so` 提交到 Git；只有源码而不下载资产时不能启动原生对局。

`prepare-runtime` 校验原生模块哈希、外服编码契约、基础卡库、语义数据、默认卡组与 Lua 脚本，
生成当前资源清单，并保留 worker 对模型本身的独立哈希校验。
不要用旧版 `install_runtime.py` 覆盖这里的运行时。

## 3. 算力机：登记自己的模型

先看 [模型契约](MODELS.zh-CN.md)。下面的路径必须指向真实训练产物，不是任意创建的样例：

```bash
room/venv/bin/python tools/deploy.py register-model \
  --checkpoint /path/to/training/checkpoints/step640679936.flax_model \
  --sidecar /path/to/training/checkpoints/step640679936.flax_model.json \
  --training-project /path/to/training/project \
  --steps 640679936 --label 640M --activate
room/venv/bin/python tools/deploy.py preflight --warmup
```

- 权重复制到按 SHA-256 命名的 `model/catalog/`，架构 sidecar 一起登记。
- 训练卡库、卡号索引、冻结语义数据必须与部署资源完全匹配；原生引擎另有发布契约校验。
- `--activate` 只允许首次离线初始化。已有活动模型或 `service.pid` 时会拒绝，线上切换走管理面板。
- 额外模型重复运行登记命令，不加 `--activate`。登记列表不伪造胜率或 Top 10 排名。
- `preflight --warmup` 真正加载权重并执行一次策略预热，但不连接任何外服，不等于完整对局验收。

## 4. 算力机：创建网关与管理配置

将示例 HTTPS 地址替换成实际映射到本机 `6006` 的根地址：

```bash
room/venv/bin/python tools/deploy.py init-compute \
  --gateway https://compute.example.com
```

生成：

- `room/admin/control.key`：新的管理密钥。
- `room/admin/agent.json`：当前安装路径、模型目录及本地服务配置。
- `room/invitations.json`：桥接邀请的哈希。
- `room/private/panel-bootstrap.json`：公网机配置所需的私密配对文件。

工具拒绝覆盖已有凭据。用 SSH/SCP 将 `panel-bootstrap.json` 传到公网机的私密目录，
文件权限设为 `600`；不要提交、截图或发送到公共聊天中。

网关本身监听 HTTP `6006`，由租赁平台或 Nginx 在外层提供 HTTPS/WSS。
自管 TLS 可使用 [`deploy/nginx-compute.conf.example`](../deploy/nginx-compute.conf.example)，
替换域名和证书路径后由 Nginx 管理员检查并加载。不得把裸 HTTP 网关暴露在不可信网络中。

## 5. 算力机：进程托管

有 systemd 的主机：

```bash
room/venv/bin/python tools/deploy.py write-units --role compute --user "$(id -un)"
sudo install -m 644 deploy/generated/ygo-room.service /etc/systemd/system/
sudo install -m 644 deploy/generated/ygo-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ygo-room ygo-agent
curl --fail http://127.0.0.1:7916/health
```

等待健康状态变为 `ready`。初次 JAX 编译可能需要较长时间；日志查看：

```bash
journalctl -u ygo-room -u ygo-agent -n 100 --no-pager
tail -n 60 room/room.log room/data/warmup.log
```

租赁容器没有 systemd 时，在平台进程托管器中分别托管以下两个前台命令，工作目录设为仓库根目录：

```bash
room/venv/bin/python room/room_service.py --app-dir room/app-305m-admin-20260915
room/venv/bin/python room/admin/agent.py --config room/admin/agent.json
```

每个命令是独立进程，不是连续运行的一条 shell 命令。容器重建后需重新启动托管任务。
不要同时启动 systemd 与第二份房间监管器。

## 6. 公网机：面板与游戏入口

```bash
cd /srv/ygo-model-duel-server
python3.11 -m venv panel/venv
panel/venv/bin/python -m pip install -r panel/ygo-admin/app/requirements.txt
panel/venv/bin/python tools/deploy.py init-panel \
  --bootstrap /private/panel-bootstrap.json \
  --origin https://admin.example.com \
  --game-host game.example.com \
  --deck /private/model-deck.ydk \
  --username admin
```

`--deck` 是该房间默认使用的模型卡组，必须使用兼容卡池并满足卡组张数限制。
工具在终端交互读取新的面板密码，不通过命令参数或日志输出密码。
配置和房间口令保存在 `panel/private/`。

配置 [`deploy/nginx-panel.conf.example`](../deploy/nginx-panel.conf.example) 的域名、证书，
将 HTTPS 反向代理到 `127.0.0.1:8090`，保留原始 Host。
`--origin` 必须与浏览器访问的 HTTPS 根地址一致，否则 Host/CSRF 检查会拒绝请求。

```bash
panel/venv/bin/python tools/deploy.py write-units --role panel --user "$(id -un)"
sudo install -m 644 deploy/generated/ygo-bridge.service /etc/systemd/system/
sudo install -m 644 deploy/generated/ygo-panel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ygo-bridge ygo-panel
```

首次登录后启用 TOTP 并离线保存恢复码。

## 7. 端口与客户端

| 主机 | 端口 | 可达范围 |
|---|---|---|
| 公网机 | `443/TCP` | 面板 HTTPS，建议再限制管理来源 |
| 公网机 | `6008/TCP` | 游戏客户端真实原生 TCP 入口 |
| 公网机 | `8090`、`6010` | 仅本机，不在安全组开放 |
| 算力机 | `6006` | 仅 TLS 终结/平台映射层可达 |
| 算力机 | `7915`–`7918` | 仅 `127.0.0.1`，不公开 |

在云安全组及主机防火墙中按此最小范围开放。HTTPS 平台地址不能直接当作原生 TCP 游戏地址。
客户端填写公网机域名、`6008` 和 `panel/private/room-codes.json` 中匹配客户端协议的口令。
两个默认口令预设分别对应 YGOPro2 与 legacy，仍共享一套单局模型房间。

## 8. 模型连接外服

1. 登录面板，确认算力机管理 API 在线并能列出已登记模型。
2. 上传/编辑符合目标服规则的卡组；目标服具有最终卡池与禁限判定权。
3. 保存外服档案：实际主机、原生 TCP 端口、协议、模型、卡组以及必要的房间信息。
4. 只在获得许可的测试房间中启动一次对局，先验证录像、决策日志与终局。
5. 需要停止时使用面板停止操作。只保存档案不会自动连接。

外服进程与自建房间的常驻策略分离。网络侧只能使用可见信息，胜率不能直接与本地原生评测等同。
特殊账号登录、随机卡组、match/tag 等功能取决于目标服的协议与当前适配，不能假定所有外服通用。
先用普通受控单局验收，不用公共匹配充当调试环境。

## 9. 常见问题

- **找不到 `.so` 或 `ygoenv`**：没有安装 Release 资产，或使用了非 CPython 3.11 环境。
- **`Training assets differ`**：训练与部署的卡号顺序/卡库/语义数据不一致，不要跳过检查。
- **模型列表为空**：没有登记模型，或 `agent.json` 的 `model_catalog` 仍指向旧路径。
- **网关 401**：公网机配对文件与算力机邀请/控制密钥不匹配。不要关闭鉴权。
- **面板 403**：检查访问域名、HTTPS、`origin`、反向代理 Host 和浏览器会话。
- **native encoder contract changed**：原生观察与外服编码契约不匹配，不能仅改哈希强行运行。
- **外服未知卡/未知消息失败**：是适配边界，不代表模型权重损坏；保存真实 trace 后再修复和验证。
- **服务因内存退出**：先检查模型结构与 4 GiB 监管上限，不要无限重试影响训练任务。

升级时先检查私服和外服均为空闲，备份 `private/`、`room/admin/` 配置与业务数据，
再更新代码、重新预检并重启对应服务。不要把运行备份包直接覆盖到正在服务的目录。

## 10. 与赛事房间组合

赛事房间可以独立安装，不依赖平台账号或 AI 权重，见
[赛事复现指南](REPRODUCE_TOURNAMENT.zh-CN.md)。平台外服管理可把已登记模型作为选手连接到赛事
`6009`，分别填写每个席位的连接码；赛事 `6009` 与平台人机入口 `6008` 是不同服务。
平台 HTTPS 和赛事 `/tournament/` 可以共用 Nginx；保留各自配置、私钥与数据目录。

源代码测试和平台配置生成不需要模型；真实模型对战必须自行提供权重、架构 sidecar 和匹配训练资源。
`runtime-20260919` 是基础 CPU 运行时，不能直接运行 corrected Top5：
该高级功能要求 `docs/CORRECTED_TOP5.md` 所列额外引擎与评测产物，本仓库不包含原作者的评测目录。
