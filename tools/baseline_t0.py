# coding:utf-8
"""做 T 对照组（基准线）：不看任何信号，每天固定做一次，别的策略都要跟它比。

    .venv313\\Scripts\\python.exe tools\\baseline_t0.py                          # 默认 600366 605208 600436
    .venv313\\Scripts\\python.exe tools\\baseline_t0.py 600366=800 605208=600   # 代码=每次股数，算成金额
    .venv313\\Scripts\\python.exe tools\\baseline_t0.py --slip 0.5             # 每边加半个价位滑点

只在 .venv313 跑（用了 3.7+ 写法）。不下单。叫法按用户的（2026-10-21）：
  固定反T = 开盘卖底仓、收盘买回（赚「开盘到收盘下跌」）
  固定正T = 开盘买、收盘卖掉底仓（赚「开盘到收盘上涨」）
两者毛收益正好相反，扣两遍费用，所以必然至少有一个是亏的；看的是：开→收本身有没有方向、有多大，费用吃掉多少。

另按「昨收在 20 日均线上方 / 下方」（--ma 改天数）分组再算一遍：均线用前复权价、只用昨天及以前的数据，开盘前就知道。

数据：QMT 本地日线（.DAT），2015 年起。开盘、收盘都可以走集合竞价按开盘价 / 收盘价成交，默认不加滑点。
一字板（最高 = 最低）那天买不进或卖不出，跳过。费用按 sim_t0/params.json（万0.8、最低 5 元、印花税、过户费），
按每次股数 × 价格算成交额；不给股数就按每笔 3 万元算。
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from t0_sim import load_params  # noqa: E402

DEFAULT = ['600366=800', '605208=600', '600436=100']   # 实盘额度 / 片仔癀模拟盘股数
MA_N = 20


def code_of(c):
    c = c.strip().upper()
    return c if '.' in c else c + ('.SH' if c[0] in '659' else '.SZ')


def fees(p, amount):
    """一买一卖的总费用（元）。"""
    return 2 * max(amount * p['commission'], p['min_commission']) + amount * (p['stamp_tax'] + 2 * p['transfer_fee'])


def stats(x):
    n = len(x)
    if n < 3:
        return {'天数': n}
    return {'天数': n, '平均%': x.mean(), 't': x.mean() / x.std() * np.sqrt(n) if x.std() > 0 else np.nan,
            '胜率%': (x > 0).mean() * 100, '最差一天%': x.min(), '最好一天%': x.max()}


def run(code, qty, p, slip, since, ma=20):
    from qmt_bridge.dat_reader import read_daily
    d, lag = read_daily(code)
    d = d[(d.index >= since) & (d['volume'] > 0) & (d['high'] > d['low'])].copy()
    oc = (d['close'] / d['open'] - 1) * 100                          # 开→收
    amt = (qty * d['open']) if qty else pd.Series(30000.0, index=d.index)
    cost = pd.Series([fees(p, a) / a * 100 for a in amt], index=d.index) + 2 * slip * 0.01 / d['open'] * 100
    res = pd.DataFrame({'反T毛': -oc, '正T毛': oc, '成本': cost})
    res['反T净'] = res['反T毛'] - res['成本']
    res['正T净'] = res['正T毛'] - res['成本']
    res['反T元'] = res['反T净'] / 100 * amt
    res['正T元'] = res['正T净'] / 100 * amt
    res['年'] = res.index.str[:4]
    res['均线'] = ma_side(code, d.index, ma, since)
    return d, res, lag


def ma_side(code, days, n, since):
    """开盘前就知道的条件：昨收在「截至昨天的 n 日均线」上方还是下方。
    .DAT 是不复权的，先用 preClose / 前一天 close 算出除权因子做后复权，免得除权缺口把均线拉歪。"""
    from qmt_bridge.dat_reader import read_daily
    d, _ = read_daily(code)
    d = d[d['volume'] > 0]
    f = (d['preClose'] / d['close'].shift(1)).fillna(1.0)        # 除权日 < 1，平时 = 1
    adj = d['close'] * f[::-1].cumprod()[::-1].shift(-1).fillna(1.0)   # 前复权：乘上之后所有除权因子
    ma = adj.rolling(n).mean()
    side = pd.Series(np.where(adj > ma, '上方', np.where(adj < ma, '下方', '-')), index=d.index)
    side[ma.isna()] = '-'
    return side.shift(1).reindex(days).fillna('-')           # 用昨天的状态


def report(code, qty, d, res, lag):
    pd.set_option('display.unicode.east_asian_width', True)
    pd.set_option('display.width', 200)
    print('\n' + '=' * 96)
    print('%s %s~%s 共 %d 天（日线滞后 %s 天）%s，平均来回成本 %.3f%%' % (
        code, res.index[0], res.index[-1], len(res), lag,
        '，每次 %d 股（约 %.0f 元）' % (qty, qty * d['close'].iloc[-1]) if qty else '，每笔按 3 万元',
        res['成本'].mean()))
    print('=' * 96)
    rows = []
    for name, sub in (('全部', res), ('近250天', res.tail(250)), ('近60天', res.tail(60)), ('近20天', res.tail(20))):
        for side in ('反T', '正T'):
            s = stats(sub[side + '净'])
            rows.append({'区间': name, '方式': '固定' + side + ('（开盘卖、收盘买）' if side == '反T' else '（开盘买、收盘卖）'),
                         '扣费前%': sub[side + '毛'].mean(), **s, '累计元': sub[side + '元'].sum() if qty else np.nan})
    t = pd.DataFrame(rows)
    print(t.round(2).to_string(index=False))

    print('\n  逐年（扣费后，每天平均 %；括号里是胜率）：')
    y = res.groupby('年').agg(天数=('反T净', 'size'), 开到收平均=('正T毛', 'mean'),
                             反T净=('反T净', 'mean'), 反T胜率=('反T净', lambda s: (s > 0).mean() * 100),
                             正T净=('正T净', 'mean'), 正T胜率=('正T净', lambda s: (s > 0).mean() * 100))
    if qty:
        y['反T累计元'] = res.groupby('年')['反T元'].sum()
        y['正T累计元'] = res.groupby('年')['正T元'].sum()
    print('  ' + y.round(2).to_string().replace('\n', '\n  '))
    print('\n  按「昨收在 %d 日均线上方 / 下方」分组（开盘前就知道；扣费后每天平均 %%，前/后半 = 时间上前一半 / 后一半，'
          '同号年数 = 逐年平均跟全部同号的年份数）：' % MA_N)
    rows = []
    for g, sub in res[res['均线'] != '-'].groupby('均线'):
        h = len(sub) // 2
        for side in ('反T', '正T'):
            x = sub[side + '净']
            s = stats(x)
            yearly = sub.groupby('年')[side + '净'].mean()
            rows.append({'昨收': '均线' + g, '方式': '固定' + side, '天数': s['天数'], '扣费前%': sub[side + '毛'].mean(),
                         '扣费后%': s.get('平均%'), 't': s.get('t'), '胜率%': s.get('胜率%'),
                         '前半': x.iloc[:h].mean(), '后半': x.iloc[h:].mean(),
                         '同号年数': '%d/%d' % ((np.sign(yearly) == np.sign(x.mean())).sum(), len(yearly)),
                         '近250天': sub[sub.index >= res.index[-250]][side + '净'].mean(),
                         '累计元': sub[side + '元'].sum() if qty else np.nan})
    print('  ' + pd.DataFrame(rows).round(2).to_string(index=False).replace('\n', '\n  '))
    big = (res['正T毛'].abs() >= 3).mean() * 100
    print('\n  开→收的绝对值 ≥3%% 的天数占 %.0f%%：这些天里做反了，一天亏的抵得上很多天的小赚' % big)


def main():
    ap = argparse.ArgumentParser(description='做 T 对照组：固定反T（开盘卖收盘买）/ 固定正T（开盘买收盘卖）')
    ap.add_argument('codes', nargs='*', default=DEFAULT, help='代码 或 代码=每次股数')
    ap.add_argument('--slip', type=float, default=0.0, help='每边滑点几个价位，默认 0（集合竞价成交）')
    ap.add_argument('--since', default='20150101')
    ap.add_argument('--ma', type=int, default=20, help='分组用的均线天数，默认 20')
    a = ap.parse_args()
    global MA_N
    MA_N = a.ma
    p = load_params()
    print('费用：佣金 %.4f%%（最低 %.0f 元）+ 卖出印花税 %.3f%% + 过户费；滑点每边 %.1f 个价位' % (
        p['commission'] * 100, p['min_commission'], p['stamp_tax'] * 100, a.slip))
    for c in a.codes:
        code, _, q = c.partition('=')
        code, qty = code_of(code), int(q) if q else 0
        d, res, lag = run(code, qty, p, a.slip, a.since, a.ma)
        report(code, qty, d, res, lag)


if __name__ == '__main__':
    main()
