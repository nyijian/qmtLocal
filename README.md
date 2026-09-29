# qmtLocal

在本机围绕**国金证券大QMT**（迅投QMT，装在 `D:\国金证券QMT交易端`）做量化开发的工作台。

大QMT自带的 Python 是 3.6，策略只能在它的「策略编辑器」里运行；国金又停了 miniQMT，
官方 `xtquant` 在外部取不到数据。这个仓库解决三件事：

| 线 | 做什么 | 用哪个环境 |
|---|---|---|
| **策略开发** | 在 VSCode 里写 QMT 策略，用 `qmt_mock` 本地假运行一遍查语法和逻辑，再贴回 QMT | `.venv`（3.6） |
| **QMT 桥接** | QMT 里常驻一个「桥接服务」策略，把行情、交易转发到本机 TCP 58611；外部脚本用跟官方 `xtquant` 同名同签名的 `qmt_bridge` 调用 | 两个都行 |
| **数据分析** | 大盘快照、离线读 QMT 本地日线缓存、akshare 取财务/龙虎榜/两融 | `.venv313`（3.13） |

## 5 分钟上手

```powershell
# 1. 大盘快照（盘中要先在 QMT 里把「桥接服务」跑起来；盘后加 --offline）
.venv313\Scripts\python.exe tools\market_overview.py
.venv313\Scripts\python.exe tools\market_overview.py --offline

# 2. 本地假运行一个 QMT 策略
.venv\Scripts\python.exe -m qmt_mock.runner strategies\双均线示例.py --code 600000.SH --start 20240101 --end 20240301

# 3. 看持仓和委托（需要桥接服务在运行，账号写在 .env）
.venv\Scripts\python.exe tools\trade_monitor.py
```

环境还没建好的话，先看 [docs/01-环境搭建.md](docs/01-环境搭建.md)。

## 目录

```
strategies/           QMT 策略（GBK），贴进大QMT「策略编辑器」运行，本地用 qmt_mock 假运行
qmt_client_scripts/   在大QMT里常驻的服务脚本（GBK）：桥接服务、提取财务数据
tools/                本地命令行工具：大盘快照、tick 订阅、交易监控、本地日线
qmt_bridge/           桥接客户端库（仿 xtquant 接口）+ 本地日线 .DAT 读取器   → 详见 qmt_bridge/README.md
qmt_mock/             本地模拟 QMT 运行时（假 ContextInfo、假下单）          → 详见 qmt_mock/README.md
tests/                桥接自测/压测、dat 读取自测、dotenv 自测
docs/                 项目文档
real_data/  logs/     本机临时数据、运行日志（都不进 git）
                      共享数据（akshare 等）在 OneDrive：.env 的 QMTLOCAL_DATA_DIR，见 docs/01-环境搭建.md §7
.venv/  .venv313/     Python 3.6 / 3.13 两个虚拟环境（不进 git）
```

## 文档

| 文档 | 什么时候看 |
|---|---|
| [01-环境搭建](docs/01-环境搭建.md) | 换机器、重建环境、第一次挂桥接服务 |
| [02-架构与数据流](docs/02-架构与数据流.md) | 想知道数据从哪来、各部分怎么连起来 |
| [03-脚本清单](docs/03-脚本清单.md) | 某个脚本干什么、用哪个 Python 跑、要不要开桥 |
| [04-开发约定](docs/04-开发约定.md) | **新建或修改文件之前**：编码、3.6 兼容、放哪个目录 |
| [05-踩坑记录](docs/05-踩坑记录.md) | 报错了先来这里搜 |
| [决策记录](docs/决策记录.md) | 为什么这么设计 |
| [路线图](docs/路线图.md) | 接下来要做什么 |
