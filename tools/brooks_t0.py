# coding:utf-8
"""布鲁克斯（Al Brooks）价格行为版做 T：5 分钟 K 线 + 20 EMA，先判市况再找信号。回测和模拟盘共用一套引擎。

    .venv313\\Scripts\\python.exe tools\\brooks_t0.py fetch                     # 从桥拉 5 分钟 K 线存到共享数据目录（增量）
    .venv313\\Scripts\\python.exe tools\\brooks_t0.py bt                        # 回测：布鲁克斯版 vs 现在的 VWAP 版
    .venv313\\Scripts\\python.exe tools\\brooks_t0.py bt --grid                 # 只用 2025 年挑参数，2026 年样本外检验
    .venv313\\Scripts\\python.exe tools\\brooks_t0.py sim                       # 模拟盘：盘中每根 5 分钟 K 线收完跑一次，虚拟成交
    .venv313\\Scripts\\python.exe tools\\brooks_t0.py report                    # 模拟盘累计战绩

**不下任何真实委托。**

规则（能写成代码的那一部分布鲁克斯，参数在 PARAMS）：
  * 市况（每根收完的 K 线判一次，用最近 ctx_n 根，跨日连续）：
      多头趋势 = 至少 trend_frac 的收盘在 EMA 上方，且 EMA 在 slope_n 根里升了 slope_atr 个 ATR 以上；空头对称；其余算震荡。
  * 信号（信号 K 线收完后，下一根挂突破单，只挂一根，没触发就撤）：
      空头趋势 -> L2 做正T（先卖）：反弹到 EMA 附近的第二次向下尝试，卖在信号 K 线低点下 1 分。
      多头趋势 -> H2 做反T（先买）：对称。
      震荡 -> 区间上沿 1/4 出现空头反转 K 线做正T，下沿出现多头反转 K 线做反T。
      多头趋势里不做正T，空头趋势里不做反T（不逆着「总在场方向」做）。
  * 止损放在信号 K 线另一头外 1 分；目标 = 入场 ± target_r × 风险。风险超过 max_risk_atr 个 ATR 不做；
    目标赚的钱不够 min_edge 倍来回费用也不做（第一天的教训：费用吃掉 64% 毛利）。
  * 每只每天最多一轮（两笔委托，跟实盘额度一致），entry_start ~ entry_end 才开，14:55 那根收盘强制平（回测要算清 T 的盈亏）。
  * 同一根 K 线里既碰止损又碰目标，按先止损算；入场那根只查止损不查目标（都往保守算）。

对照组（现在实盘的 VWAP 版，按 5 分钟 K 线近似）：价格 ≥ VWAP × (1 + 带宽) 先卖，回到 VWAP 买回，再涨一个带宽止损。

数据：共享数据目录 bars5m/<代码>.csv（fetch 生成）；结果写 brooks_t0/。只在 .venv313 跑（用了 3.7+ 写法）。
"""
import argparse
import datetime as dt
import itertools
import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from t0_sim import DATA_DIR, fee, load_params  # noqa: E402

BAR_DIR = os.path.join(DATA_DIR, 'bars5m')
OUT_DIR = os.path.join(DATA_DIR, 'brooks_t0')

CODES = {'600366.SH': 800, '605208.SH': 600, '600436.SH': 100}   # 代码 -> 每次股数（前两只跟实盘一致）

PARAMS = {
    'ema_n': 20,
    'atr_n': 20,
    'ctx_n': 10,            # 判市况看几根
    'trend_frac': 0.7,      # 至少这么多收盘在 EMA 同一侧
    'slope_n': 5,
    'slope_atr': 0.3,       # EMA 在 slope_n 根里至少走这么多个 ATR
    'pb_near_atr': 0.5,     # 回调要碰到 EMA ± 这么多 ATR 以内才算「回到均线」
    'range_n': 20,          # 震荡区间看几根
    'range_zone': 0.25,     # 区间上下沿各占多少
    'range_min_atr': 2.0,   # 区间至少这么宽，不然不做
    'use_trend': True,
    'use_range': True,
    'target_r': 1.0,
    'max_risk_atr': 1.5,
    'min_edge': 1.5,        # 目标盈利 ≥ 这么多倍来回费用
    'entry_start': '094500',
    'entry_end': '143000',
    'exit_time': '145500',  # 这根收盘强制平
}

TICK = 0.01


# ---------------------------------------------------------------- 数据

def fetch(codes, start='20250101'):
    from qmt_bridge import xtdata
    xtdata.enable_hello = False
    xtdata.connect()
    os.makedirs(BAR_DIR, exist_ok=True)
    for c in codes:
        # 每次全量重拉（一只一次调用，很快）。QMT 本地没下过分钟线的日子，桥给的是全 0 占位行 —— 丢掉，
        # 在大QMT「数据管理」里补了 5 分钟线再 fetch 一次就齐了。
        path = os.path.join(BAR_DIR, '%s.csv' % c)
        d = xtdata.get_market_data_ex(['open', 'high', 'low', 'close', 'volume', 'amount'], [c], period='5m',
                                      start_time=start, end_time=dt.date.today().strftime('%Y%m%d'))
        df = d.get(c)
        if df is None or not len(df):
            print('%s 没拿到数据' % c)
            continue
        df.index = [str(i)[:14] for i in df.index]
        df = df[['open', 'high', 'low', 'close', 'volume', 'amount']]
        n_all = len(df)
        df = df[(df['low'] > 0) & (df['high'] >= df['low'])]
        df.index.name = 'time'
        df.to_csv(path, encoding='utf-8-sig')
        days = len(set(i[:8] for i in df.index))
        print('%s 有效 %d 根（%d 天），%s ~ %s%s' % (
            c, len(df), days, df.index[0] if len(df) else '-', df.index[-1] if len(df) else '-',
            '；另有 %d 根是 0（QMT 本地没下这段 5 分钟线，去「数据管理」补）' % (n_all - len(df)) if n_all > len(df) else ''))


def load_bars(code):
    df = pd.read_csv(os.path.join(BAR_DIR, '%s.csv' % code), dtype={'time': str}, encoding='utf-8-sig')
    df = df.set_index('time').sort_index()
    df = df[(df['high'] >= df['low']) & (df['low'] > 0)]
    return df


def prepare(df, p):
    """加 EMA、ATR、日期、时刻、当日 VWAP、对照组带宽。全部只用当根及以前的数据。"""
    df = df.copy()
    df['day'] = [i[:8] for i in df.index]
    df['hm'] = [i[8:14] for i in df.index]
    df['ema'] = df['close'].ewm(span=p['ema_n'], adjust=False).mean()
    df['atr'] = (df['high'] - df['low']).rolling(p['atr_n'], min_periods=5).mean()
    ratio = (df['amount'] / df['volume'].replace(0, np.nan) / df['close']).median()
    unit = 100 if ratio > 50 else 1
    cum_amt = df.groupby('day')['amount'].cumsum()
    cum_vol = df.groupby('day')['volume'].cumsum() * unit
    df['vwap'] = cum_amt / cum_vol.replace(0, np.nan)
    daily = df.groupby('day').agg(h=('high', 'max'), l=('low', 'min'), c=('close', 'last'))
    amp = ((daily['h'] - daily['l']) / daily['c'].shift(1)).rolling(20, min_periods=5).mean().shift(1)
    sim = load_params()
    band = (amp * sim['band_k']).clip(sim['band_min'], sim['band_max'])
    df['band'] = df['day'].map(band)
    return df


# ---------------------------------------------------------------- 市况和信号

def context(df, i, p):
    """'bull' / 'bear' / 'range'，只用 i 及以前。"""
    n = p['ctx_n']
    if i < max(n, p['slope_n']) or np.isnan(df['atr'].iat[i]):
        return 'range'
    c = df['close'].values[i - n + 1:i + 1]
    e = df['ema'].values[i - n + 1:i + 1]
    above = (c > e).mean()
    atr = df['atr'].iat[i]
    slope = (df['ema'].iat[i] - df['ema'].iat[i - p['slope_n']]) / atr
    if above >= p['trend_frac'] and slope >= p['slope_atr']:
        return 'bull'
    if above <= 1 - p['trend_frac'] and slope <= -p['slope_atr']:
        return 'bear'
    return 'range'


def _attempts(h, l, k, i, up):
    """回调里第几次尝试恢复趋势：从极点 k 之后，数「这根高点突破上一根高点、而上一根没突破再上一根」的次数（H 计数）；
    up=False 时数低点（L 计数）。"""
    n = 0
    for m in range(k + 2, i + 1):
        if up and h[m] > h[m - 1] and h[m - 1] <= h[m - 2]:
            n += 1
        if not up and l[m] < l[m - 1] and l[m - 1] >= l[m - 2]:
            n += 1
    return n


def signal(df, i, ctx, p):
    """K 线 i 收完后要不要挂单。返回 (side, setup, trigger, stop) 或 None。side: 'sell'=正T 先卖，'buy'=反T 先买。"""
    o, h, l, c = (df[k].values for k in ('open', 'high', 'low', 'close'))
    ema, atr = df['ema'].iat[i], df['atr'].iat[i]
    if np.isnan(atr) or atr <= 0:
        return None
    rng = h[i] - l[i]
    if rng <= 0:
        return None
    look = p['range_n']
    lo = max(0, i - look + 1)
    if p['use_trend'] and ctx == 'bear':
        k = lo + int(np.argmin(l[lo:i + 1]))                      # 这段下跌的低点
        if i - k >= 2 and h[k:i + 1].max() >= ema - p['pb_near_atr'] * atr:   # 反弹碰到 EMA 附近
            # 信号 K 线 i：收在下半段的阴线；之前已经有过一次向下尝试（L1），i 之后挂的是 L2
            if c[i] < o[i] and c[i] <= l[i] + 0.5 * rng and _attempts(h, l, k, i, up=False) == 1:
                return 'sell', 'L2', l[i] - TICK, h[i] + TICK
    if p['use_trend'] and ctx == 'bull':
        k = lo + int(np.argmax(h[lo:i + 1]))
        if i - k >= 2 and l[k:i + 1].min() <= ema + p['pb_near_atr'] * atr:
            if c[i] > o[i] and c[i] >= h[i] - 0.5 * rng and _attempts(h, l, k, i, up=True) == 1:
                return 'buy', 'H2', h[i] + TICK, l[i] - TICK
    if p['use_range'] and ctx == 'range':
        hi, lw = h[lo:i + 1].max(), l[lo:i + 1].min()
        w = hi - lw
        if w >= p['range_min_atr'] * atr:
            if h[i] >= hi - p['range_zone'] * w and c[i] < o[i] and c[i] <= l[i] + 0.5 * rng:
                return 'sell', '区间上沿', l[i] - TICK, h[i] + TICK
            if l[i] <= lw + p['range_zone'] * w and c[i] > o[i] and c[i] >= h[i] - 0.5 * rng:
                return 'buy', '区间下沿', h[i] + TICK, l[i] - TICK
    return None


# ---------------------------------------------------------------- 一天的模拟（回测和模拟盘共用）

def round_cost(p_fee, px, qty):
    return fee(p_fee, 'sell', px * qty) + fee(p_fee, 'buy', px * qty)


def run_day(df, idx, qty, p, p_fee, strategy='brooks'):
    """df 已 prepare；idx = 这一天各根在 df 里的位置（只含已收完的）。返回事件列表，最多一轮。"""
    events = []
    pend = pos = None
    done = False
    o, h, l, c, hm = (df[k].values for k in ('open', 'high', 'low', 'close', 'hm'))
    for i in idx:
        t = hm[i]
        # 1. 挂着的突破单：只活一根
        if pend is not None:
            side, setup, trig, stop, tgt, ctx, kind = pend
            pend = None
            if kind == 'stop':        # 突破单：卖在跌破、买在涨破，跳空按开盘价（更差）
                hit = (l[i] <= trig) if side == 'sell' else (h[i] >= trig)
                px = min(o[i], trig) if side == 'sell' else max(o[i], trig)
            else:                     # 限价单：卖在涨到、买在跌到，跳空按开盘价（更好）
                hit = (h[i] >= trig) if side == 'sell' else (l[i] <= trig)
                px = max(o[i], trig) if side == 'sell' else min(o[i], trig)
            if hit:
                pos = {'side': side, 'setup': setup, 'ctx': ctx, 'px': px, 'stop': stop, 'tgt': tgt,
                       'in': df.index[i], 'entry_bar': True}
                events.append(('open', df.index[i], side, px, setup, ctx))
        # 2. 持仓：止损 / 目标 / 收盘强平
        if pos is not None:
            side, reason, px = pos['side'], None, None
            if side == 'sell':
                if h[i] >= pos['stop']:
                    reason, px = '止损', max(o[i], pos['stop']) if not pos['entry_bar'] else pos['stop']
                elif not pos['entry_bar'] and pos['tgt'] is not None and l[i] <= pos['tgt']:
                    reason, px = '目标', min(o[i], pos['tgt'])
            else:
                if l[i] <= pos['stop']:
                    reason, px = '止损', min(o[i], pos['stop']) if not pos['entry_bar'] else pos['stop']
                elif not pos['entry_bar'] and pos['tgt'] is not None and h[i] >= pos['tgt']:
                    reason, px = '目标', max(o[i], pos['tgt'])
            if reason is None and strategy == 'vwap' and not pos['entry_bar']:
                v = df['vwap'].iat[i]
                if (side == 'sell' and l[i] <= v) or (side == 'buy' and h[i] >= v):
                    reason, px = '回均价', v
            if reason is None and t >= p['exit_time']:
                reason, px = '收盘平', c[i]
            pos['entry_bar'] = False
            if reason:
                sell_px, buy_px = (pos['px'], px) if side == 'sell' else (px, pos['px'])
                gross = (sell_px - buy_px) * qty
                cost = fee(p_fee, 'sell', sell_px * qty) + fee(p_fee, 'buy', buy_px * qty)
                events.append(('close', df.index[i], side, px, reason, gross, cost, gross - cost, pos))
                pos, done = None, True
            continue
        # 3. 空仓：看要不要挂单
        if done or pend is not None or t < p['entry_start'] or t > p['entry_end']:
            continue
        if strategy == 'brooks':
            ctx = context(df, i, p)
            s = signal(df, i, ctx, p)
            if s is None:
                continue
            side, setup, trig, stop = s
            risk = abs(stop - trig)
            atr = df['atr'].iat[i]
            if risk <= 0 or risk > p['max_risk_atr'] * atr:
                continue
            tgt = trig - p['target_r'] * risk if side == 'sell' else trig + p['target_r'] * risk
            if p['target_r'] * risk * qty < p['min_edge'] * round_cost(p_fee, trig, qty):
                continue
            pend = (side, setup, trig, stop, tgt, ctx, 'stop')
        else:                                       # 对照：VWAP 版，只做正T（跟实盘一致），在这根里直接成交
            v, b = df['vwap'].iat[i], df['band'].iat[i]
            if np.isnan(v) or np.isnan(b):
                continue
            # 下一根涨到 VWAP×(1+b) 就卖（用这根收完时的 VWAP 近似）
            trig = v * (1 + b)
            pend = ('sell', 'VWAP带宽', trig, trig * (1 + b), None, '-', 'limit')
    return events


def backtest(codes, p, strategy, start=None, end=None):
    p_fee = load_params()
    rows = []
    for code in codes:
        df = prepare(load_bars(code), p)
        qty = CODES.get(code, 100)
        for day, g in _days(df):
            if (start and day < start) or (end and day > end):
                continue
            for ev in run_day(df, g, qty, p, p_fee, strategy):
                if ev[0] == 'close':
                    pos = ev[8]
                    rows.append({'代码': code, '日期': day, '策略': strategy, '方向': '正T' if ev[2] == 'sell' else '反T',
                                 '形态': pos['setup'], '市况': pos['ctx'], '开仓': pos['in'][8:12], '开仓价': round(pos['px'], 3),
                                 '平仓': ev[1][8:12], '平仓价': round(ev[3], 3), '原因': ev[4], '数量': qty,
                                 '毛利': round(ev[5], 2), '费用': round(ev[6], 2), '净利': round(ev[7], 2)})
    return pd.DataFrame(rows)


def _days(df):
    pos = np.arange(len(df))
    for day, g in df.groupby('day', sort=True):
        yield day, pos[df.index.get_indexer(g.index)]


def summarize(t, title):
    if t is None or not len(t):
        print('%s：没有交易' % title)
        return
    eq = t.sort_values(['日期', '开仓'])['净利'].cumsum()
    dd = (eq - eq.cummax()).min()
    print('%s：%d 轮，胜率 %.0f%%，毛利 %+.0f，费用 %.0f，净利 %+.0f，平均每轮 %+.1f，最大回撤 %.0f' % (
        title, len(t), (t['净利'] > 0).mean() * 100, t['毛利'].sum(), t['费用'].sum(), t['净利'].sum(),
        t['净利'].mean(), dd))


def table(t, key):
    g = t.groupby(key)['净利'].agg(['count', 'sum', 'mean', lambda s: (s > 0).mean() * 100])
    g.columns = ['轮数', '净利', '平均', '胜率%']
    return g.round(1).to_string()


def cmd_bt(a):
    codes = a.codes or list(CODES)
    os.makedirs(OUT_DIR, exist_ok=True)
    if a.grid:
        grid = {'target_r': [1.0, 1.5, 2.0], 'use_range': [True, False], 'trend_frac': [0.6, 0.7, 0.8],
                'max_risk_atr': [1.0, 1.5]}
        res = []
        keys = list(grid)
        for vals in itertools.product(*grid.values()):
            p = dict(PARAMS, **dict(zip(keys, vals)))
            t = backtest(codes, p, 'brooks', end='20251231')
            res.append(dict(zip(keys, vals), 轮数=len(t), 净利=round(t['净利'].sum(), 0) if len(t) else 0,
                            胜率=round((t['净利'] > 0).mean() * 100, 0) if len(t) else 0))
        r = pd.DataFrame(res).sort_values('净利', ascending=False)
        print('== 只用 2025 年挑参数（前 10）==')
        print(r.head(10).to_string(index=False))
        best = dict(PARAMS, **{k: r.iloc[0][k] for k in keys})
        for k in keys:
            best[k] = type(PARAMS[k])(best[k])
        print('\n选中：%s' % {k: best[k] for k in keys})
        t26 = backtest(codes, best, 'brooks', start='20260101')
        b26 = backtest(codes, PARAMS, 'vwap', start='20260101')
        print('\n== 2026 样本外 ==')
        summarize(t26, '布鲁克斯（挑出来的参数）')
        summarize(backtest(codes, PARAMS, 'brooks', start='20260101'), '布鲁克斯（默认参数）')
        summarize(b26, 'VWAP 对照')
        if len(t26):
            print(table(t26, '形态'))
        r.to_csv(os.path.join(OUT_DIR, 'grid_2025.csv'), index=False, encoding='utf-8-sig')
        return
    tb = backtest(codes, PARAMS, 'brooks', a.start, a.end)
    tv = backtest(codes, PARAMS, 'vwap', a.start, a.end)
    days = pd.concat([prepare(load_bars(c), PARAMS)['day'] for c in codes]).nunique()
    print('== 回测 %s，%d 只，约 %d 个交易日，默认参数 ==' % ('、'.join(codes), len(codes), days))
    summarize(tb, '布鲁克斯')
    summarize(tv, 'VWAP 对照（只做正T）')
    if len(tb):
        tb['年'] = tb['日期'].str[:4]
        for key in ['年', '代码', '形态', '市况', '方向', '原因']:
            print('-- 布鲁克斯 按%s' % key)
            print(table(tb, key))
    if len(tv):
        tv['年'] = tv['日期'].str[:4]
        print('-- VWAP 对照 按年')
        print(table(tv, '年'))
        print('-- VWAP 对照 按原因')
        print(table(tv, '原因'))
    tb.to_csv(os.path.join(OUT_DIR, 'bt_brooks.csv'), index=False, encoding='utf-8-sig')
    tv.to_csv(os.path.join(OUT_DIR, 'bt_vwap.csv'), index=False, encoding='utf-8-sig')


# ---------------------------------------------------------------- 模拟盘

def cmd_sim(a):
    """盘中每 20 秒拉一次 5 分钟 K 线（带前几天，EMA 才连续），每多收完一根就把当天从头重算一遍，新事件打日志。
    重算是确定性的，所以中途重启也接得上，跟回测的结果逐根一致。"""
    from qmt_bridge import xtdata
    xtdata.enable_hello = False
    xtdata.connect()
    p, p_fee = PARAMS, load_params()
    codes = a.codes or [c for c in CODES if c != '600436.SH']
    day = dt.date.today().strftime('%Y%m%d')
    os.makedirs(OUT_DIR, exist_ok=True)
    logf = open(os.path.join(OUT_DIR, 'sim_log_%s.txt' % day), 'a', encoding='utf-8-sig')

    def log(msg):
        print(msg, flush=True)
        logf.write(msg + '\n')
        logf.flush()

    seen = {c: 0 for c in codes}
    final = {}
    log('== %s 布鲁克斯模拟盘开始：%s（虚拟成交，不下单）==' % (day, '、'.join(codes)))
    while True:
        now = dt.datetime.now().strftime('%H%M%S')
        if now >= (a.until or '150100'):
            break
        if '113100' < now < '130000' or now < '093000':
            time.sleep(20)
            continue
        for c in codes:
            try:
                d = xtdata.get_market_data_ex(['open', 'high', 'low', 'close', 'volume', 'amount'], [c], period='5m',
                                              count=400)
            except Exception as e:
                log('%s %s 取 K 线失败：%s' % (now, c, e))
                continue
            df = d.get(c)
            if df is None or not len(df):
                continue
            df.index = [str(i)[:14] for i in df.index]
            df = df[[x[:8] < day or x[8:14] <= now for x in df.index]]   # 去掉还没收完的那根
            df = prepare(df[(df['high'] >= df['low']) & (df['low'] > 0)], p)
            pos = np.arange(len(df))
            today = pos[(df['day'] == day).values]
            # 最后一根可能还没收完：K 线时间戳是结束时刻，结束时刻 > 现在的就是没收完
            today = [i for i in today if df['hm'].iat[i] <= now]
            if len(today) <= seen[c]:
                continue
            seen[c] = len(today)
            evs = run_day(df, today, CODES[c], p, p_fee, 'brooks')
            for ev in evs[final.get(c, 0):]:
                if ev[0] == 'open':
                    log('%s %s 开 %s @ %.3f（%s，市况 %s）' % (ev[1][8:12], c, '正T 先卖' if ev[2] == 'sell' else '反T 先买',
                                                         ev[3], ev[4], ev[5]))
                else:
                    log('%s %s 平 @ %.3f（%s）毛利 %+.0f 费用 %.0f 净 %+.0f' % (ev[1][8:12], c, ev[3], ev[4], ev[5], ev[6], ev[7]))
                    pos_ = ev[8]
                    row = pd.DataFrame([{'代码': c, '日期': day, '方向': '正T' if ev[2] == 'sell' else '反T',
                                         '形态': pos_['setup'], '市况': pos_['ctx'], '开仓': pos_['in'][8:12],
                                         '开仓价': round(pos_['px'], 3), '平仓': ev[1][8:12], '平仓价': round(ev[3], 3),
                                         '原因': ev[4], '数量': CODES[c], '毛利': round(ev[5], 2), '费用': round(ev[6], 2),
                                         '净利': round(ev[7], 2)}])
                    path = os.path.join(OUT_DIR, 'sim_trades.csv')
                    row.to_csv(path, mode='a', header=not os.path.exists(path), index=False, encoding='utf-8-sig')
            final[c] = len(evs)
        time.sleep(20)
    log('== %s 收工 ==' % day)
    logf.close()


def cmd_report(a):
    path = os.path.join(OUT_DIR, 'sim_trades.csv')
    if not os.path.exists(path):
        print('还没有模拟记录')
        return
    t = pd.read_csv(path, encoding='utf-8-sig', dtype={'日期': str})
    summarize(t, '布鲁克斯模拟盘累计（%d 天）' % t['日期'].nunique())
    for key in ['日期', '代码', '形态']:
        print('-- 按%s' % key)
        print(table(t, key))


def main():
    ap = argparse.ArgumentParser(description='布鲁克斯价格行为做 T：回测 / 模拟盘（不下单）')
    ap.add_argument('cmd', choices=['fetch', 'bt', 'sim', 'report'])
    ap.add_argument('codes', nargs='*', help='默认 %s' % ' '.join(CODES))
    ap.add_argument('--start', help='回测起始日 YYYYMMDD')
    ap.add_argument('--end', help='回测结束日 YYYYMMDD')
    ap.add_argument('--grid', action='store_true', help='2025 年挑参数、2026 年样本外检验')
    ap.add_argument('--until', help='模拟盘几点收工，HHMMSS，默认 150100')
    a = ap.parse_args()
    if a.cmd == 'fetch':
        fetch(a.codes or list(CODES))
    elif a.cmd == 'bt':
        cmd_bt(a)
    elif a.cmd == 'sim':
        cmd_sim(a)
    else:
        cmd_report(a)


if __name__ == '__main__':
    main()
