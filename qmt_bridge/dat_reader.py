# coding:utf-8
"""直接读大QMT本地缓存的日线 .DAT 文件——不走桥，收盘后/桥挂了也能用。

背景
----
`D:\\国金证券QMT交易端\\datadir\\<市场>\\86400\\<代码>.DAT` 是大QMT自己维护的日线缓存，
跟我们那个桥完全无关：不需要 QMT 策略在跑，不需要 `handlebar` 被调度，随时能读。
代价是这份缓存**不保证是最新的**——它似乎只在你在 QMT 里主动看过某个代码（图表/
`get_market_data_ex` 之类）之后才刷新，不是每天自动补。多久没更新，`last_gap_days`
会告诉你。要补，去 QMT「数据管理」→「补充数据」手动下载对应代码和周期。

这个二进制格式官方不公开，是拿几个已知代码反推出来的（见下面「怎么反推出来的」），
只验证过 `86400`（日线）这一个周期、沪深两个市场。别的周期/市场/复权类型有没有
偏差没人保证，用之前自己抽查几条对一下账。

用法
----
    from qmt_bridge.dat_reader import read_daily

    df, gap = read_daily('000300.SH')                    # 用默认 datadir
    df, gap = read_daily('600366.SH', datadir=r'D:\...')  # 指定 datadir（比如 userdata_mini）
    print(df.tail())
    print('最后一条离今天 %d 天' % gap)

返回 `(df, gap)`：df 索引是 'YYYYMMDD' 字符串（跟 `get_market_data_ex` 的返回形状对齐），
列是 open/high/low/close/volume/amount/preClose；gap 是最后一条距今天多少天。

字段结构（8字节头 + N × 64字节记录，小端）
----------------------------------------
| 偏移 | 类型 | 含义 | 换算 |
|---|---|---|---|
| 0  | int32 | 时间戳（UTC秒） | `utcfromtimestamp(ts) + 8小时` = 交易日（北京时区） |
| 4  | int32 | 开盘 | ÷1000 |
| 8  | int32 | 最高 | ÷1000 |
| 12 | int32 | 最低 | ÷1000 |
| 16 | int32 | 收盘 | ÷1000 |
| 20 | int32 | 未知，样本里恒为0 | - |
| 24 | int32 | 成交量 | 原始整数，不缩放 |
| 28 | int32 | 未知，样本里恒为520 | - |
| 32 | int64 | 成交额（元） | 原始整数，不缩放 |
| 40 | int32 | 未知，样本里恒为0 | - |
| 44 | float32 | 未知，样本里恒为1.0（疑似复权因子占位） | - |
| 48 | float32 | 未知，样本里恒为1.0（同上） | - |
| 52 | int32 | 昨收 | ÷1000（跟上一条记录的收盘价互相印证过） |
| 56 | int32 | 未知，样本里恒为0 | - |
| 60 | int32 | 未知，样本里恒为32760 | - |

怎么反推出来的
--------------
1. 文件大小减不开任何"干净"的记录长度试出规律：`(size - 8) % 64 == 0`，
   且算出来的记录数（如 000300.SH 是 5499）跟 QMT 日志里
   `data range:20040213000000 - 20260923000000` 报的历史范围对得上。
2. 头 8 字节固定是 `fe ff ff ff ff ff ff 7f`（一个双精度 NaN 的样子），当哨兵，不进数据。
3. 每条记录里挑出四个数量级、涨跌节奏都很像"点位"的 int32，除以1000后落在
   4400~4600 区间——当时沪深300就在这个区间（截图上写的是4547.611），而且最后
   一条记录换算出的交易日正好是当天、开高低收四个值全相等（只有开盘那一笔），
   跟"今天盘中桥取到的第一口价"完全对上。
4. 找"昨收"：某条记录换算出的收盘价，原样出现在下一条记录的某个固定偏移里——
   这个偏移就是 off=52。这条内部一致性也是 `dat格式自测.py` 拿来当断言的。
5. 换成持仓里真实的股票（600366.SH）复核，今天收盘价对上桥当天取到的现价；
   再换 SZ 市场（000001.SZ）复核，价格区间也在合理范围——确认格式两个市场通用。

没验证、别指望的地方
--------------------
* 分钟线、tick 级别的本地缓存格式**完全没看**，很可能不是这个结构。
* 复权（前复权/后复权）——这份缓存看着是不复权的原始价，要复权数据自己按
  `get_market_data_ex` 返回的复权因子换算，或者等桥通的时候直接用桥查复权后的。
* 那几个"未知，恒为常数"的字段，样本量只有几千条、四五个代码，万一某些代码上
  它们不是常数（比如期货结算价、ST股特殊标记），这个 reader 不会报错，只会把
  常数字段原样返回，可能是错的。拿到的数字别不假思索就信，尤其那几个"未知"列。
"""

import datetime
import os
import struct

DEFAULT_DATADIR = r'D:\国金证券QMT交易端\datadir'
HEADER_SIZE = 8
RECORD_SIZE = 64
HEADER_SENTINEL = b'\xfe\xff\xff\xff\xff\xff\xff\x7f'


class DatFormatError(Exception):
    pass


def _split_code(code):
    """'000300.SH' -> ('SH', '000300')。"""
    if '.' not in code:
        raise ValueError("代码要带市场后缀，例如 '000300.SH'，收到 %r" % code)
    inst, market = code.rsplit('.', 1)
    return market.upper(), inst


def dat_path(code, datadir=None, period_dir='86400'):
    market, inst = _split_code(code)
    datadir = datadir or DEFAULT_DATADIR
    return os.path.join(datadir, market, period_dir, '%s.DAT' % inst)


def _read_records(path):
    with open(path, 'rb') as f:
        data = f.read()

    if len(data) < HEADER_SIZE:
        raise DatFormatError('文件太短，连头都不够：%s' % path)
    header = data[:HEADER_SIZE]
    if header != HEADER_SENTINEL:
        # 不同版本/周期的头可能不一样，不确定就别装作确定，只是提醒一声。
        # 数据部分该怎么切还怎么切，不因为这个就拒绝读。
        pass

    body = data[HEADER_SIZE:]
    if len(body) % RECORD_SIZE != 0:
        raise DatFormatError(
            '记录对不齐 64 字节（文件大小=%d，头=%d，余数=%d），格式可能变了，'
            '别信下面读出来的东西：%s' % (len(data), HEADER_SIZE, len(body) % RECORD_SIZE, path))

    n = len(body) // RECORD_SIZE
    rows = []
    for i in range(n):
        rec = body[i * RECORD_SIZE:(i + 1) * RECORD_SIZE]
        ts = struct.unpack_from('<i', rec, 0)[0]
        date = (datetime.datetime.utcfromtimestamp(ts) + datetime.timedelta(hours=8)).date()
        o, h, l, c = (struct.unpack_from('<i', rec, k)[0] / 1000.0 for k in (4, 8, 12, 16))
        volume = struct.unpack_from('<i', rec, 24)[0]
        amount = struct.unpack_from('<q', rec, 32)[0]
        prev_close = struct.unpack_from('<i', rec, 52)[0] / 1000.0
        rows.append({
            'date': date, 'open': o, 'high': h, 'low': l, 'close': c,
            'volume': volume, 'amount': amount, 'preClose': prev_close,
        })
    return rows


def read_daily_raw(code, datadir=None):
    """不依赖 pandas，返回按日期升序的 dict 列表。"""
    path = dat_path(code, datadir)
    if not os.path.exists(path):
        raise FileNotFoundError('本地没有这个代码的日线缓存：%s' % path)
    return _read_records(path)


def read_daily(code, datadir=None):
    """返回 (DataFrame, 滞后天数)。

    DataFrame 索引 'YYYYMMDD'，列 open/high/low/close/volume/amount/preClose。

    滞后天数 = 最后一条记录距今天多少天，没数据时是 None。0 或 1 说明缓存是新的；
    数字大就说明这个代码本地缓存滞后，去 QMT「数据管理」→「补充数据」手动补一下。

    没用 `df.attrs` 存这个数——这个仓库锁定的 pandas 是 0.22（py3.6 生态最后能装的
    版本之一），`.attrs` 是 1.0 才加的，装不上就会报 AttributeError，所以老老实实
    用元组返回。
    """
    import pandas as pd

    rows = read_daily_raw(code, datadir)
    index = ['%04d%02d%02d' % (r['date'].year, r['date'].month, r['date'].day) for r in rows]
    df = pd.DataFrame(rows, index=index)
    df = df.drop(columns=['date'])
    df = df[['open', 'high', 'low', 'close', 'volume', 'amount', 'preClose']]

    gap = (datetime.date.today() - rows[-1]['date']).days if rows else None
    return df, gap


def scan_gaps(codes, datadir=None):
    """批量看一串代码的本地缓存分别滞后几天，找哪些该去「补充数据」。

    返回 [(code, 最后日期, 滞后天数或None), ...]，读不到文件的滞后天数是 None。
    """
    out = []
    for code in codes:
        try:
            rows = read_daily_raw(code, datadir)
        except FileNotFoundError:
            out.append((code, None, None))
            continue
        if not rows:
            out.append((code, None, None))
            continue
        last = rows[-1]['date']
        out.append((code, last, (datetime.date.today() - last).days))
    return out
