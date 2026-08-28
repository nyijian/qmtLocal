# qmt_mock 本地模拟运行框架

不打开国金证券QMT交易端（迅投QMT），本地跑一遍策略脚本的 `init`/`handlebar` 逻辑，
用来在编辑器里就发现语法错误、`NameError`、逻辑bug，再回QMT客户端里正式编译/运行/回测。

## 背景

- QMT客户端策略编辑器（"新建策略文件-策略编辑器"）内置的是QMT自己的Python 3.6运行时，
  策略脚本里用到的 `ContextInfo`/`C`、`passorder`、`get_trade_detail_data`、
  `timetag_to_datetime`、`C.draw_text` 等都是QMT运行时注入的全局变量/方法，脱离QMT客户端
  无法直接 `import` 或运行。
- 本地QMT安装目录：`D:\国金证券QMT交易端`，其策略Python环境的 site-packages 在
  `D:\国金证券QMT交易端\bin.x64\Lib\site-packages`（py3.6，numpy/pandas/talib等都是
  对应ABI预编译的二进制）。
- 本项目 `D:\QuantStock\qmtLocal` 用 `D:\QuantStock\python36`（Python 3.6.8）建了一个venv
  （`.venv`），并把QMT自带的 numpy 1.19.1 / pandas 0.22.0 / talib 0.4.17 等直接从上面的
  site-packages拷贝过来，保证版本、二进制ABI跟QMT客户端里完全一致（这些包在py3.6上现在
  基本没有可pip安装的wheel了，只能照搬QMT自带的）。
- `qmt_mock` 就是在这套环境基础上，补一个**本地假的QMT运行时**，让策略脚本能在没有QMT
  客户端、没有真实行情连接的情况下被直接执行一遍。

## 从QMT拷贝的第三方包（环境复现用）

`.venv` 本身只有 pip/setuptools，以下都是从
`D:\国金证券QMT交易端\bin.x64\Lib\site-packages` **直接拷贝**（不是pip安装）到
`.venv\Lib\site-packages` 的，版本/二进制ABI跟QMT客户端里完全一致：

| 包（目录/文件） | 对应 dist-info | 版本 |
|---|---|---|
| `numpy/` | `numpy-1.19.1.dist-info/` | 1.19.1 |
| `pandas/` | `pandas-0.22.0.dist-info/` | 0.22.0 |
| `talib/` | `TA_Lib-0.4.17.dist-info/` | 0.4.17 |
| `dateutil/` | `python_dateutil-2.8.0.dist-info/` | 2.8.0 |
| `pytz/` | `pytz-2018.3.dist-info/` | 2018.3 |
| `six.py` | `six-1.12.0.dist-info/` | 1.12.0 |

如果 `.venv` 需要重建，用下面的PowerShell脚本原样复现这一步（先按前文重新建好基于
`D:\QuantStock\python36` 的venv，再执行）：

```powershell
$src = "D:\国金证券QMT交易端\bin.x64\Lib\site-packages"
$dst = "D:\QuantStock\qmtLocal\.venv\Lib\site-packages"
$items = @(
  "numpy", "numpy-1.19.1.dist-info",
  "pandas", "pandas-0.22.0.dist-info",
  "dateutil", "python_dateutil-2.8.0.dist-info",
  "pytz", "pytz-2018.3.dist-info",
  "six.py", "six-1.12.0.dist-info",
  "talib", "TA_Lib-0.4.17.dist-info"
)
foreach ($i in $items) {
  $s = Join-Path $src $i
  $d = Join-Path $dst $i
  if (Test-Path $s -PathType Container) {
    robocopy $s $d /E /NFL /NDL /NJH /NJS /NC /NS | Out-Null
  } else {
    Copy-Item $s $d -Force
  }
}
```

拷贝完用venv的python验证一下：

```
.venv\Scripts\python.exe -c "import pandas, numpy, talib; print(pandas.__version__, numpy.__version__, talib.__version__)"
```

应输出 `0.22.0 1.19.1 0.4.17`。

> 另外venv里的 `xtquant` 包不是从这里拷的，是单独pip安装的QMT官方Python交易/行情API
> （`xtdata`/`xttrader`），跟 `bin.x64\Lib\site-packages` 无关；它的数据接口需要连着运行中的
> QMT客户端才能用（见下方"局限性"），跟`qmt_mock`的本地模拟是两回事。

## 目录结构

```
qmt_mock/
  __init__.py
  data.py     生成确定性的模拟K线数据（SyntheticMarket / trading_calendar）
  context.py  模拟ContextInfo对象 + 模拟全局函数（PaperAccount / MockContextInfo / build_globals）
  runner.py   命令行入口，加载策略文件、跑逐bar循环
  README.md   本文件
```

## 使用方法

在项目根目录（`D:\QuantStock\qmtLocal`）下，用venv的python以模块方式运行：

```
.venv\Scripts\python.exe -m qmt_mock.runner <策略文件.py> [选项]
```

例如：

```
.venv\Scripts\python.exe -m qmt_mock.runner main.py --code 600000.SH --start 20240101 --end 20240301 --seed 42
```

命令行参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `strategy`（位置参数） | 无，必填 | 策略`.py`文件路径 |
| `--code` | `000300.SH` | 主图/测试标的，格式 `代码.市场`，如 `600000.SH` |
| `--start` | `20240101` | 回测起始日期 `YYYYMMDD` |
| `--end` | `20240601` | 回测结束日期 `YYYYMMDD` |
| `--period` | `1d` | K线周期，目前数据只按日线生成 |
| `--cash` | `1000000.0` | 模拟账户初始资金 |
| `--seed` | 随机 | 固定后每次生成的模拟行情一致，便于复现调试 |

运行结束会打印每根bar的策略输出（策略脚本里的`print`），以及最后的模拟账户资金/持仓汇总。

Windows控制台如果中文输出乱码，是终端代码页问题，不是脚本问题；PowerShell下可先执行
`$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8` 再运行。

## 各模块说明

### `data.py`
- `trading_calendar(start_date, end_date)`：生成起止日期间的工作日列表（周一到周五，不排节假日）。
- `SyntheticMarket`：按股票代码用固定种子做随机游走，生成 `open/high/low/close/volume/amount`
  的DataFrame（索引为`YYYYMMDD`日期字符串）。同一代码+同一seed每次生成的数据完全一致；
  同一次运行里对同一代码只生成一次，会缓存复用。

### `context.py`
- `MockContextInfo`：模拟QMT的 `ContextInfo`/`C` 对象，持有 `stockcode`/`market`/`period`/
  `barpos`、内部的行情生成器和 `PaperAccount`。已实现：
  - `get_bar_timetag(barpos)`
  - `get_market_data_ex(field_list, stock_list, end_time, period, count, dividend_type, fill_data, subscribe)`
  - `draw_text(chart_id, sub_id, text)`（直接打印到控制台）
- `PaperAccount`：极简纸面账户，`buy`/`sell`按现金/持仓量做加减，`account_detail`/
  `position_detail`返回跟QMT一致字段名（`m_dAvailable`、`m_strInstrumentID`、
  `m_strExchangeID`、`m_nVolume`等）的对象列表。
- `build_globals(context)`：返回要注入到策略脚本执行命名空间里的全局函数：
  - `timetag_to_datetime(timetag, fmt)`
  - `passorder(opType, orderType, accountid, orderCode, prType, price, volume, C)`：
    按**下单当根K线的收盘价**成交（`price=-1/0`时用收盘价，否则用传入价），只支持
    `optype=23`（买入，`STOCK_BUY`）和`24`（卖出，`STOCK_SELL`），其余类型会打印提示后忽略。
  - `get_trade_detail_data(accountid, accounttype, datatype, strategyname)`：`datatype`为
    `'account'`或`'position'`时返回对应数据，忽略`accountid`（本框架每次运行只有一个模拟账户）。

### `runner.py`
- `run(strategy_path, code, start, end, period, cash, seed)`：核心流程——
  1. 建 `MockContextInfo` + 交易日历。
  2. 读取策略源码，`exec` 到一个预先注入了`passorder`等全局函数的命名空间里（策略脚本
     不是被`import`的，而是像QMT编辑器里"运行"一样被整体执行）。
  3. 依次调用 `init(C)`，然后按交易日历逐bar调用 `handlebar(C)`（每次先更新`C.barpos`）。
  4. 打印结束后的账户资金、持仓。
- `main()`：命令行参数解析入口（`python -m qmt_mock.runner ...`）。

## 局限性（重要）

- **行情是合成的随机游走数据，不是真实历史行情**，只能用来验证策略代码本身能不能跑通、
  逻辑分支对不对，**不能**用来评估策略的真实收益/回撤——真实回测必须回到QMT客户端里，
  用QMT下载好的真实历史数据跑。
- 成交撮合是"按下单当根K线收盘价全部成交"的简化模型，不模拟滑点、涨跌停、盘口价格、
  部分成交等。
- 只实现了目前策略脚本实际用到的QMT接口子集（见上）。像 `set_universe`/`get_universe`、
  `ContextInfo.paint`、`get_history_data`、期货/期权相关接口等**还没实现**，用到会直接
  `AttributeError`/`NameError`。
- venv里的 `xtquant`（`xtdata`/`xttrader`）是QMT官方的Python交易/行情API包，它的接口内部
  都要连接一个正在运行的QMT/MiniQMT客户端进程（本地RPC），脱离客户端调用会抛
  `"无法连接行情服务！"`。`qmt_mock` 不依赖也不使用 `xtquant`，是完全独立的本地模拟实现。

## 如何扩展（遇到未实现的QMT接口时）

1. 先去 `D:\国金证券QMT交易端\python\_PyContextInfo.py` 或对应示例脚本确认该接口的参数、
   返回值格式。
2. 如果是 `ContextInfo`/`C`的方法：在 `context.py` 的 `MockContextInfo` 类里加一个同名方法。
3. 如果是模块级全局函数（跟`passorder`一样直接裸调用，不经过`C.`）：在 `context.py` 的
   `build_globals(context)` 里加一个闭包函数，塞进返回的字典。
4. 不需要改 `runner.py`——新加的方法/函数只要挂在 `MockContextInfo`/`build_globals`上就会
   自动对新策略脚本生效。
