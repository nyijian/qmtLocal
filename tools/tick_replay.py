# coding:utf-8
"""用存下来的 tick（tools/tick_archive.py）回放讨论过的做 T 策略，按真实买一 / 卖一成交，给出当天的盘口成本和每个策略的结果。

    .venv313\\Scripts\\python.exe tools\\tick_replay.py                 # 最近一个存过的交易日，默认三只
    .venv313\\Scripts\\python.exe tools\\tick_replay.py 20261021 600366

只在 .venv313 跑（用了 3.7+ 写法）。不连桥、不下单。**一天只是一个个案**，看执行细节（价差、挂单能不能成交、
策略当天会怎么动作），不能当统计结论；天数攒多了再按天汇总。

回放内容：
  1. 盘口：买卖价差（几个价位 / 占股价 %）、买一卖一挂单量中位数、坏 tick 数。
  2. 当天的条件：开盘缺口、昨收在 20 日均线上方 / 下方、星期；对照股性档案规则账本，看今天触发哪条、账本里是什么状态。
  3. 固定对照组：开盘集合竞价先卖、收盘集合竞价买回 / 开盘先买、收盘卖出（按集合竞价价成交，没有价差）。
  4. 开盘缺口规则：开盘集合竞价进场，挂 0.5% / 1% / 2% 了结（成交价要「穿过」挂单价 1 分钱才算成交），没成交收盘了结。
  5. 均价带宽策略（t0_sim 的 StockT，就是模拟盘 / 实盘在用的规则），逐笔喂 tick、按对手价成交：
     模拟盘默认（底仓 20%、两个方向）、按股性档案（qty / sides）、按实盘规则（只做先卖后买、600366≤800 / 605208≤600 股）。
     带宽 = 近 20 日平均振幅 × band_k，用 QMT 日线（可能滞后一两天，跟实盘开盘时算的会差一点）。
  6. 盘中时段：开盘→10:00、10:00→14:55、13:30→14:55 的涨跌，对照六因子模型里「10 点后偏弱、13:30 后偏强」。
费用按 sim_t0/params.json（万0.8、最低 5 元、印花税、过户费），按实际股数算。
"""
import argparse
import datetime as dt
import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import t0_sim  # noqa: E402
from t0_sim import DATA_DIR, fee, load_params  # noqa: E402

TICK_DIR = os.path.join(DATA_DIR, 'ticks')
PROF_DIR = os.path.join(DATA_DIR, 'stock_profiles')
LIVE_CAPS = {'600366.SH': 800, '605208.SH': 600}     # t0_live.LIMITS（2026-10-19 用户定）
WEEK = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']


def code_of(c):
    c = c.strip().upper()
    return c if '.' in c else c + ('.SH' if c[0] in '659' else '.SZ')


def load_ticks(day, code):
    path = os.path.join(TICK_DIR, day, '%s.csv.gz' % code)
    if not os.path.exists(path):
        return None
    t = pd.read_csv(path, dtype={'stime': str})
    t['hms'] = t['stime'].str[8:14]
    return t


def base_of(code):
    from stock_profile import latest_base
    return latest_base(code)


def round_pnl(p, side, q, px_in, px_out):
    """side = 'buy_first' / 'sell_first'。返回 (毛利, 费用, 净利) 元。"""
    sell, buy = (px_out, px_in) if side == 'buy_first' else (px_in, px_out)
    gross = (sell - buy) * q
    cost = fee(p, 'sell', sell * q) + fee(p, 'buy', buy * q)
    return gross, cost, gross - cost


# ---------------------------------------------------------------- 各部分

def book_stats(t):
    c = t[(t['hms'] >= '093000') & (t['hms'] < '145700') & (t['bid1'] > 0) & (t['ask1'] > 0)]
    sp = (c['ask1'] - c['bid1']).round(2)
    mid = (c['ask1'] + c['bid1']) / 2
    v = t[t['volume'] > 0]
    vw = v['amount'] / v['volume'] / 100
    return {'价差1个价位占比%': (sp <= 0.01).mean() * 100, '平均价差_价位': (sp / 0.01).mean(),
            '平均价差%': (sp / mid).mean() * 100, '买一量中位_手': c['bidVol1'].median(),
            '卖一量中位_手': c['askVol1'].median(), 'tick数': len(t), '坏tick': int((abs(vw / v['lastPrice'] - 1) > 0.1).sum())}


def day_context(t, code, day):
    from qmt_bridge.dat_reader import read_daily
    o = t.loc[t['open'] > 0, 'open'].iloc[0]
    pc = t['lastClose'].iloc[-1]
    close = t['lastPrice'].iloc[-1]
    d, _ = read_daily(code)
    d = d[(d['volume'] > 0) & (d.index < day)]
    closes = d['close'].tolist()
    if not len(d) or d.index[-1] < prev_day(day):          # 日线缺了昨天，用 tick 里的昨收补上
        closes.append(pc)
    ma20 = np.mean(closes[-20:])
    return {'开盘': o, '昨收': pc, '收盘': close, '最高': t['high'].iloc[-1], '最低': t['low'].iloc[-1],
            '缺口%': (o / pc - 1) * 100, '昨收vs20日线': '上方' if pc > ma20 else '下方', '20日线': ma20,
            '星期': WEEK[pd.Timestamp(day).weekday()], '开到收%': (close / o - 1) * 100}


def prev_day(day):
    x = pd.Timestamp(day) - pd.Timedelta(days=1)
    while x.weekday() >= 5:
        x -= pd.Timedelta(days=1)
    return x.strftime('%Y%m%d')


def ledger_today(code, ctx):
    path = os.path.join(PROF_DIR, '%s.json' % code)
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        led = json.load(f)['规则账本']['开盘缺口']
    g = ctx['缺口%']
    out = []
    for r in led:
        lo, hi = (float(z) for z in r['缺口档'].rstrip('%').split('~'))
        a = g if r['方向'] == '高开先卖' else -g
        if lo < a <= hi:
            out.append(r)
    return out


def limit_exit(t, side, px_in, tgt):
    """开盘进场后挂 tgt% 了结；成交价穿过挂单价 1 分钱才算成交。返回 (出场价, 时间, 原因)。"""
    c = t[(t['hms'] > '093000') & (t['hms'] < '150000') & (t['lastPrice'] > 0)]
    if side == 'buy_first':
        lim = round(px_in * (1 + tgt / 100), 2)
        hit = c[c['lastPrice'] >= lim + 0.01]
    else:
        lim = round(px_in * (1 - tgt / 100), 2)
        hit = c[c['lastPrice'] <= lim - 0.01]
    if len(hit):
        return lim, hit['hms'].iloc[0], '挂单成交'
    return t['lastPrice'].iloc[-1], '150000', '收盘了结'


def band_of(code, p):
    from qmt_bridge.dat_reader import read_daily
    d, _ = read_daily(code)
    d = d[d['volume'] > 0].tail(21)
    amp = ((d['high'] - d['low']) / d['close'].shift(1)).dropna().tail(20).mean()
    return min(max(amp * p['band_k'], p['band_min']), p['band_max'])


def replay_band(t, code, base, p, ov, label, logs):
    band = band_of(code, p)
    st = t0_sim.StockT(code, '', base, band, p, lambda m: logs.append('    ' + m), ov)
    for r in t.itertuples():
        if r.hms < '093000' or r.lastPrice <= 0:
            continue
        st.on_tick(r.stime, r.lastPrice, r.bid1, r.ask1, r.amount, r.volume, r.lastClose)
    st.settle()
    rs = st.rounds
    return {'策略': label, '每次股数': st.qty, '带宽%': band * 100, '轮数': len(rs),
            '毛利': sum(x['毛利'] for x in rs), '费用': sum(x['费用'] for x in rs), '净利': sum(x['净利'] for x in rs),
            '明细': '；'.join('%s %s@%.2f→%s@%.2f %s %+.0f' % (
                '先卖' if x['方向'] == '正T' else '先买', x['开仓时间'][:5], x['开仓价'], x['平仓时间'][:5],
                x['平仓价'], x['原因'], x['净利']) for x in rs)}


def session_moves(t):
    c = t[(t['lastPrice'] > 0) & (t['hms'] >= '093000')]

    def at(h):
        x = c[c['hms'] <= h]
        return x['lastPrice'].iloc[-1] if len(x) else np.nan
    o = t.loc[t['open'] > 0, 'open'].iloc[0]
    p10, p1330, p1455 = at('100000'), at('133000'), at('145500')
    return {'开盘→10:00%': (p10 / o - 1) * 100, '10:00→14:55%': (p1455 / p10 - 1) * 100,
            '13:30→14:55%': (p1455 / p1330 - 1) * 100}


# ---------------------------------------------------------------- 主流程

def run(day, code, p):
    t = load_ticks(day, code)
    if t is None:
        print('\n%s %s 没有存 tick' % (day, code))
        return
    base = base_of(code) or 0
    print('\n' + '=' * 100)
    ctx = day_context(t, code, day)
    print('%s %s %s｜昨收 %.2f 开 %.2f（缺口 %+.2f%%）高 %.2f 低 %.2f 收 %.2f（开→收 %+.2f%%）｜昨收在 20 日线（%.2f）%s｜底仓 %d' % (
        day, code, ctx['星期'], ctx['昨收'], ctx['开盘'], ctx['缺口%'], ctx['最高'], ctx['最低'], ctx['收盘'],
        ctx['开到收%'], ctx['20日线'], ctx['昨收vs20日线'], base))
    print('=' * 100)
    b = book_stats(t)
    print('【盘口】价差 1 个价位的时间 %.0f%%，平均 %.2f 个价位（%.3f%%）；买一 / 卖一挂单量中位 %.0f / %.0f 手；%d 笔 tick，坏 tick %d 笔' % (
        b['价差1个价位占比%'], b['平均价差_价位'], b['平均价差%'], b['买一量中位_手'], b['卖一量中位_手'], b['tick数'], b['坏tick']))
    qty = LIVE_CAPS.get(code) or max(100, int(base * p['qty_ratio'] // 100 * 100))
    print('        → 用对手价一进一出，光价差就付约 %.3f%%；按 %d 股算约 %.0f 元' % (
        b['平均价差%'], qty, b['平均价差%'] / 100 * ctx['开盘'] * qty))
    o, cl = ctx['开盘'], ctx['收盘']
    rows = []
    for side, name in (('sell_first', '固定：开盘卖、收盘买回'), ('buy_first', '固定：开盘买、收盘卖出')):
        g, c_, n = round_pnl(p, side, qty, o, cl)
        rows.append({'策略': name, '股数': qty, '进场': '09:25 %.2f' % o, '出场': '15:00 %.2f' % cl,
                     '毛利': g, '费用': c_, '净利': n, '备注': ''})
    led = ledger_today(code, ctx)
    for r in led:
        side = 'sell_first' if r['方向'] == '高开先卖' else 'buy_first'
        px_out, tm, why = limit_exit(t, side, o, r['挂单%'])
        g, c_, n = round_pnl(p, side, qty, o, px_out)
        rows.append({'策略': '缺口规则：%s' % r['规则'], '股数': qty, '进场': '09:25 %.2f' % o,
                     '出场': '%s:%s %.2f %s' % (tm[:2], tm[2:4], px_out, why), '毛利': g, '费用': c_, '净利': n,
                     '备注': '账本：%s（十年 %d 天，净 %+.2f%%，t %+.1f）' % (r['状态'], r['天数'], r['净%'], r['t'])})
    pd.set_option('display.unicode.east_asian_width', True)
    pd.set_option('display.width', 250)
    print('\n【开盘进、收盘或挂单出】（集合竞价价成交，没有价差）')
    print('  ' + pd.DataFrame(rows).round(1).to_string(index=False).replace('\n', '\n  '))
    if ctx['昨收vs20日线'] == '上方':
        print('  ※ 昨收在 20 日线上方：按对照组的十年统计，今天不该开盘先卖')
    if not led:
        print('  ※ 今天的缺口（%+.2f%%）不在任何缺口规则的档位里' % ctx['缺口%'])

    print('\n【均价带宽策略逐笔回放】（按对手价成交；明细：方向 开仓时间@价→平仓时间@价 原因 净利）')
    from t0_sim import load_profile_overrides
    logs = []
    res = [replay_band(t, code, base or qty * 5, p, {}, '模拟盘默认（底仓20%、双向）', logs)]
    ov = load_profile_overrides(code)
    if ov:
        res.append(replay_band(t, code, base or qty * 5, p, ov, '按股性档案 %s' % json.dumps(ov, ensure_ascii=False), logs))
    if code in LIVE_CAPS:
        res.append(replay_band(t, code, base or qty * 5, p, {'qty': LIVE_CAPS[code], 'sides': ['sell_first']},
                               '按实盘规则（只先卖后买，≤%d 股）' % LIVE_CAPS[code], logs))
    for r in res:
        print('  %-40s 每次 %4d 股 带宽 %.2f%%：%d 轮 毛 %+.0f 费 %.0f 净 %+.0f  %s' % (
            r['策略'], r['每次股数'], r['带宽%'], r['轮数'], r['毛利'], r['费用'], r['净利'], r['明细']))

    m = session_moves(t)
    print('\n【盘中时段】开盘→10:00 %+.2f%%，10:00→14:55 %+.2f%%，13:30→14:55 %+.2f%%（六因子模型：10 点后偏弱、13:30 后偏强）' % (
        m['开盘→10:00%'], m['10:00→14:55%'], m['13:30→14:55%']))


def main():
    ap = argparse.ArgumentParser(description='用存下的 tick 回放做 T 策略')
    ap.add_argument('day', nargs='?', help='YYYYMMDD，默认最近一个存过的交易日')
    ap.add_argument('codes', nargs='*', default=['600366', '605208', '600436'])
    a = ap.parse_args()
    day = a.day
    if not day:
        days = sorted(x for x in os.listdir(TICK_DIR) if x.isdigit())
        day = days[-1] if days else dt.date.today().strftime('%Y%m%d')
    p = load_params()
    print('回放 %s，费用：佣金万%.1f 最低 %.0f 元 + 印花税 + 过户费（参数 v%s）' % (
        day, p['commission'] * 1e4, p['min_commission'], p['version']))
    for c in a.codes:
        run(day, code_of(c), p)


if __name__ == '__main__':
    main()
