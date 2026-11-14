# 赛事房间

`tournament/` 是独立的赛事记分服务。它保存房间、赛程、选手入口、实时积分和观战状态，不修改模型训练任务。

## 赛制

- 支持 K 人循环赛，K 可配置为 3–32 人；系统自动生成 `K*(K-1)/2` 个对位。
- 创建联赛后，所有 `K*(K-1)/2` 个对位房间同时开放；每个房间独立保存选手连接码、观战码、种子、先手和比赛进度。
- 每个选手对位默认进行 10 个 BO2，也可创建为按实际局数模式。
- 每个 BO2 共 2 局，双方各先手一次；因此每个对位常规赛为 20 局。
- BO2 胜者得到 3 分，BO2 平局双方各得 1 分，败者 0 分。
- 循环赛结束后按总积分排序。
- 如果创建时填写 `final_opponent_name`，积分第一名与真人挑战者进行 BO7；可用 `final_completion_policy` 选择一方先赢 4 局立即结束，或不管中途胜负固定打满 7 局。固定七局后按胜场决定冠军；若胜场相同则继续单局加赛。每局异种子，先手按 A/B/A/B 交替。
- 未填写真人挑战者时，保留旧版前两名模型 BO2 决赛兼容行为。
- 加赛一组为 2 局，双方各先手一次；一组内一方双胜即结束，否则增加下一组加赛。

例如，3 人产生 3 个对位，4 人产生 6 个对位，5 人产生 10 个对位，6 人产生 15 个对位。多个赛事房间互相独立，可以同时进行。

双人系列赛提供两种赛制：

- BO2：输入组数（例如 10 组实际打 20 局）；每组交换先后手，可选择两局同种子或每局新种子；可选择平分后加赛。
- 按局数：输入 N 就打 N 局，每局新种子，A/B 逐局轮换先手（奇数 N 的先手次数相差 1）。每局胜 3 分、平 1 分、负 0 分；打满即结束，即使积分相同也不加赛。

管理员可关闭等待中、进行中或暂停的房间。关闭会保留历史比分并中断对应的原生 PvP 会话；房间无法恢复，也不再接受结果。

## 引擎模式

`native-pvp` 使用 MDPro3 / YGOPro2 兼容原生引擎。正常胜负只能来自引擎的 `MSG_WIN` 和签名回调。模型卡死、超时且无法恢复时，管理员可以使用“判罚结算”将指定选手的剩余局或整场判负，也可以在循环赛房间中使用“强制结束并指定积分”，直接填写双方最终总积分。两种操作都会封存当前 PvP 会话、断开选手并禁止继续对局，但保留原观战码和所有已完成真实牌局；未完成的当前局不会写成录像。

`manual-admin` 仅用于没有原生引擎时的测试或手工赛事。该模式才显示管理员的本局胜负和撤销按钮。

判罚记录包含判负选手、范围、原因、受影响局号和时间。若判罚后尚未产生其他比赛结果，管理员可以撤销判罚；原生房间会建立新的 PvP 会话继续未完成赛程。后续比赛已经推进时系统拒绝撤销，避免覆盖新结果。

强制积分结算记录包含双方最终积分、原因、理论积分上限和时间。该积分覆盖该配对的最终联赛积分，但不会改写真实牌局胜负、BO2 统计或录像。最后一个循环赛房间结算后，系统仍按最终积分排名并正常创建冠军决赛。

原生房间允许多个观众加入原生观战连接；公开网页只显示每个配对房间的观战地址、端口和观战码，不显示选手连接码。正常结束或管理员封存后，已完成局会继续通过原观战入口播放；逐局 YRP 会在后台恢复为完整或部分系列录像。若一局都没有完成，页面会明确显示暂无可播放录像。

进行中的原生房间会在双方用户名下显示座位连接状态：

- `在线`：原生 TCP 座位仍连接，并在最近 120 秒内有协议通信。
- `已断线，等待重连`：连接已经断开，但连续系列赛仍为该选手保留原席位。
- `已退出 / 断线`：连接已断开且当前没有有效重连窗口。
- `疑似卡住`：座位仍保持连接，但牌局进行中连续 120 秒没有协议通信。该状态只是告警，不会自动判负或改分。
- `尚未连接`：选手还没有进入该原生房间。

赛事服务每 5 秒从 PvP 网关同步一次状态，并显示最后通信或断线持续时间。管理员应结合客户端画面、模型日志和重连情况判断是否使用“判罚结算”。

## 本地启动

新机器安装优先使用 [完整复现指南](REPRODUCE_TOURNAMENT.zh-CN.md)，包含自动生成私钥、
固定引擎和一条命令启动。以下是单独运行赛事 HTTP 服务的底层入口：

```powershell
cd ygo-model-duel-server
.\.venv\Scripts\python.exe -m pip install -r tournament/requirements.txt
.\.venv\Scripts\python.exe -m tournament.server --data-dir data/tournament --host 127.0.0.1 --port 8787
```

管理员密钥是可选的。未设置 `TOURNAMENT_ADMIN_KEY` 时，管理 API 不要求密钥，适合已经由内网、VPN、Nginx 白名单或访问认证保护的管理页面；设置该环境变量后，管理 API 恢复要求 `X-Tournament-Admin-Key` 请求头。生产环境不要把密钥写入仓库。

网页入口：

```text
http://127.0.0.1:8787/
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health
```

## 选手流程

1. 管理员创建房间，输入 K 名选手；联赛开始后系统一次生成全部两两配对房间；如果需要真人决赛，同时填写真人挑战者名称并选择 BO7 结束方式。
2. 管理员把循环赛选手入口和真人决赛挑战者入口分别发送给对应人员。
3. 选手使用房间编号和令牌进入自己的席位；同一名选手可以同时参与多个配对房间。
4. 对应配对激活后，双方点击“准备本局”；真人决赛沿用同一个原生会话，不退回服务器页面。
5. 原生模式下网页显示该选手在当前配对房间自己的服务器、端口、座位和连接码。
6. 在 MDPro3 / YGOPro2 中连接；引擎结束牌局后自动推进记分和下一局。主动离房或网络断线后，只要连续系列赛尚未结束，就能使用原主机、端口和选手密码返回原席位。

令牌只绑定创建时的选手 ID。管理员重新生成入口后，旧令牌立即失效。

## 核心接口

```text
POST /api/rooms
     X-Tournament-Admin-Key

POST /api/rooms/ROOM_ID/matches/MATCH_ID/adjudicate
     X-Tournament-Admin-Key
     {"penalized_side":"a","scope":"remaining","reason":"模型卡死且重连失败"}

POST /api/rooms/ROOM_ID/matches/MATCH_ID/adjudication/undo
     X-Tournament-Admin-Key

POST /api/rooms/ROOM_ID/matches/MATCH_ID/force-finish
     X-Tournament-Admin-Key
     {"points_a":3,"points_b":0,"reason":"选手弃权"}

     {
       "name":"Top K Challenge",
       "room_type":"league",
       "player_count":5,
       "players":["A","B","C","D","E"],
       "final_opponent_name":"真人选手",
       "final_completion_policy":"play_all_seven",
       "engine":"native-pvp"
     }

POST /api/rooms/{room_id}/join
     {"token":"..."}

POST /api/rooms/{room_id}/players/{player_id}/ready
     X-Tournament-Player-Token
     {"ready":true}

GET  /api/rooms/{room_id}
GET  /ws/{room_id}

POST /api/rooms/{room_id}/close
     X-Tournament-Admin-Key

GET  /api/rooms/{room_id}/native-spectator
     X-Tournament-Admin-Key

POST /api/internal/duel-events
     X-PVP-Signature: HMAC-SHA256(TOURNAMENT_INTERNAL_KEY)
```

公开房间、网页观战和 WebSocket 不返回管理员密钥、选手令牌或原生选手连接码。
公开观战信息可以包含专用观战码及已完成对局的回放入口。

## 阿里云部署建议

赛事 HTTP 服务和 PvP 网关分开运行：

- Nginx/HTTPS：对外提供网页、API 和 WebSocket。
- PvP 原生 TCP：只开放实际给 MDPro3/YGOPro2 客户端连接的端口。
- 赛事服务端口 `8787`、PvP HTTP `8790` 只监听内网或回环地址。
- 如果不设置 `TOURNAMENT_ADMIN_KEY`，必须在 Nginx、VPN、内网或其他网络层限制创建、开始、暂停、恢复、关闭和删除接口；否则任何能访问页面的人都拥有管理员权限。
- PvP 网关内部回调赛事服务，双方使用独立的内部 HMAC 密钥。
- 不要把训练端口、Evolution 内部端口或管理端口直接暴露到公网。

生成 systemd 单元：

```bash
cd /srv/ygo-model-duel-server
python3.11 -m venv tournament/venv
tournament/venv/bin/python -m pip install -r tournament/requirements.txt
tournament/venv/bin/python tools/deploy.py write-units --role tournament --user "$(id -un)"
```

具体端口以当前 `deploy/` 单元和 Nginx 配置为准。修改公网端口后，要同时更新安全组、Nginx、PvP 网关和网页中返回的原生连接地址。

## 原生验收

先使用短流程验证一局真实引擎回调：

```bash
export TOURNAMENT_ADMIN_KEY='从服务环境读取'
export TOURNAMENT_URL=http://127.0.0.1:8787
export PVP_HOST=127.0.0.1
export PVP_PORT=6009
export CARDS_CDB=/path/to/cards.cdb
python3 -m tools.native_series_e2e
```

该脚本会检查座位换位、引擎胜负、回调幂等和赛事状态。正式赛事前应先完成一个完整 BO2，再运行完整循环赛。
通过 SSH 隧道或其他本地转发验收时，使用 `PVP_HOST` 和 `PVP_PORT`
覆盖原生连接地址；脚本不会误连房间状态里返回的公网端口。

## 模型对局

`tools/model_tournament.py` 用隔离数据目录启动临时原生房间，读取算力端发布的 corrected Top5 manifest，固定同一副卡组，分别启动两个 outbound 模型进程，并保存：

- 模型 checkpoint SHA-256、sidecar SHA-256；
- 卡组文件 SHA-256；
- 每局随机种子、先后手、引擎座位映射；
- 原生网络轨迹、决策日志和 YRP 回放；
- 只接受真实引擎终局，不接受脚本伪造胜负。

正在训练的任务不需要停止，也不会被该脚本切换模型或修改默认房间。
