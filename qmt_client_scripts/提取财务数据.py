# coding:gbk
"""
本文件要在 QMT 客户端的"策略编辑器"里打开/粘贴运行，不是本地venv/qmt_mock能跑的脚本——
ContextInfo.get_financial_data 是QMT运行时里读本地财务数据库的真实接口，脱离客户端没有数据源。

用途：批量提取 STOCK_LIST 里每只股票的财务字段，导出成本地CSV，方便之后离线用
（比如喂给 qmt_mock，或者拿去做别的分析）。

用法：
1. 在QMT里"新建策略文件"，把本文件内容粘进去（或者直接用文件管理器把这个.py丢进
   D:\\国金证券QMT交易端\\python\\ 目录后在QMT里打开）。
2. 按需修改下面 STOCK_LIST / FIELD_LIST / EXPORT_PATH。
3. 右侧"默认品种"随便选一个能正常出K线的标的即可（本策略本身不依赖主图品种）。
4. 点击"运行"或"回测"都行，会在最后一根K线导出一次。
5. 运行日志里能看到每只股票的取数结果，同时会写一份 EXPORT_PATH 指定的CSV文件。
6. 看到"已导出到 ..."之后可以点"停止"。

FIELD_LIST 里的 (表名, 字段名) 目前只收录了QMT自带示例脚本
（D:\\国金证券QMT交易端\\python\\股本营收资产.py、STOA.py）里验证过真实存在的两张表：
CAPITALSTRUCTURE（股本结构）、PERSHAREINDEX（每股指标）。如果要加别的财务表/字段
（比如利润表、资产负债表、现金流量表的具体科目），需要去QMT官方的Python API文档查
对应的表名和字段名，这里不确定的字段没有瞎编，避免取到不存在的字段导致取数出错或
解释器raise异常。

取数方式说明（重要，踩过的坑）：
1. 一开始用 get_financial_data(表名, 字段名, market, code, index) 这种"按K线下标"取值
   的写法，index传的是ContextInfo.barpos——但barpos是"默认品种"自己K线序列的下标，
   跟STOCK_LIST里查询的股票不是同一个标的，下标对不上，实测全部返回nan（F10页面里
   确认过600000.SH本地是有财务数据的，问题在取数方式，不是没数据）。
2. 改成 get_financial_data(字段列表, 股票列表, 起始日期, 结束日期) 这种按日期区间查
   的写法后，能查到非空的DataFrame了，但直接取"区间内最后一个日期"（也就是运行当天）
   那一行还是nan——因为财务数据是按季度/年报公布的，运行当天这种普通交易日大概率
   没有财务快照落在这天，表里对应那一行本来就是空的，只有真正的报告发布日那几行
   才有值。所以要在区间里找"最后一个不为NaN的值"，而不是"最后一个日期的值"。

调试：DEBUG=True 时每根K线都会打印一行进度日志。排查完问题后可以改回False，避免
回测区间长的时候日志刷屏。
"""

import csv
import os

# ========== 配置区：按需修改 ==========
STOCK_LIST = ["600000.SH", "000001.SZ"]  # 要提取财务数据的股票代码.市场
FIELD_LIST = [
    ("CAPITALSTRUCTURE", "total_capital"),        # 总股本
    ("CAPITALSTRUCTURE", "circulating_capital"),  # 流通股本
    ("PERSHAREINDEX", "inc_revenue"),             # 每股营业收入（累计）
    ("PERSHAREINDEX", "s_fa_bps"),                # 每股净资产
]
EXPORT_PATH = "D:/QuantStock/qmtLocal/real_data/financial_data.csv"
LOOKBACK_START_DATE = "20000101"  # 查财务数据时往前找多早，早于所有股票上市日期即可
DEBUG = True
# ======================================


def init(ContextInfo):
    print("[init] 开始初始化，STOCK_LIST =", STOCK_LIST)
    ContextInfo.set_universe(STOCK_LIST)
    ContextInfo.rows = []
    ContextInfo.exported = False
    print("[init] 初始化完成")


def handlebar(ContextInfo):
    barpos = ContextInfo.barpos
    if DEBUG:
        try:
            bar_date_dbg = timetag_to_datetime(ContextInfo.get_bar_timetag(barpos), "%Y%m%d")
        except Exception as e:
            bar_date_dbg = "取日期失败:{}".format(e)
        print("[handlebar] barpos={} date={} is_last_bar={} exported={}".format(
            barpos, bar_date_dbg, ContextInfo.is_last_bar(), ContextInfo.exported))

    if ContextInfo.exported or not ContextInfo.is_last_bar():
        return

    bar_date = timetag_to_datetime(ContextInfo.get_bar_timetag(barpos), "%Y%m%d")

    rows = []
    for stock in STOCK_LIST:
        row = {"stock": stock, "date": bar_date}
        for tabname, colname in FIELD_LIST:
            key = tabname + "." + colname
            value = None
            try:
                frame = ContextInfo.get_financial_data([key], [stock], LOOKBACK_START_DATE, bar_date)
                if frame is not None and len(frame) > 0 and colname in frame:
                    series = frame[colname].dropna().sort_index()
                    if len(series) > 0:
                        value = series.iloc[-1]
                        print(stock, key, "取到值，报告日=", series.index[-1])
                    else:
                        print(stock, key, "区间内全部是NaN（可能是这个字段确实没有数据）")
                else:
                    print(stock, key, "查询结果为空")
            except Exception as e:
                print(stock, key, "取数失败：", e)
            row[key] = value
        rows.append(row)
        print(row)

    _export(rows)
    ContextInfo.rows = rows
    ContextInfo.exported = True


def _export(rows):
    if not rows:
        print("没有取到任何数据，不导出")
        return
    export_dir = os.path.dirname(EXPORT_PATH)
    if export_dir and not os.path.exists(export_dir):
        os.makedirs(export_dir)
    fieldnames = list(rows[0].keys())
    with open(EXPORT_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print("已导出到", EXPORT_PATH)
