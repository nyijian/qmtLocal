# qmt_bridge —— 从大QMT里把行情和交易数据转出来

## 为什么需要它

国金把 miniQMT 停了。官方 `xtquant.xtdata` 现在的实际表现是：

- 大QMT（`XtItClient.exe`）在监听 `127.0.0.1:58600`，`xtdata.connect('127.0.0.1', 58600)`
  **能连上**，`is_connected()` 返回 True；
- 但除了 `get_stock_list_in_sector`（读本地板块文件）之外，`get_full_tick`、
  `subscribe_quote`、`download_history_data`、`get_trading_dates`、`get_market_data_ex`
  全部返回 `ErrorID 200005 未找到订阅数据`，`get_instrument_detail` 返回 `None`；
- `%USERPROFILE%\.xtquant\<guid>\` 下只有 `running_status`，没有 `xtdata.cfg`，
  说明行情服务压根没对外登记。

用QMT自带的那份 xtquant（`bin.x64\Lib\site-packages\xtquant`，`IPythonApiClient` 版本）
打到 58600 结果一样。管道通，管道后面的服务没开。`xtquant.xttrader.XtQuantTrader`
走的是同一条被停掉的路，一样连不上。

行情和交易都只在**大QMT策略运行时**里拿得到（`ContextInfo` 上有 `subscribe_quote` /
`subscribe_whole_quote` / `get_full_tick` / `get_market_data_ex`，全局函数里有
`get_trade_detail_data` / `passorder` / `cancel`），所以这里做一层中转：
QMT里跑一个常驻脚本把数据推出来，本地用跟 `xtdata` / `xttrader` 同名同签名的客户端接。

## 怎么用

**1. 启动QMT侧服务**

这一步有三个坑，都是实机踩出来的，按顺序做：

**(1) 把脚本粘进去。** 文件是 GBK，直接读会乱码，用这条拿到剪贴板（PowerShell 5.1 / 7 通用）：

```powershell
[IO.File]::ReadAllText("D:\QuantStock\qmtLocal\qmt_client_scripts\桥接服务.py", [Text.Encoding]::GetEncoding(936)) | Set-Clipboard
```

QMT「策略编辑器」→ 新建 Python 策略 → 清空 → 粘贴 → 「编译」。

**(2) 右侧「基本信息」里把「启动本地python」取消勾选。** 这一条不做，后面全白搭：
勾上时 QMT 会拉一个外部 Python 进程跑策略，路径没配的话主日志里是

```
[TC::CTradeStrategyData::doRun] execude cmd:  -u "...\桥接服务.py" "...\userdata" 1790132560681
[TC::CTradeStrategyData::doRun] return code:1
```

注意 `cmd:` 后面解释器是空的。进程根本起不来，Python 一行都不执行，
「策略日志」只会有「开始运行 / 结束运行」，连 `[bridge]` 都看不到。

**(3) 去「模型交易」→「新建策略交易」建一条，并保持「运行中」。**
策略类型选「桥接服务」，账号类型「股票账号」，资金账号选你自己的。
「策略日志」里出现这两行才算成功：

```
[bridge] 已监听 127.0.0.1:58611
[bridge] 下单开关 ALLOW_ORDER=False，passorder 参数个数 PASSORDER_ARGS=11
```

本地随时可以验：

```powershell
if (Get-NetTCPConnection -State Listen -LocalPort 58611 -ErrorAction SilentlyContinue) { "桥通了" } else { "桥没起来" }
```

### 跑起来之后别碰的几件事

* **别在「策略编辑器」里点「运行」。** 桥靠 daemon 线程活着，这些线程只在策略真正
  运行时存在。编辑器里点运行（或者在桥跑着时反复保存）会起一个短命实例，它按注册表
  逻辑把正在服务的那个关掉、自己接管端口，然后转眼被 QMT 拆掉线程 ——
  只剩一个**能握手、不回话**的僵尸监听。客户端连上之后 `connect()` 会 ping 一下把这种
  情况认出来并直接说清楚，不会让你对着「调用 xxx 超时」猜。
  真遇上了：回「模型交易」重新启动那一行即可。
* **运行模式「模拟」就够了**，不影响查询 —— 实测 `get_trade_detail_data` 返回的是
  真实账号的资金和持仓（拿 QMT 日志里的 `update fund ... avai` 对过）。
  只有真要下单时才需要「实盘」。
* **QMT 要一直开着，策略别停。**

要下单的话，先把查询跑通再回来改 `ALLOW_ORDER`，见下面「下单之前」那节。

**2. 本地取行情**

```python
from qmt_bridge import xtdata          # 走桥
# from xtquant import xtdata           # miniQMT 恢复后换回这行，其余代码一个字不用改

def on_data(datas):                    # {'000001.SZ': [tick, tick, ...]}
    for code, ticks in datas.items():
        for t in ticks:
            print(code, t['time'], t['lastPrice'])

xtdata.subscribe_quote('000001.SZ', period='tick', callback=on_data)
xtdata.run()
```

现成的例子：`tick_subscribe.py`。

```
.venv\Scripts\python.exe tick_subscribe.py 000001.SZ 600000.SH --snapshot
.venv\Scripts\python.exe tick_subscribe.py SH SZ --mode whole
```

**3. 本地取交易数据**

```python
from qmt_bridge.xttrader import XtQuantTrader, XtQuantTraderCallback
from qmt_bridge.xttype import StockAccount
from qmt_bridge import xtconstant
# from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
# from xtquant.xttype import StockAccount
# from xtquant import xtconstant

trader = XtQuantTrader('', 0)          # path / session_id 收下不用
trader.start()
trader.connect()

# 账号得自己填：`trader.get_default_account()` 读的是 ContextInfo.accountid，
# 实机上这个版本没有这个属性，返回 None。真实账号不建议写进代码——这仓库要传
# GitHub，硬编码进源码提交历史里就删不干净了。设一次环境变量，trade_monitor.py /
# tick_subscribe.py 都会自动去读：
#   PowerShell（当前会话）  $env:QMT_ACCOUNT_ID = '你的账号'
#   PowerShell（永久生效）  [Environment]::SetEnvironmentVariable('QMT_ACCOUNT_ID','你的账号','User')
acc = StockAccount('你的资金账号')
asset = trader.query_stock_asset(acc)
print(asset.total_asset, asset.cash, asset.market_value)

for p in trader.query_stock_positions(acc):
    print(p.stock_code, p.volume, p.can_use_volume, p.open_price)

for o in trader.query_stock_orders(acc, cancelable_only=True):
    print(o.order_id, o.stock_code, o.order_status, o.status_msg)
```

要委托/成交的实时变化：

```python
class Cb(XtQuantTraderCallback):
    def on_stock_order(self, o): print('委托', o.stock_code, o.status_msg)
    def on_stock_trade(self, t): print('成交', t.stock_code, t.traded_volume, t.traded_price)

trader.register_callback(Cb())
trader.subscribe(acc)
trader.run_forever()
```

现成的例子：`trade_monitor.py`（只读，不下单）。

```
.venv\Scripts\python.exe trade_monitor.py            # 打一次快照就退
.venv\Scripts\python.exe trade_monitor.py --watch    # 盯着变化
.venv\Scripts\python.exe trade_monitor.py -a 你的资金账号 -t CREDIT
```

字段名对齐官方（`asset.cash`、`position.can_use_volume`、`order.order_status`、
`trade.traded_volume` …）。QMT 原始的 `m_xxx` 字段一个不落地挂在每个对象的 `.raw` 上 ——
各版本字段名不尽相同，对不上的时候先打 `.raw` 看看实际给了什么。

**4. 改完两边之后跑自测**

```
.venv\Scripts\python.exe -m qmt_bridge.桥接自测
```

用假的 `ContextInfo` 和假柜台把协议端到端走一遍，不需要QMT在跑，**也不碰任何真实账号**。
协议、序列化、订阅号路由、交易字段翻译、下单开关、断线清理这些问题它都能挡掉。
几秒跑完，最后一行 `FAILURES: 0` 才算过。

改了桥的线程模型、队列策略、连接生命周期之后，再跑一遍压测（十几秒）：

```
.venv\Scripts\python.exe -m qmt_bridge.桥接压测
```

它盯的是上了盘才会咬人的那几条路：背压时行情回调线程会不会被拖住、
多客户端会不会串台、在编辑器里重新点「运行」端口会不会被占死、并发请求会不会串号、
两万行的大 payload 能不能完整重组、`ContextInfo` 抛异常会不会把服务端弄死、
卡住的请求会不会永久挂着、坏回调会不会把接收线程弄死或刷屏。

## 下单之前

**桥会真的下单。** `passorder` 打的是这个策略在QMT界面上绑的那个真实账号，没有模拟盘开关。
所以：

1. **`ALLOW_ORDER` 默认是 `False`。** 查询不受影响，`order_stock` / `cancel_order_stock`
   会直接报错。先把查询链路跑通，确认账号、代码、数量都对了，再去桥接服务里改成 `True`
   并重新「运行」一次。
2. **`PASSORDER_ARGS` 要对。** `passorder` 在不同QMT版本上参数个数不一样（11 / 10 / 8），
   本机是哪个只能试出来。对不上时只会报 `TypeError`，**桥绝不自动换个签名重试** ——
   重试有可能把同一笔单发两次。报错了照日志提示改常量。
3. **只有 11 参数版支持 `userOrderId`（投资备注）。** `passorder` 本身不返回委托号，
   桥靠这个备注回查委托列表把交易所委托号认回来。10 / 8 参数版认不回来，
   `order_stock` 返回的是本地流水号（撤单时这个号也认，但要多一次回查）。
4. **`price_type` 是原样透传给 `passorder` 的 `prType` 的。** 限价 11、最新价 5 两边一致，
   `xtconstant` 里收录了。市价那几档（五档即成剩撤、对手方最优价等）两边编号对不上，
   **没有收录也不做翻译**：要用就查大QMT帮助里 `passorder` 的 `prType` 表，直接填数字。

```python
order_id = trader.order_stock(acc, '000001.SZ', xtconstant.STOCK_BUY,
                              100, xtconstant.FIX_PRICE, 11.39)
trader.cancel_order_stock(acc, order_id)
```

## 接口对齐情况

### 行情（`qmt_bridge.xtdata`）

| 接口 | 状态 |
|---|---|
| `connect` / `reconnect` / `disconnect` / `get_client` | 有，参数表跟官方一致 |
| `subscribe_quote` / `subscribe_quote2` | 有。回调收到 `{code: [tick, ...]}` |
| `subscribe_whole_quote` | 有。回调收到 `{code: tick}` |
| `unsubscribe_quote` / `run` | 有 |
| `get_full_tick` / `get_market_data_ex` / `get_instrument_detail` | 有。`get_market_data_ex` 仍返回 `{code: DataFrame}` |
| `get_stock_list_in_sector` / `get_trading_dates` | 有 |
| `download_history_data` | **没有**。大QMT的补数据是客户端自己管的，桥不转 |

`subscribe_quote` 的 `start_time` / `end_time` / `count` 收下但不生效——
`ContextInfo.subscribe_quote` 不接这几个参数，只有增量推送，不回补历史。要历史请单独调
`get_market_data_ex`。

### 交易（`qmt_bridge.xttrader`）

| 接口 | 状态 |
|---|---|
| `start` / `connect` / `stop` / `sleep` / `run_forever` | 有。`start` 是占位，桥没有后台进程要拉起来 |
| `register_callback` / `subscribe` / `unsubscribe` | 有 |
| `query_stock_asset` / `query_stock_positions` / `query_stock_position` | 有 |
| `query_stock_orders` / `query_stock_order` / `query_stock_trades` | 有 |
| `order_stock` / `cancel_order_stock` / `cancel_order_stock_sysid` | 有，但受 `ALLOW_ORDER` 管着 |
| `order_stock_async` / `cancel_order_stock_async` | 有，**但不是真异步**，只是不等委托号 |
| `get_default_account` | 桥自己加的。**实机上返回 None** —— 这个版本的 ContextInfo 没有 accountid，账号得自己传 |
| `on_account_status` / `smt_*` 那一套 | 没有 |

**交易数据没有推送接口，是轮询出来的。** 桥每 `TRADE_POLL_INTERVAL` 秒（默认 1 秒）
拉一次，QMT 的委托/成交回调会把这一轮立刻叫醒。所以 `on_stock_order` / `on_stock_trade`
比官方晚，最多一秒。没有客户端订阅时不轮询。

`order_status` 用的是官方那套 48~57，`xtconstant.ORDER_STATUS_TEXT` 有中文对照，
`order.cancelable` 直接告诉你还能不能撤。

## 协议

单条 TCP 连接，按行分隔的 JSON（UTF-8）。行情和交易共用一条连接。

```
请求  {"id": 1, "func": "subscribe_quote", "kwargs": {"seq": 7, "stock_code": "000001.SZ", ...}}
响应  {"id": 1, "ok": true, "ret": 7}
      {"id": 1, "ok": false, "err": "RuntimeError: ..."}
推送  {"push": 7, "data": {...}}
交易推送  {"push": 9, "data": {"kind": "order"|"deal"|"asset"|"position",
                              "rows": [...], "init": false}}
```

DataFrame 走 `{"__df__": 1, "index": [...], "columns": [...], "data": [[...]]}`，客户端还原。

**订阅号由客户端分配**，随请求一起发给服务端。这样客户端能在发请求之前就把回调挂好；
要是等服务端返回订阅号再挂，首包推送会先到、然后被丢掉。

**交易对象的字段名不在QMT侧翻译**，`m_xxx` 原样送到本地再翻。QMT侧改一次就要重新粘贴
一次脚本，而各版本的字段名又不尽相同，把这层易变的东西放在本地更好改。
翻译表在 `xttrader.py` 里，每个属性给一串候选名，取第一个有值的。

## 收盘后 / 桥挂了的时候：不走桥，直接读本地日线缓存

这个是实测踩出来的额外发现，跟桥完全独立，写在这单独一节里。

**桥只在交易时段能用。** 收盘后 QMT 不再驱动 `handlebar`，Python 线程被饿死，
上面整套东西全部失效，而且已经确认过：绕开桥、直接用QMT自带那份官方
`xtquant.xtdata`（包括号称读本地的 `get_local_data`）也不行——同样报
「无法连接行情服务」，miniQMT那个服务本来就没注册，跟收盘不收盘无关。

但大QMT自己会把日线K线缓存成本地文件：`<datadir>\<市场>\86400\<代码>.DAT`。
这份缓存不经过那个坏掉的服务，纯粹是文件，`qmt_bridge/dat_reader.py` 反推出了
它的二进制格式（8字节头 + N条64字节定长记录），不需要QMT在跑、不需要桥、
随时能读：

```python
from qmt_bridge.dat_reader import read_daily, scan_gaps

df, gap = read_daily('000300.SH')   # df: 索引YYYYMMDD，列 open/high/low/close/volume/amount/preClose
print(df.tail())
print('本地缓存滞后 %s 天' % gap)

scan_gaps(['600366.SH', '600436.SH'])   # 批量看哪些代码该去「补充数据」补了
```

现成的例子：`local_history.py`。

```
.venv\Scripts\python.exe local_history.py --gaps              # 看持仓里哪些滞后了
.venv\Scripts\python.exe local_history.py 000300.SH -n 20     # 打印最近20条
```

**这份缓存不保证是最新的。** 实测发现它好像只在你在QMT里主动看过/查过某个代码之后
才刷新，不是每天自动补——同一时刻测过的4只代码里，2只（当天被桥查过的）缓存到了
当天，另外2只停在了整整61天前的同一天不动。滞后了就去QMT「数据管理」→「补充数据」
手动点一下，`scan_gaps` / `local_history.py --gaps` 能告诉你该补哪些。

这个格式官方不公开，是拿几个已知代码反推出来的，怎么反推、验证到什么程度、
没验证过什么，都写在 `dat_reader.py` 的模块注释里，改动或者怀疑数据不对之前先看那份。
跑 `python -m qmt_bridge.dat读取自测` 能验一遍格式解析逻辑本身对不对（这部分不依赖
真实QMT数据）加上对本机真实数据的内部一致性抽查（这部分依赖这台机器，别的机器上
自动跳过，不算失败）。

已知只验证过日线（`86400`）周期、沪深两个市场，没验证过分钟线/tick本地缓存，
也没验证过复权价格——这份缓存看着是不复权的原始价。

## 设计上几个不能改的地方

1. **行情回调里只入队，绝不做网络IO。** 回调跑在QMT自己的线程上，在里面阻塞会把整个策略
   和行情一起拖死。每个客户端一个 `queue.Queue(20000)`，满了丢最老的一条，发送交给独立线程。
   丢了多少条会在客户端断开时打到日志里。
2. **所有 `ContextInfo` 和交易接口的调用集中在一个专用线程上串行执行**，避免多个客户端
   并发碰同一个 C++ 上下文对象。交易轮询也排这同一个队 —— 它不是单独开线程去查的。
3. **服务端只认白名单里的函数名**，不做 `eval`/`getattr` 派发，而且只监听 `127.0.0.1`。
   这个端口不会变成任意代码执行的入口。**但它现在能下单了**，所以 `ALLOW_ORDER` 那道闸
   是有意义的，别顺手改成默认 `True`。
4. **服务对象挂在 `builtins` 上**。脚本被重新「运行」时能找到上一轮的实例并干净关掉，
   不会出现端口被僵尸线程占住、或者旧的 `ContextInfo` 还在被引用的情况。
5. **`passorder` 报 `TypeError` 时不换签名重试。** 见上面「下单之前」第 2 条。
6. **引擎回调 `order_callback` / `deal_callback` 一律用 `*args` 收参数。** 各版本参数个数
   不一样，签名对不上引擎会直接报错。这几个回调只负责把轮询叫醒，不读回调带来的对象
   （形状各版本不同，不敢信），一律回头去查权威数据。

## 还没做的

- **断线不会自动重连。** 客户端 `run()` / `run_forever()` 在桥断开时结束，重连逻辑得调用方
  自己写。
- **僵尸端口只能靠重启策略收拾。** QMT 停止策略时会把 Python 线程拆掉，但监听 fd 留在
  QMT 进程里。脚本里有个 `stop(C)` 钩子想在策略停止时主动关掉监听（只关自己起的那个），
  但 QMT 认不认这个入口各版本不一样，所以是尽力而为；不好使就把 `ENABLE_STOP_HOOK`
  改成 False 退回原状。
- **没做限流。** 全推整个市场时的量没实测过，先从少量标的开始。
- **下单的幂等没做。** `order_stock` 发一次就是一次，网络超时之后是发出去了还是没发出去，
  桥不知道也不替你猜 —— 超时了请回查委托列表确认，别直接重发。
- **`order_stock_async` 不是真异步。** 跟同步走一条路，`on_order_stock_async_response`
  只是走个形式。
- **本地日线缓存的格式是反推出来的，不是官方文档。** 见上面那节，那几个标了"未知，
  样本里恒为常数"的字段，样本量只有几千条、四五个代码，别的代码上说不定就不是常数了
  （比如期货结算价、ST股特殊标记），`dat_reader.py` 不会替你验，读出来的数字自己核对。
