# coding:utf-8
"""趋势日过滤研究：实盘正T（价格 ≥ VWAP×(1+带宽) 先卖，回 VWAP 买回）触发的那一刻，哪些已知信息能分出
「之后会回到均价」（震荡日，赚）和「一路走高回不来」（趋势日，亏）。

    .venv313\\Scripts\\python.exe tools\\trend_filter.py               # 默认：持仓 3 只 + 80 只同类股票
    .venv313\\Scripts\\python.exe tools\\trend_filter.py --peers 0     # 只看持仓（不联网取同类股票）
    .venv313\\Scripts\\python.exe tools\\trend_filter.py --peers 150   # 同类股票多取一些

只在 .venv313 跑（用了 3.7+ 写法）。不下单。

数据（都是 5 分钟线，近似 tick）：
  * 600436：QMT 本地 5 分钟线 2023-05 起 + 新浪补近两个月 → 样本 A（长时间、单只）。
  * 600366、605208 和「同类股票」：新浪 5 分钟线，只有近两个月（缓存在 day_pattern/sina5m/，越跑越长）→ 样本 B（短时间、多只）。
    同类股票从本地日线里挑：沪深主板，近 20 日日均成交额 1~6 亿、股价 5~60 元、近 20 日平均振幅 2~6%，固定种子抽样，
    名单存在 trend_filter/peers.txt，以后跑沿用同一批（新浪缓存才能越攒越长）。
  * 指数用沪深300 5 分钟线（QMT 本地 + 新浪）。中证1000、上证50 本地没有 5 分钟线，只有新浪近两个月
    （缓存同样在 day_pattern/sina5m/），所以这两个指数的特征只在样本 B 上有值，单独列一节看。

事件：每只每天第一次触发（09:35 后、14:30 前，某根 5 分钟线最高价 ≥ 当根 VWAP×(1+带宽)），卖出价按触发价（开盘就在上方按开盘价）。
带宽跟实盘一样：前 20 个交易日平均振幅 × band_k，夹在 [band_min, band_max]（参数读 sim_t0/params.json）。
结果：之后到 14:55 前有没有回到 VWAP（回到 = 赢）；净收益 %（扣 0.17% 来回费用，约 8000 元一笔的最低佣金 + 印花税）：
  有止损（比卖出价再涨一个带宽买回）和无止损（14:55 按收盘价买回）两种。

特征（只用触发那根之前已经走完的 K 线，不偷看）：
  时刻（开盘后几分钟）、开盘缺口、从开盘涨了多少、离昨天最高价多远（正 = 已突破）、今天已走振幅 ÷ 平时振幅、
  量比（到上一根的累计量 ÷ 前 5 天同一时刻的累计量）、前 15 分钟涨跌、VWAP 前 30 分钟的斜率（带宽为单位）、
  沪深300 从开盘到此刻的涨跌、相对强度（个股当天涨跌 − 沪深300）、昨天涨跌、昨收相对 20 日均线；
  2026-10-23 加：触发前 15 分钟沪深300 / 中证1000 涨跌（这次冲高是不是大盘带的）、个股前 15 分钟 − 中证1000
  前 15 分钟（独立拉升的部分）、中证1000 开盘到此刻、大小盘差（上证50 − 中证1000，开盘到此刻）。

检验：每个特征按样本 A 的五分位切档，看 A 和 B 各档的胜率和平均净收益；两边同方向才算数。
候选过滤 =「这个特征落在最差一档就不卖」，在 A 上挑、在 B 上验（B 跟 A 时间、股票都不重叠）。
结果写 trend_filter/events.csv（逐事件特征和结果，可以自己再切）。
"""
import argparse
import os
import random
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from t0_sim import DATA_DIR, load_params  # noqa: E402

OUT_DIR = os.path.join(DATA_DIR, 'trend_filter')
HOLDINGS = ['600366.SH', '605208.SH', '600436.SH']
LONG = '600436.SH'                 # 有 QMT 本地长历史的那只 = 样本 A
COST = 0.17                        # 来回费用 %
DATADIR = r'D:\国金证券QMT交易端\datadir'

FEATS = [
    ('分钟', '开盘后第几分钟触发'),
    ('缺口%', '开盘缺口'),
    ('开盘起%', '触发价相对开盘'),
    ('距昨高%', '触发价相对昨天最高价（正 = 已突破）'),
    ('已走振幅', '今天已走的振幅 ÷ 平时振幅'),
    ('量比', '累计量 ÷ 前 5 天同时刻'),
    ('前15分%', '触发前 15 分钟涨跌'),
    ('均价斜率', 'VWAP 前 30 分钟变化（带宽为单位）'),
    ('指数%', '沪深300 开盘到此刻'),
    ('相对强度%', '个股当天涨跌 − 沪深300'),
    ('昨涨跌%', '昨天涨跌'),
    ('距20线%', '昨收相对 20 日均线'),
    ('指数前15分%', '触发前 15 分钟沪深300 涨跌'),
]
# 只有新浪近两个月数据的指数特征：只在样本 B 上有值
FEATS_B = [
    ('中证1000%', '中证1000 开盘到此刻'),
    ('中证1000前15分%', '触发前 15 分钟中证1000 涨跌'),
    ('独立上冲%', '个股前 15 分钟 − 中证1000 前 15 分钟'),
    ('大小盘差%', '上证50 − 中证1000（开盘到此刻）'),
]
INDEXES = {'沪深300': '000300.SH', '中证1000': '000852.SH', '上证50': '000016.SH'}


# ---------------------------------------------------------------- 数据

def pick_peers(n, seed=20261022):
    """从本地日线挑同类股票，名单落盘，以后沿用。"""
    path = os.path.join(OUT_DIR, 'peers.txt')
    old = []
    if os.path.exists(path):
        with open(path, encoding='utf-8-sig') as f:
            old = [x.strip() for x in f if x.strip() and not x.startswith('#')]
    if len(old) >= n:
        return old[:n]
    from qmt_bridge.dat_reader import read_daily
    cands = []
    for mkt, prefixes in (('SH', ('600', '601', '603', '605')), ('SZ', ('000', '001', '002'))):
        for fn in os.listdir(os.path.join(DATADIR, mkt, '86400')):
            if fn.endswith('.DAT') and fn.startswith(prefixes):
                cands.append('%s.%s' % (fn[:6], mkt))
    random.Random(seed).shuffle(cands)
    picked = [c for c in old]
    print('从 %d 只主板股票里挑同类股票……' % len(cands))
    for c in cands:
        if len(picked) >= n:
            break
        if c in picked or c in HOLDINGS:
            continue
        try:
            d, _ = read_daily(c)
        except Exception:
            continue
        d = d.tail(21)
        if len(d) < 21 or d.index[-1] < '20261001':
            continue
        amt = d['amount'].tail(20).mean()
        px = d['close'].iloc[-1]
        amp = ((d['high'] - d['low']) / d['preClose']).tail(20).mean()
        if 1e8 <= amt <= 6e8 and 5 <= px <= 60 and 0.02 <= amp <= 0.06:
            picked.append(c)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(path, 'w', encoding='utf-8-sig') as f:
        f.write('# trend_filter.py 的同类股票名单（固定，以后沿用；要换就删掉这个文件）\n')
        f.write('\n'.join(picked) + '\n')
    return picked


def load_daily(code):
    from qmt_bridge.dat_reader import read_daily
    d, _ = read_daily(code)
    d = d[d['volume'] > 0].copy()
    d['amp'] = (d['high'] - d['low']) / d['preClose']
    d['amp20'] = d['amp'].rolling(20, min_periods=10).mean().shift(1)     # 前 20 天，不含当天
    d['ma20'] = d['close'].rolling(20).mean()
    d['yhigh'] = d['high'].shift(1)
    d['yret'] = (d['close'] / d['preClose'] - 1).shift(1) * 100
    d['y_ma20'] = (d['close'] / d['ma20'] - 1).shift(1) * 100
    return d


def load_5m(code, use_sina):
    from factor_model import bars_5m
    from day_pattern import load_bars
    if code == LONG or not use_sina:
        df = bars_5m(code) if use_sina else None
        if df is None:
            from qmt_bridge.dat_reader import read_bars
            try:
                df = read_bars(code, '5m')
            except FileNotFoundError:
                df = load_bars(code, use_sina=False)
        return df
    df = load_bars(code, use_sina=True)
    if df is None:
        return None
    cnt = df.groupby(df.index.str[:8]).size()
    return df[df.index.str[:8].isin(cnt[cnt >= 48].index)]


def index_bars(use_sina):
    """{名字: {日期: 按 hm 索引的 5 分钟线}}。沪深300 有 QMT 本地长历史，另外两个只有新浪。"""
    from factor_model import index_5m
    from day_pattern import load_bars
    out = {}
    for name, code in INDEXES.items():
        df = index_5m() if code == '000300.SH' else load_bars(code, use_sina=use_sina)
        if df is None or not len(df):
            print('  %s 5 分钟线没拿到，相关特征留空' % name)
            continue
        df = df.copy()
        df['day'] = df.index.str[:8]
        df['hm'] = df.index.str[8:12]
        out[name] = {d: g.set_index('hm') for d, g in df.groupby('day')}
        print('  %s 5 分钟线 %d 天（%s ~ %s）' % (name, len(out[name]), min(out[name]), max(out[name])))
    return out


def index_move(idx, name, day, hm_prev, back=None):
    """指数从开盘（back=None）或往前 back 根到 hm_prev 收盘的涨跌 %；没数据返回 nan。"""
    ix = idx.get(name, {}).get(day)
    if ix is None or hm_prev not in ix.index:
        return np.nan
    k = ix.index.get_loc(hm_prev)
    base = ix['open'].iloc[0] if back is None else ix['close'].iloc[max(0, k - back)]
    return (ix['close'].iloc[k] / base - 1) * 100


# ---------------------------------------------------------------- 事件

def events_for(code, bars, daily, idx, p):
    """每天第一次触发：特征 + 结果。"""
    bars = bars[['open', 'high', 'low', 'close', 'volume', 'amount']].astype(float).copy()
    bars['day'] = bars.index.str[:8]
    bars['hm'] = bars.index.str[8:12]
    # 成交量单位：QMT/新浪都换成手了，VWAP = 额 / 量 / 100；万一不是就自动认
    scale = 100.0
    test = (bars['amount'] / bars['volume'] / bars['close']).replace([np.inf, -np.inf], np.nan).median()
    if not 50 < test < 200:
        scale = 1.0
    cum_by_day = {}                                    # 每天每个时刻的累计量，算量比用
    out = []
    days = sorted(bars['day'].unique())
    for di, day in enumerate(days):
        g = bars[bars['day'] == day].sort_values('hm')
        if len(g) < 45:
            continue
        cum = g.set_index('hm')['volume'].cumsum()
        cum_by_day[day] = cum
        if day not in daily.index:
            continue
        dd = daily.loc[day]
        if pd.isna(dd['amp20']) or pd.isna(dd['yhigh']):
            continue
        band = min(max(dd['amp20'] * p['band_k'], p['band_min']), p['band_max'])
        pre = dd['preClose']
        o, h, l, c = g['open'].values, g['high'].values, g['low'].values, g['close'].values
        hm = g['hm'].values
        vwap = (g['amount'].cumsum() / g['volume'].cumsum() / scale).values
        if not 0.8 < np.nanmedian(vwap / c) < 1.2:
            continue
        hit = None
        for i in range(1, len(g)):
            if '0935' < hm[i] <= '1430' and h[i] >= vwap[i] * (1 + band):
                hit = i
                break
        if hit is None:
            continue
        i = hit
        entry = max(vwap[i] * (1 + band), o[i])
        # 结果
        back, ex_stop, ex_free, mfe = False, None, None, 0.0
        for j in range(i + 1, len(g)):
            if hm[j] > '1455':
                break
            mfe = max(mfe, h[j] / entry - 1)
            if l[j] <= vwap[j]:
                back = True
                ex_free = ex_free if ex_free is not None else vwap[j]
                ex_stop = ex_stop if ex_stop is not None else vwap[j]
                break
            if ex_stop is None and h[j] >= entry * (1 + band):
                ex_stop = entry * (1 + band)
        last = c[hm <= '1455'][-1]
        ex_free = ex_free if ex_free is not None else last
        ex_stop = ex_stop if ex_stop is not None else last
        # 特征：只用 i 之前走完的 K 线
        prev = slice(0, i)
        hm_prev = hm[i - 1]
        past = [cum_by_day[x] for x in days[max(0, di - 5):di] if x in cum_by_day]
        past_cum = [s.get(hm_prev) for s in past if hm_prev in s.index]
        rvol = cum.loc[hm_prev] / np.mean(past_cum) if len(past_cum) >= 3 and np.mean(past_cum) > 0 else np.nan
        k30 = max(0, i - 6)
        idx_ret = index_move(idx, '沪深300', day, hm_prev)
        z1000 = index_move(idx, '中证1000', day, hm_prev)
        z1000_15 = index_move(idx, '中证1000', day, hm_prev, back=3)
        sz50 = index_move(idx, '上证50', day, hm_prev)
        day_ret = (c[i - 1] / pre - 1) * 100
        r15 = (c[i - 1] / c[max(0, i - 4)] - 1) * 100 if i >= 2 else np.nan
        out.append({
            '代码': code, '日期': day, '时刻': hm[i], '带宽%': band * 100, '卖出价': entry,
            '分钟': (int(hm[i][:2]) * 60 + int(hm[i][2:]) - 570) - (90 if hm[i] > '1300' else 0),
            '缺口%': (o[0] / pre - 1) * 100,
            '开盘起%': (entry / o[0] - 1) * 100,
            '距昨高%': (entry / dd['yhigh'] - 1) * 100,
            '已走振幅': (h[prev].max() - l[prev].min()) / pre / dd['amp20'],
            '量比': rvol,
            '前15分%': r15,
            '均价斜率': (vwap[i - 1] / vwap[k30] - 1) / band,
            '指数%': idx_ret,
            '相对强度%': day_ret - idx_ret if not np.isnan(idx_ret) else np.nan,
            '昨涨跌%': dd['yret'],
            '距20线%': dd['y_ma20'],
            '指数前15分%': index_move(idx, '沪深300', day, hm_prev, back=3),
            '中证1000%': z1000,
            '中证1000前15分%': z1000_15,
            '独立上冲%': r15 - z1000_15,
            '大小盘差%': sz50 - z1000,
            '回均价': back,
            '净%_止损': (entry / ex_stop - 1) * 100 - COST,
            '净%_无止损': (entry / ex_free - 1) * 100 - COST,
            '继续上冲带宽': mfe / band,
        })
    return out


# ---------------------------------------------------------------- 检验

def bucket_table(a, b, feat):
    x = a[feat].dropna()
    if len(x) < 30:
        return None
    edges = np.unique(np.quantile(x, [0, .2, .4, .6, .8, 1]))
    if len(edges) < 4:
        return None
    edges[0], edges[-1] = -np.inf, np.inf
    rows = []
    for name, s in (('A', a), ('B', b)):
        cut = pd.cut(s[feat], edges)
        grp = s.groupby(cut, observed=False)
        rows.append(pd.DataFrame({
            name + '数': grp.size(), name + '胜率%': grp['回均价'].mean() * 100,
            name + '净%': grp['净%_止损'].mean()}))
    t = pd.concat(rows, axis=1)
    t.index = ['≤%.2f' % e if np.isfinite(e) else '其余' for e in edges[1:]]
    return t, edges


def corr(s, feat, y):
    s = s[[feat, y]].dropna()
    return s[feat].rank().corr(s[y].rank()) if len(s) > 20 else np.nan


def stats(s, label):
    if not len(s):
        return '%s：0 次' % label
    return '%s：%d 次，回均价 %.0f%%，净 有止损 %+.3f%% / 无止损 %+.3f%%' % (
        label, len(s), s['回均价'].mean() * 100, s['净%_止损'].mean(), s['净%_无止损'].mean())


def report(ev):
    a = ev[(ev['代码'] == LONG) & (ev['样本'] == 'A')]
    b = ev[ev['样本'] == 'B']
    print('\n' + '=' * 100)
    print('样本 A（%s 长历史）%s ~ %s；样本 B（持仓 + 同类股票近两个月）%d 只 %s ~ %s' % (
        LONG, a['日期'].min(), a['日期'].max(), b['代码'].nunique(), b['日期'].min(), b['日期'].max()))
    print(stats(a, '  A 全部'))
    print(stats(b, '  B 全部'))
    for c in HOLDINGS:
        print(stats(ev[ev['代码'] == c], '  %s' % c))

    print('\n---- 单个特征（秩相关：正 = 特征越大越容易回均价 / 净收益越高）')
    summary = []
    for feat, desc in FEATS:
        ca, cb = corr(a, feat, '回均价'), corr(b, feat, '回均价')
        pa, pb = corr(a, feat, '净%_止损'), corr(b, feat, '净%_止损')
        same = np.sign(ca) == np.sign(cb) and np.sign(pa) == np.sign(pb) and np.sign(ca) == np.sign(pa)
        summary.append((feat, desc, ca, cb, pa, pb, same))
        print('  %-8s %-26s 胜率 A %+.2f B %+.2f｜净收益 A %+.2f B %+.2f %s' % (
            feat, desc, ca, cb, pa, pb, '← 两边同向' if same else ''))

    print('\n---- 同向特征的分档（按 A 的五分位切）')
    filters = []
    for feat, desc, ca, cb, pa, pb, same in summary:
        if not same or min(abs(ca), abs(cb)) < 0.05:
            continue
        r = bucket_table(a, b, feat)
        if r is None:
            continue
        t, edges = r
        print('\n%s（%s）' % (feat, desc))
        print(t.to_string(float_format=lambda v: '%.1f' % v if abs(v) >= 1 else '%+.3f' % v))
        # 候选过滤：A 上净收益最差的一档不做（只考虑两端）
        ends = [0, len(t) - 1]
        worst = min(ends, key=lambda k: t['A净%'].iloc[k])
        lo, hi = edges[worst], edges[worst + 1]
        filters.append((feat, worst, lo, hi))

    print('\n---- 候选过滤：落在 A 上最差那一端的一档就不卖（在 A 挑，B 验）')
    for feat, worst, lo, hi in filters:
        def keep(s):
            v = s[feat]
            return s[~((v > lo) & (v <= hi))]
        rng = ('%s ≤ %.2f' % (feat, hi)) if worst == 0 else ('%s > %.2f' % (feat, lo))
        ka, kb = keep(a), keep(b)
        print('  跳过 %-18s A：%+.3f%% → %+.3f%%（剩 %d/%d）｜B：%+.3f%% → %+.3f%%（剩 %d/%d）｜B 胜率 %.0f%% → %.0f%%' % (
            rng, a['净%_止损'].mean(), ka['净%_止损'].mean(), len(ka), len(a),
            b['净%_止损'].mean(), kb['净%_止损'].mean(), len(kb), len(b),
            b['回均价'].mean() * 100, kb['回均价'].mean() * 100))

    report_b_only(b)


def tstat(x):
    x = x.dropna()
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x))) if len(x) > 2 and x.std(ddof=1) > 0 else np.nan


def report_b_only(b):
    """只有新浪数据的指数特征：没法 A 挑 B 验，只能在 B 内部按三分位看，样本外要靠以后的 tick 存档。"""
    print('\n---- 只在样本 B 有值的指数特征（按 B 自己的三分位切；没有独立样本验证，只当线索）')
    for feat, desc in FEATS_B + [('指数前15分%', '触发前 15 分钟沪深300 涨跌')]:
        s = b.dropna(subset=[feat])
        if len(s) < 60:
            print('  %s：只有 %d 次有值，跳过' % (feat, len(s)))
            continue
        print('\n%s（%s），%d 次，秩相关 胜率 %+.2f 净收益 %+.2f' % (
            feat, desc, len(s), corr(s, feat, '回均价'), corr(s, feat, '净%_止损')))
        cut = pd.qcut(s[feat], 3, labels=['低', '中', '高'])
        rows = []
        for (lab, g), am in [((k, s[cut == k]), False) for k in ['低', '中', '高']] + \
                [((k, s[(cut == k) & (s['时刻'] < '1130')]), True) for k in ['低', '中', '高']]:
            # 逐只：这一档里平均净收益为正的股票占比（至少 3 次的股票才算），看是不是少数几只撑起来的
            per = g.groupby('代码')['净%_止损'].agg(['mean', 'size'])
            per = per[per['size'] >= 3]
            rows.append({'时段': '上午' if am else '全天', '档': lab,
                         '范围': '%.2f~%.2f' % (g[feat].min(), g[feat].max()) if len(g) else '',
                         '次数': len(g), '回均价%': g['回均价'].mean() * 100, '净%': g['净%_止损'].mean(),
                         't': tstat(g['净%_止损']), '为正股票%': (per['mean'] > 0).mean() * 100 if len(per) else np.nan})
        print(pd.DataFrame(rows).to_string(index=False, float_format=lambda v: '%.2f' % v))


def main():
    ap = argparse.ArgumentParser(description='趋势日过滤研究（正T 先卖什么时候不该卖）')
    ap.add_argument('--peers', type=int, default=80, help='同类股票几只（0 = 只看持仓）')
    ap.add_argument('--no-sina', action='store_true', help='不联网，只用缓存')
    a = ap.parse_args()
    p = load_params()
    os.makedirs(OUT_DIR, exist_ok=True)
    use_sina = not a.no_sina
    idx = index_bars(use_sina)
    peers = pick_peers(a.peers) if a.peers else []
    codes = HOLDINGS + peers
    rows = []
    for n, code in enumerate(codes):
        try:
            bars = load_5m(code, use_sina)
            daily = load_daily(code)
        except Exception as e:
            print('  %s 跳过：%s' % (code, str(e)[:80]))
            continue
        if bars is None or not len(bars):
            continue
        ev = events_for(code, bars, daily, idx, p)
        rows += ev
        if n < len(HOLDINGS) or (n + 1) % 20 == 0:
            print('  %d/%d %s：%d 天 5 分钟线，%d 次触发' % (n + 1, len(codes), code, bars.index.str[:8].nunique(), len(ev)))
    ev = pd.DataFrame(rows)
    # 样本 A = 600436 新浪窗口之前；B = 新浪窗口（所有股票共同覆盖的近两个月）
    b_start = ev[ev['代码'] != LONG]['日期'].min()
    ev['样本'] = np.where(ev['日期'] < b_start, 'A', 'B')
    ev.to_csv(os.path.join(OUT_DIR, 'events.csv'), index=False, encoding='utf-8-sig')
    report(ev)
    print('\n逐事件明细：%s' % os.path.join(OUT_DIR, 'events.csv'))


if __name__ == '__main__':
    main()
