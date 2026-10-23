# coding:utf-8
"""资金足迹（订单流）：从每天存的 tick 里提取「对手」留下的痕迹，按 5 分钟攒成表，天数够了检验能不能预测接下来的涨跌。

    .venv313\\Scripts\\python.exe tools\\order_flow.py                  # 最近一个存过 tick 的交易日：当天足迹 + 写入累计表
    .venv313\\Scripts\\python.exe tools\\order_flow.py 20261021 600366
    .venv313\\Scripts\\python.exe tools\\order_flow.py --study          # 用累计的所有天检验：足迹能不能预测之后 5/15/30 分钟

只在 .venv313 跑（用了 3.7+ 写法）。不下单。tick_archive.py 收工后会自动调（只做当天记录，不跑 --study）。

能从 tick 里看到的（看不到「是谁」，只能看到「怎么下的单」）：
  外盘 / 内盘（= 主动买 / 主动卖）：3 秒快照里新增的成交量，成交价 ≥ 上一笔卖一 = 外盘（买方主动吃单）；
                    ≤ 上一笔买一 = 内盘（卖方主动砸单）；夹在中间不算。QMT 的 tick 是 3 秒快照，一个快照里可能混着买卖，这是近似，
                    跟行情软件显示的内外盘数字会有出入。
  外内比 / 净主动量：外盘 ÷ 内盘（这 5 分钟、开盘到此刻累计两种）；(外盘 - 内盘) ÷ 总量。
  量比：开盘到此刻累计成交量 ÷（过去 5 天平均每分钟成交量 × 已开盘分钟数），行情软件的通行算法；
        5 分钟量比：这 5 分钟的量 ÷ 过去 5 天同一个 5 分钟的平均量。过去 5 天的量取 5 分钟线（factor_model.bars_5m）。
  大单：当天成交量最大 1% 的快照，分主动买卖；平均每笔手数（成交量 ÷ 成交笔数）偏大说明有大资金。
  挂单失衡：五档买盘总量 vs 卖盘总量，(买 - 卖) ÷ (买 + 卖)。
  吸收：主动方向和价格方向背离——主动买多但价格跌（上方有人挂单接货 / 出货），或主动卖多但价格涨（下方有人挂单托住 / 吸筹）。
  集合竞价：开盘、收盘集合竞价量占全天比例。
累计表：共享数据目录 order_flow/<代码>.csv，每 5 分钟一行（同一天重跑覆盖）。
--study：每个足迹指标跟之后 5 / 15 / 30 分钟涨跌的秩相关，按天聚合算 t、逐日同号比例。少于 20 天只给参考。
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from t0_sim import DATA_DIR  # noqa: E402

TICK_DIR = os.path.join(DATA_DIR, 'ticks')
OUT_DIR = os.path.join(DATA_DIR, 'order_flow')
CODES = ['600366.SH', '605208.SH', '600436.SH']
FEATS = ['净主动量', '大单净主动量', '挂单失衡', '每笔手数比', '吸收', '近15分净主动量',
         '量比', '5分钟量比', '累计外内比']


def minutes_open(hhmm):
    """09:30 到 hhmm 已开盘的分钟数（扣掉午休）。"""
    m = int(hhmm[:2]) * 60 + int(hhmm[2:4])
    if m <= 11 * 60 + 30:
        return max(m - (9 * 60 + 30), 1)
    return 120 + max(m - 13 * 60, 0)


def past_volume(code, day, n=5):
    """过去 n 个交易日：平均每分钟成交量、每个 5 分钟时段（K 线结束时刻）的平均量。没数据返回 (None, {})。"""
    try:
        from factor_model import bars_5m
        b = bars_5m(code)
    except Exception:
        return None, {}
    if b is None:
        return None, {}
    days = sorted(d for d in set(b.index.str[:8]) if d < day)[-n:]
    if len(days) < n:
        return None, {}
    x = b[b.index.str[:8].isin(days)]
    per_min = x['volume'].sum() / len(days) / 240.0
    slot = x.groupby(x.index.str[8:12])['volume'].mean().to_dict()
    return per_min, slot


def code_of(c):
    c = c.strip().upper()
    return c if '.' in c else c + ('.SH' if c[0] in '659' else '.SZ')


def load(day, code):
    path = os.path.join(TICK_DIR, day, '%s.csv.gz' % code)
    if not os.path.exists(path):
        return None
    t = pd.read_csv(path, dtype={'stime': str})
    t['hms'] = t['stime'].str[8:14]
    t = t[t['lastPrice'] > 0].reset_index(drop=True)
    t['dv'] = t['volume'].diff().fillna(t['volume']).clip(lower=0)
    t['dn'] = t['transactionNum'].diff().fillna(t['transactionNum']).clip(lower=0)
    pb, pa = t['bid1'].shift(1), t['ask1'].shift(1)
    side = np.where(t['lastPrice'] >= pa, 1, np.where(t['lastPrice'] <= pb, -1, 0))
    side[(t['hms'] < '093000').values | (t['hms'] > '145700').values] = 0
    t['side'] = side
    bv = t[['bidVol%d' % i for i in range(1, 6)]].sum(axis=1)
    av = t[['askVol%d' % i for i in range(1, 6)]].sum(axis=1)
    t['imb'] = (bv - av) / (bv + av).replace(0, np.nan)
    return t


def bars(t, per_min=None, slot_avg=None):
    """连续竞价按 5 分钟聚合足迹，带之后 5/15/30 分钟的涨跌。per_min / slot_avg 来自 past_volume，算量比用。"""
    slot_avg = slot_avg or {}
    auction = t.loc[t['hms'] < '093000', 'dv'].sum()
    c = t[(t['hms'] >= '093000') & (t['hms'] < '145700')].copy()
    big_th = c['dv'].quantile(0.99)
    avg_day = c['dv'].sum() / max(c['dn'].sum(), 1)
    m = c['hms'].str[:2].astype(int) * 60 + c['hms'].str[2:4].astype(int)
    c['slot'] = (m // 5 * 5).map(lambda x: '%02d%02d' % (x // 60, x % 60))
    rows = []
    cum_vol, cum_out, cum_in = auction, 0.0, 0.0
    for s, x in c.groupby('slot'):
        vol = x['dv'].sum()
        if vol <= 0:
            continue
        buy, sell = x.loc[x['side'] == 1, 'dv'].sum(), x.loc[x['side'] == -1, 'dv'].sum()
        cum_vol += vol
        cum_out += buy
        cum_in += sell
        m = int(s[:2]) * 60 + int(s[2:]) + 5                      # 这个 5 分钟的结束时刻，跟 5 分钟线的时间戳对齐
        end = '%02d%02d' % (m // 60, m % 60)
        if end == '1135':
            end = '1130'
        bx = x[x['dv'] >= big_th]
        ret = (x['lastPrice'].iloc[-1] / x['lastPrice'].iloc[0] - 1) * 100
        nf = (buy - sell) / vol
        rows.append({'时段': s, '收盘价': x['lastPrice'].iloc[-1], '成交量': vol, '涨跌%': ret,
                     '外盘': buy, '内盘': sell, '外内比': buy / sell if sell > 0 else np.nan,
                     '累计外内比': cum_out / cum_in if cum_in > 0 else np.nan,
                     '量比': cum_vol / (per_min * minutes_open(end)) if per_min else np.nan,
                     '5分钟量比': vol / slot_avg[end] if slot_avg.get(end) else np.nan,
                     '净主动量': nf,
                     '大单净主动量': (bx.loc[bx['side'] == 1, 'dv'].sum() - bx.loc[bx['side'] == -1, 'dv'].sum()) / vol,
                     '挂单失衡': x['imb'].mean(),
                     '每笔手数比': (vol / max(x['dn'].sum(), 1)) / avg_day,
                     '吸收': -np.sign(nf) * np.sign(ret) * min(abs(nf), 1) if ret != 0 and nf != 0 else 0.0})
    b = pd.DataFrame(rows)
    if not len(b):
        return b
    b['近15分净主动量'] = b['净主动量'].rolling(3, min_periods=1).mean()
    px = b['收盘价']
    for n, k in ((1, '后5分%'), (3, '后15分%'), (6, '后30分%')):
        b[k] = (px.shift(-n) / px - 1) * 100
    return b


def day_summary(day, code, t, b):
    total = t['dv'].sum()
    c = t[(t['hms'] >= '093000') & (t['hms'] <= '145700')]
    buy, sell = c.loc[c['side'] == 1, 'dv'].sum(), c.loc[c['side'] == -1, 'dv'].sum()
    big = c[c['dv'] >= c['dv'].quantile(0.99)]
    o = t.loc[t['open'] > 0, 'open'].iloc[0]
    close = t['lastPrice'].iloc[-1]
    print('\n== %s %s 开 %.2f 收 %.2f（开→收 %+.2f%%），全天 %.0f 手' % (day, code, o, close, (close / o - 1) * 100, total))
    print('   集合竞价：开盘 %.1f%%，收盘 %.1f%%；连续竞价外盘 %.0f / 内盘 %.0f 手，外内比 %.2f，平均每笔 %.1f 手' % (
        t.loc[t['hms'] < '093000', 'dv'].sum() / total * 100, t.loc[t['hms'] > '145700', 'dv'].sum() / total * 100,
        buy, sell, buy / max(sell, 1), c['dv'].sum() / max(c['dn'].sum(), 1)))
    if len(b) and b['量比'].notna().any():
        pick = b[b['时段'].isin(['0955', '1025', '1125', '1355', '1450'])]
        ends = {'0955': '10:00', '1025': '10:30', '1125': '11:30', '1355': '14:00', '1450': '14:55'}
        print('   到某时刻为止的量比 / 累计外内比：' + '  '.join('到%s 量比%.2f 外内比%.2f' % (
            ends[r['时段']], r['量比'], r['累计外内比']) for _, r in pick.iterrows()))
        hot = b[b['5分钟量比'] >= 3]
        if len(hot):
            print('   突然放量（5 分钟量比 ≥3，时间是这 5 分钟的开始）：' + '，'.join('%s:%s起 %.1f倍 外内比%.2f 价%+.2f%%' % (
                r['时段'][:2], r['时段'][2:], r['5分钟量比'], r['外内比'] if r['外内比'] == r['外内比'] else 0, r['涨跌%'])
                for _, r in hot.iterrows()))
    print('   大单（最大 1%% 的 3 秒快照，%d 个）：主动买 %.0f / 主动卖 %.0f 手；最大几笔：%s' % (
        len(big), big.loc[big['side'] == 1, 'dv'].sum(), big.loc[big['side'] == -1, 'dv'].sum(),
        '，'.join('%s:%s %s%.0f@%.2f' % (r.hms[:2], r.hms[2:4], {1: '买', -1: '卖'}.get(r.side, '?'), r.dv, r.lastPrice)
                 for r in big.sort_values('dv', ascending=False).head(5).itertuples())))
    if len(b):
        ab = b[b['吸收'] > 0.15]
        if len(ab):
            print('   背离（主动方向和价格反着走）：' + '，'.join('%s:%s 主动%s%.0f%%但价%+.2f%%' % (
                r['时段'][:2], r['时段'][2:], '买' if r['净主动量'] > 0 else '卖', abs(r['净主动量']) * 100, r['涨跌%'])
                for _, r in ab.iterrows()))


def record(day, code):
    t = load(day, code)
    if t is None:
        return None
    per_min, slot_avg = past_volume(code, day)
    b = bars(t, per_min, slot_avg)
    day_summary(day, code, t, b)
    if len(b):
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, '%s.csv' % code)
        b.insert(0, '日期', day)
        if os.path.exists(path):
            old = pd.read_csv(path, dtype={'日期': str, '时段': str}, encoding='utf-8-sig')
            b = pd.concat([old[old['日期'] != day], b])
        b.sort_values(['日期', '时段']).to_csv(path, index=False, encoding='utf-8-sig')
    return b


def run(day=None, codes=None):
    if not day:
        ds = sorted(x for x in os.listdir(TICK_DIR) if x.isdigit())
        day = ds[-1]
    for c in codes or CODES:
        record(day, c)


def study(codes):
    pd.set_option('display.unicode.east_asian_width', True)
    pd.set_option('display.width', 200)
    for code in codes:
        path = os.path.join(OUT_DIR, '%s.csv' % code)
        if not os.path.exists(path):
            continue
        x = pd.read_csv(path, dtype={'日期': str, '时段': str}, encoding='utf-8-sig')
        nd = x['日期'].nunique()
        print('\n== %s 累计 %d 天、%d 个 5 分钟%s' % (code, nd, len(x), '（少于 20 天，只当参考）' if nd < 20 else ''))
        rows = []
        for f in FEATS:
            for tgt in ('后5分%', '后15分%', '后30分%'):
                per = x.dropna(subset=[f, tgt]).groupby('日期').apply(
                    lambda z: z[f].rank().corr(z[tgt].rank()) if len(z) > 10 else np.nan).dropna()
                if not len(per):
                    continue
                m = per.mean()
                rows.append({'足迹': f, '预测': tgt, 'IC均值': m,
                             't': m / per.std() * np.sqrt(len(per)) if len(per) > 2 and per.std() > 0 else np.nan,
                             '同号天数': '%d/%d' % ((np.sign(per) == np.sign(m)).sum(), len(per))})
        print(pd.DataFrame(rows).round(3).to_string(index=False))


def main():
    ap = argparse.ArgumentParser(description='资金足迹（订单流）')
    ap.add_argument('day', nargs='?')
    ap.add_argument('codes', nargs='*', default=[])
    ap.add_argument('--study', action='store_true')
    a = ap.parse_args()
    codes = [code_of(c) for c in a.codes] or CODES
    if a.study:
        study(codes)
    else:
        run(a.day, codes)


if __name__ == '__main__':
    main()
