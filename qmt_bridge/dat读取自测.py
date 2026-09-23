# coding:utf-8
"""dat_reader 自测。

    .venv\\Scripts\\python.exe -m qmt_bridge.dat读取自测

分两段：
1) 人工构造一个假 .DAT 文件，纯格式解析的往返测试——不挑机器，哪儿都能跑。
2) 如果这台机器上有真实的大QMT datadir，额外对已知代码的真实数据做几条
   内部一致性检查（昨收链、价格量级）。**这段依赖具体机器的本地文件，
   跑不了就跳过，不算失败** —— 别的机器上这个目录不存在是正常的。
"""
import datetime
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qmt_bridge import dat_reader

failures = []


def check(name, cond, detail=''):
    print(('  OK   ' if cond else '  FAIL ') + name + ('' if cond else '  <- ' + str(detail)))
    if not cond:
        failures.append(name)


def make_record(date, o, h, l, c, volume, amount, prev_close,
                 unk20=0, unk28=520, unk40=0, unk44=1.0, unk48=1.0, unk56=0, unk60=32760):
    """按 dat_reader 文档里那张字段表拼一条 64 字节记录。"""
    # 不能用 datetime.timestamp()：会掺进本机时区，不可控。直接手算
    # UTC 当天 16:00（= 北京时间次日 00:00）对应的 epoch 秒。
    epoch_day = (date - datetime.date(1970, 1, 1)).days
    ts = epoch_day * 86400 - 8 * 3600
    return struct.pack(
        '<i i i i i i i i q i f f i i i',
        ts,
        int(round(o * 1000)), int(round(h * 1000)), int(round(l * 1000)), int(round(c * 1000)),
        unk20, volume, unk28,
        amount,
        unk40, unk44, unk48,
        int(round(prev_close * 1000)), unk56, unk60,
    )


print('1) 往返测试（构造假文件，不依赖真实环境）')

d1 = datetime.date(2026, 9, 22)
d2 = datetime.date(2026, 9, 23)
rec1 = make_record(d1, 10.50, 10.80, 10.40, 10.70, 123456, 1_300_000_000, 10.45)
rec2 = make_record(d2, 10.72, 10.90, 10.60, 10.75, 98765, 1_050_000_000, 10.70)

tmp = tempfile.NamedTemporaryFile(suffix='.DAT', delete=False)
tmp.write(dat_reader.HEADER_SENTINEL + rec1 + rec2)
tmp.close()

try:
    rows = dat_reader.read_daily_raw('999999.SH', datadir=None)
    check('没这个文件应该报 FileNotFoundError', False, '居然读到了 %d 条' % len(rows))
except FileNotFoundError:
    check('查无此代码要报 FileNotFoundError', True)

rows = dat_reader._read_records(tmp.name)
check('解出两条记录', len(rows) == 2, len(rows))
if len(rows) == 2:
    r1, r2 = rows
    check('第1条日期', r1['date'] == d1, r1['date'])
    check('第2条日期', r2['date'] == d2, r2['date'])
    check('第1条开高低收', (r1['open'], r1['high'], r1['low'], r1['close']) == (10.5, 10.8, 10.4, 10.7), r1)
    check('第2条昨收=第1条收盘（构造时就这么设的）', r2['preClose'] == r1['close'], (r2['preClose'], r1['close']))
    check('成交量原样', r1['volume'] == 123456, r1['volume'])
    check('成交额是64位，装得下十亿级', r1['amount'] == 1_300_000_000, r1['amount'])

# 记录对不齐 64 字节要报错，不能悄悄返回错的东西
bad = tempfile.NamedTemporaryFile(suffix='.DAT', delete=False)
bad.write(dat_reader.HEADER_SENTINEL + rec1 + b'\x00' * 10)   # 故意留 10 个字节的尾巴
bad.close()
try:
    dat_reader._read_records(bad.name)
    check('记录长度对不齐要报错', False)
except dat_reader.DatFormatError as e:
    check('记录长度对不齐报 DatFormatError', True)
finally:
    os.unlink(bad.name)

try:
    import pandas as pd  # noqa: F401
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

if HAS_PANDAS:
    # read_daily() 只接受代码（走 dat_path 拼真实路径），不接受直接传文件路径，
    # 这里单独把 DataFrame 组装那段逻辑（read_daily 内部做的事）搬出来测一遍。
    idx = ['%04d%02d%02d' % (r['date'].year, r['date'].month, r['date'].day) for r in rows]
    df = pd.DataFrame(rows, index=idx).drop(columns=['date'])
    check('DataFrame 索引是 YYYYMMDD', list(df.index) == ['20260922', '20260923'], list(df.index))
else:
    print('  跳过 DataFrame 组装检查（没装 pandas）')

os.unlink(tmp.name)


print()
print('2) 真实数据内部一致性检查（依赖这台机器上的大QMT datadir）')

if not os.path.isdir(dat_reader.DEFAULT_DATADIR):
    print('  跳过：这台机器上没有 %s' % dat_reader.DEFAULT_DATADIR)
else:
    SAMPLE_CODES = ['000300.SH', '600366.SH', '600436.SH', '605208.SH', '000001.SZ']
    tested_any = False
    for code in SAMPLE_CODES:
        try:
            rows = dat_reader.read_daily_raw(code)
        except FileNotFoundError:
            continue
        tested_any = True
        n = len(rows)
        check('%s 至少有几百条日线' % code, n > 200, n)

        # 昨收链：多数情况下这条的 preClose 该等于上一条的 close（浮点，容许极小误差）。
        # 不是100%要求相等——除权除息（送股/大额分红）那天，preClose 反映的是除权后的
        # 参考价，跟前一天未复权的收盘价本来就对不上，这是市场机制，不是解码错误。
        # 实测样本（600366.SH）里两条对不上的日子分别是 2001-04-26、2002-05-20，
        # 收盘价单日跌了三成多，正是除权跳空的样子。这里只要求"绝大多数对得上"。
        bad_links = 0
        for i in range(1, min(n, 500)):     # 抽样验证最近 500 条，别把大代码拖太久
            if abs(rows[i]['preClose'] - rows[i - 1]['close']) > 0.001:
                bad_links += 1
        checked = min(n, 500) - 1
        rate = 1 - bad_links / checked if checked else 1
        check('%s 昨收链基本自洽（抽样 %d 条，match率 %.1f%%）' % (code, checked, rate * 100),
              rate >= 0.85, '%d/%d 条对不上，多半是除权除息' % (bad_links, checked))

        # 日期严格递增
        dates = [r['date'] for r in rows]
        check('%s 日期严格递增' % code, dates == sorted(set(dates)) and len(set(dates)) == n,
              '共%d条，去重后%d个日期' % (n, len(set(dates))))

        # 高低开收的大小关系要合理
        bad_ohlc = sum(1 for r in rows if not (r['low'] <= r['open'] <= r['high']
                                                and r['low'] <= r['close'] <= r['high']
                                                and r['low'] <= r['high']
                                                and r['low'] > 0))
        check('%s OHLC 大小关系合理' % code, bad_ohlc == 0, '%d 条不合理' % bad_ohlc)

    if tested_any and HAS_PANDAS:
        gaps = dat_reader.scan_gaps(SAMPLE_CODES)
        print('  各代码本地缓存滞后天数：')
        for code, last, gap in gaps:
            print('    %-12s 最后=%s  滞后=%s天' % (code, last, gap))

    if not tested_any:
        print('  跳过：这几个示例代码本地都没有缓存文件（换过账号/持仓？）')

print()
print('FAILURES: %d' % len(failures))
sys.exit(1 if failures else 0)
