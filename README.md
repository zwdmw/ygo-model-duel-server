# YGO Model Duel Server

把兼容的游戏王模型用于两种场景：

- **自建人机房间**：MDPro3 / YGOPro2 客户端连接公网入口，与算力机上的模型对战。
- **模型连接外服**：从管理面板选择已登记模型、卡组与目标服务器，运行独立的网络对局进程。

本项目包含公网机与算力机配合的应用代码、管理面板、原生环境绑定源码、
模型登记工具、部署模板和测试。它不是任意模型格式的通用推理网关，也不是多人 PVP 房间平台。

```text
MDPro3 / YGOPro2
       |
       | native TCP :6008
       v
Public host (Aliyun ECS or another public host)
  TCP bridge + HTTPS administration panel
       |
       | authenticated HTTPS / WSS
       v
Compute host
  gateway :6006 -> room :7915 / health :7916 / spectator :7917
               -> management agent :7918
                    |
                    +-> registered Flax model + native engine (CPU)
                    +-> isolated outbound client -> external game server
```

## 开始部署

1. 阅读 [双机部署](docs/DEPLOYMENT.zh-CN.md)。
2. 准备 [兼容模型与训练资源](docs/MODELS.zh-CN.md)。
3. 从 `runtime-20260919` Release 下载原生运行资产，或提供你自己的匹配资源。
4. 运行模型登记和启动前检查，再生成配置、部署服务。

**运行资产与模型权重分开。** Git 不包含模型权重、虚拟环境、卡库二进制、生产配置、
密码、邀请令牌或历史对局。Release 的运行资产包包含原生引擎、卡库、Lua 脚本与语义数据，
不包含权重和凭据。私有仓库的访问者需要仓库及 Release 的读取权限。

## 目录

| 目录 | 用途 |
|---|---|
| `panel/ygo-admin/app/` | 公网机管理面板，登录、TOTP、模型/卡组/外服管理 |
| `panel/ygo-tcp-bridge/app/` | 公网原生 TCP 入口、鉴权与桥接控制 |
| `room/room_service.py` | 算力机房间进程监管 |
| `room/app-305m-admin-20260915/` | 当前房间、策略 worker、观战与录像实现 |
| `room/admin/` | 算力机管理 API，评测目录与自有模型两种目录模式 |
| `room/outbound/` | 外服协议、可见信息编码、策略执行与录像 |
| `room/recording-fix-20260915/build-project/` | 与发布原生模块对应的 C++/Python 环境绑定源码 |
| `tools/deploy.py` | 资源校验、模型登记、凭据生成、systemd 单元生成 |
| `deploy/` | HTTPS 反向代理模板、配置样例 |
| `tests/` | 可移植部署与安全回归测试 |

`app-305m-admin-20260915` 是历史部署目录名，不限制只能加载 305M-step 模型。
`room/server.py`、`room/worker.py` 等旧入口保留用于追溯，新的部署统一使用上述运行时目录。

## 验证与限制

- 普通部署使用 Linux x86-64、CPython 3.11、CPU JAX。Windows 只能管理源码和运行非原生测试。
- 一套房间默认只接纳一局人机对战；多个入口密码是预设，不增加并发房间数。
- 外服观察只来自网络可见消息，**不承诺与训练时原生观察完全等价或胜率相同**。
- 不支持的卡、协议和模型结构应明确失败，不切换为启发式策略。
- 只连接你拥有或得到许可的测试房间；不要用自动化干扰公共匹配。
- 详细限制、资源来源与测试方法见 [模型说明](docs/MODELS.zh-CN.md)、
  [源码与验证说明](docs/SOURCE_AND_TESTS.zh-CN.md)。
