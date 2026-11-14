# 源码、资产与验证

## 来源

本项目在 2026-09-19 从运行中的公网入口/管理机与算力机导出应用源码，
并补充可移植部署工具。导出不修改原服务器服务配置、不重启业务进程。

旧 ZIP 中有 `room_service.py`，但它是进程监管器。真正房间处理在运行时 `server.py`，
规则执行与策略在 `worker.py` 及原生环境模块中。仅复制 TCP 转接程序不能开出模型对局。

旧快照刻意排除的 `outbound/` 与协议 vendor 现在已纳入仓库。
`room/README*.md`、部分运行时目录名及历史评测工具来自旧部署，可能包含旧路径与历史验收记录；
当前部署入口以根 README、`tools/deploy.py` 与本目录文档为准，历史记录不代表本次重新实测结果。

## Git 与 Release 的分工

- Git：两端应用源码、前端、协议适配、模型模块、环境绑定源码、部署工具与测试。
- Git：赛事服务、原生 PvP 网关、固定卡组、Evolution 补丁和独立启动器。
- Release：Linux CPython 3.11 运行资产，包括 `.so`、卡库、Lua 效果、冻结语义数据、卡组与外服池。
- Release `reproducible-20261114`：固定版本赛事引擎与精确资源快照，安装时按 SHA-256 校验。
- 本地私密备份：旧运行实例缺失文件补充包和虚拟环境快照，可能包含模型权重，不上传到代码仓库。
- 不导出：SSH 凭据、控制密钥、邀请令牌、面板账号/TOTP、生产配置、对局日志和历史录像。

虚拟环境快照含原机器 Python 基础环境路径，**不是可直接跨机器搬运的安装器**。
新部署按依赖清单重新创建 venv；不要把快照当成可执行的 Windows 软件。

## 原生源码

`room/recording-fix-20260915/build-project/ygoenv/` 保存相应环境绑定及录像写入改动。
编译还依赖第三方 `ygopro-core`、Lua、pybind11、fmt、glog、gflags、
SQLiteCpp、SQLite 和 unordered_dense 等库。它们不是本项目自研代码。

本次部署路径使用与 `native-build.json` 哈希对应的预编译模块，
不是宣称只用这一份仓库就能无条件重建所有第三方依赖。
自编译后必须重新核对原生/网络观察契约、模型预热与实际对局；
不要仅修改 `native-build.json` 的哈希绕开验证。

## 自动测试

Python 3.11 的 Linux 环境中：

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
python -m unittest discover -s pvp -p 'test_*.py' -v
python -m unittest discover -s panel/ygo-tcp-bridge/app -p 'test_*.py' -v
PYTHONPATH=room:room/admin:room/app-305m-admin-20260915 \
  python -m unittest outbound.test_outbound outbound.test_pool_refresh outbound.test_multiroom -v
```

测试不会登录真实外服或改线上服务。CI 只验证源码和协议/配置测试；
它没有模型权重和原生发布资产，不能替代 `preflight --warmup` 或实战验收。

2026-09-19 发布曾通过 80 项本地源码测试，并在算力机隔离目录中完成真实 821M-step
`decision-v1` 模型的登记、资源校验与 CPU 策略预热。生产策略 PID 和权重均未改变。
没有为此次发布重开实战或连接公共外服；完整范围见
[`verification-20260919.json`](verification-20260919.json)。

## 第三方材料

模型侧代码保留 `room/app-305m-admin-20260915/project/LICENSE` 中的原始许可说明。
网络协议 vendor 保留 `room/outbound/vendor/README.md` 与源文件哈希记录。
前端第三方库及卡片数据库、脚本的原有权利信息不因打包而改变。

2026-11-14 按所有者要求公开仓库，两个应用共用源码仓库，但可以独立部署。
第三方来源和运行资产范围见 [THIRD_PARTY.md](../THIRD_PARTY.md)；原始许可证保留。
本次复现验证记录见 [verification-20261114.json](verification-20261114.json)。
