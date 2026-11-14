# 外服多房间 GPU 推理实施计划

日期：2026-10-04

## 目标

同一个模型可以从同一台算力端同时加入同一外服服务器上的多个独立房间。
各房间拥有独立网络连接、对局状态、循环赛状态、录像和故障边界，同时共享一份
常驻 GPU 的模型权重。

初始并发上限为 4 个外服房间，可通过算力端配置调整。

## 不变量

1. 每个外服房间使用独立 Worker 进程和独立会话目录。
2. 停止、失败或重连一个房间不能终止其他房间。
3. 每个推理连接拥有独立 RNN 状态；开始新一局时必须重置该连接的状态。
4. 并发房间共享同一份模型权重，不允许每个房间重复占用一份 GPU 显存。
5. 同一 GPU 推理服务只服务相同模型和相同外服卡池绑定。
6. 有活动房间时不能热切换该共享推理服务的模型或卡池。
7. GPU 后端不可用时启动失败，不静默回退到 CPU。
8. 历史会话目录、录像和网络跟踪格式保持兼容。

## 运行结构

```text
管理代理
  |
  +-- 外服任务 A Worker -- TCP --> server:port / password A
  +-- 外服任务 B Worker -- TCP --> server:port / password B
  +-- 外服任务 C Worker -- TCP --> server:port / password C
  |
  +-- 共享 GPU Policy Server
        +-- connection A / independent RNN state
        +-- connection B / independent RNN state
        +-- connection C / independent RNN state
```

外服 Worker 负责协议、牌局状态、观测编码、录像与结果落盘。Policy Server 只负责
模型加载和推理，使用私有 Unix Socket 通信。

## 持久化布局

```text
data/outbound/
  jobs/
    <session>.json
  sessions/
    <session>/
      session.json
      status.json
      result.json
      network.jsonl
      worker.log
  policy.json
  policy.sock
  policy.log
```

`current.json` 在迁移期仅保留为旧版页面兼容指针，不再作为任务所有权的唯一来源。

## 管理接口

保留现有接口：

```text
GET  /outbound
POST /outbound  {"action": "start", ...}
POST /outbound  {"action": "start_continuous", ...}
POST /outbound  {"action": "stop"}
```

新增：

```text
POST /outbound/<session>/stop
```

`GET /outbound` 新增字段：

```json
{
  "active": [],
  "capacity": {
    "active": 0,
    "limit": 4,
    "available": 4
  },
  "policy": {
    "state": "ready",
    "backend": "gpu",
    "model_id": "..."
  }
}
```

旧字段 `current` 保留，指向最近启动的活动任务；没有活动任务时为 `idle`。

## 调度改造

1. `OutboundManager.process` 改为按 session 索引的进程表。
2. `current.json` 改为 `jobs/<session>.json`，每个任务单独持久化 PID 指纹。
3. `ensure_idle()` 改为 `ensure_capacity()`。
4. `stop()` 支持指定 session；未指定时执行兼容性的全部停止。
5. 连续决斗调度器占用一个普通并发槽位，不再阻塞其他手动房间。
6. 管理代理重启后通过 PID 指纹恢复对仍存活任务的观察和停止能力。

## GPU 推理服务

1. 第一个房间启动时按模型 ID 和卡池哈希启动 Policy Server。
2. Policy Server 必须报告 `jax.default_backend() == "gpu"`。
3. 每个 Worker 建立独立 Unix Socket 连接。
4. 每个连接保存两份按席位区分的 RNN 状态。
5. `MSG_START` 时 Worker 发送 reset 控制帧。
6. GPU 调用先采用带锁的公平串行队列，避免多线程同时修改设备状态。
7. 后续性能数据证明有收益时，再增加 2-4 请求微批处理；微批处理不是首版前置条件。

## 并发与资源限制

首版默认：

```json
{
  "max_concurrent_outbound_rooms": 6,
  "outbound_policy_python": "/root/autodl-tmp/ygo-agent-main-gpu-20260824/env/bin/python",
  "outbound_gpu_device": "0",
  "outbound_gpu_memory_fraction": 0.4
}
```

启动任务前必须同时满足：

- 活动任务数小于并发上限；
- 共享 Policy Server 的模型和卡池与新任务一致；
- GPU Policy Server 健康；
- 外服目标地址通过现有公网地址校验。

## 故障隔离

- 房间连接失败：只终止该 Worker。
- 房间密码错误或席位已满：只记录该房间错误。
- 单连接推理请求异常：关闭该推理连接，不终止 Policy Server。
- Policy Server 退出：所有依赖它的活动房间明确失败，管理页显示共享故障。
- 停止某个 session：只向对应 PID 发送信号。

## 验收标准

1. 一次输入多个房间密码后，`GET /outbound` 返回对应的活动 session，最多六个。
2. 两个 Worker PID 不同，Policy Server PID 相同。
3. Policy Server 报告 GPU 后端，且只加载一份目标模型。
4. 两个连接的 RNN 状态互不影响；重置 A 不改变 B。
5. 停止 A 后，B 继续运行并可继续产生决策。
6. A 发生加入失败时，B 状态不变。
7. 管理代理重启后仍能列出并停止存活的 A/B Worker。
8. 同一外服地址、同一端口、不同房间密码可以同时连接。
9. Match 第二局结束后的牌组同步包不会使其他房间或 Policy Server 退出。
10. 原有单房间、录像下载、网络跟踪和连续决斗测试继续通过。

## 发布步骤

1. 在本地测试副本完成单元测试和双连接假 Policy Server 测试。
2. 在算力端部署代码但保持并发上限为 1，验证单房间回归。
3. 将并发上限调为 2，连接两个测试房间。
4. 验证按 session 停止和故障隔离。
5. 将并发上限调为 4，记录 GPU 显存、推理延迟和队列等待时间。
6. 稳定后再更新管理页面，使活动房间逐行显示并提供独立停止按钮。
