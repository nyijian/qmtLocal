# coding:utf-8
"""三类日内形态的历史检验（5 分钟线），给底仓做 T 用。

    .venv313\\Scripts\\python.exe tools\\pattern_tests.py                       # 默认 600436（长样本）+ 600366/605208（短样本）
    .venv313\\Scripts\\python.exe tools\\pattern_tests.py --notional 30000 --slip 0.5

只在 .venv313 跑（用了 3.7+ 写法）。不下单。方向一律写「先卖后买」「先买后卖」，不用正T/反T，免得叫法混。

第一类 高开冲高回落（先卖后买）
  描述：高开 0~1% / 1~2% / 2% 以上，上午最高价出现在什么时段；上午见高之后到收盘回撤 ≥0.5% / 1% / 2% 的比例
        （这一段用了「上午最高价」，盘中不知道，只当描述，不能直接交易）。
  可交易：A. 09:35 第一根收盘卖；B. 等从今天到目前的最高价回落 0.5% 再卖（09:40~11:00 第一次出现）。
        买回挂在卖价下方 0.5% / 1% / 2%，没成交就 14:55 买回。
第二类 低开探底回升（先买后卖）
  描述：低开 0~1% / 1~2% / 2% 以上，开盘 15 / 30 分钟内跌破开盘价的比例、最大下探、10:00 时在开盘价 / VWAP 上方的比例。
  可交易：09:40~11:00 之间，价格跌破过开盘价后第一次收回开盘价上方（或从 VWAP 下方收回上方）就买，
        卖出挂在买价上方 0.5% / 1% / 2%，没成交就 14:55 卖；另给 15/30/60 分钟、到收盘的收益分布。
第三类 昨日成交密集区的突破、回踩、假突破
  昨天的成交密集区 = 把昨天每根 5 分钟线的量摊到价位上，围绕成交最多的价位占 70% 成交量的区间 VAL~VAH。
  事件（09:40~14:30，每类每天只取第一次）：
    上破 = 收盘从 VAH 下方到上方；下破 = 收盘从 VAL 上方到下方。按突破那根的量 / 过去 20 天同时段平均分 放量(>1.5) / 平量 / 缩量(<0.8)。
    跟随：突破那根收盘顺着方向做（上破先买后卖，下破先卖后买），看 30/60 分钟和到收盘。
    假突破率：之后 30 分钟内收盘回到区内的比例。
    回踩确认：上破后 60 分钟内有一根最低碰到 VAH（+0.2% 以内）且收在 VAH 上方，在那根收盘买；下破对称。
    假突破反做：上破后 30 分钟内收盘回到 VAH 下方，在那根收盘先卖后买；下破对称。
统计：每天最多一笔；挂单要「穿过」挂单价 1 分钱才算成交；费用按 --notional 每笔金额套万0.8（最低 5 元）+ 印花税 + 过户费，
再加每边 --slip 个价位的滑点。600436 分前 60% / 后 40% 天数各算一遍，两段同向才算有点意思；600366+605208 合并当短样本检验。
日线长样本复核：第一、二类的「开盘就进场 + 挂单」版本，用 QMT 本地日线（2015 年起）再算一遍——
  开盘进场时挂单成不成交只看当天最高 / 最低，日线就够准，样本从 40 天放大到上千天。
结果：控制台 + 共享数据目录 pattern_tests/events.csv（每个事件一行，可以自己再切）。

2026-10-21 首轮结论（详见 docs/决策记录.md）：5 分钟线 40 天里「高开 ≥1% 开盘卖、挂低 1% 买回」14 次全赢，
日线十年复核 600366 高开 1~2% 168 天净 -0.29%——短样本骗人。突破昨日密集区上沿跟进买入两段都亏，假突破率 55~72%。
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from t0_sim import DATA_DIR, load_params  # noqa: E402
from day_pattern import load_bars, load_preclose, profile, value_area  # noqa: E402

OUT_DIR = os.path.join(DATA_DIR, 'pattern_tests')
TARGETS = [0.5, 1.0, 2.0]


# ---------------------------------------------------------------- 逐日准备

def days_of(code):
    df = load_bars(code, use_sina=True)
    if df is None or not len(df):
        return []
    pre = load_preclose(code)
    out, prev, slot_hist = [], None, {}
    for d in sorted(set(df.index.str[:8])):
        b = df[df.index.str[:8] == d]
        hm = np.array(b.index.str[8:12])
        v = b['volume'].values.astype(float)
        if prev is not None and len(b) >= 45:
            pc = pre.get(d, prev['close'].iloc[-1])
            step = max(0.01, round(pc * 0.001, 2))
            e, pv = profile(prev, step)
            poc, val, vah = value_area(e, pv)
            amt = (b['amount'] if 'amount' in b else b['close'] * b['volume']).values
            cv, ca = np.cumsum(v), np.cumsum(amt)
            vwap = np.where(cv > 0, ca / np.maximum(cv, 1), b['close'].values)
            if np.nanmedian(vwap / b['close'].values) > 50:          # amount 元 / volume 手
                vwap = vwap / 100.0
            rv = np.array([v[i] / np.mean(slot_hist[s][-20:]) if s in slot_hist and len(slot_hist[s]) >= 10
                           and np.mean(slot_hist[s][-20:]) > 0 else np.nan for i, s in enumerate(hm)])
            out.append(dict(code=code, d=d, hm=hm, o=b['open'].values, h=b['high'].values, l=b['low'].values,
                            c=b['close'].values, vwap=vwap, rv=rv, pc=pc, gap=(b['open'].iloc[0] / pc - 1) * 100,
                            val=val, vah=vah, poc=poc, i1455=int(np.where(hm <= '1455')[0][-1])))
        for s, vv in zip(hm, v):
            slot_hist.setdefault(s, []).append(vv)
        prev = b
    return out


def idx(day, t):
    w = np.where(day['hm'] == t)[0]
    return int(w[0]) if len(w) else None


def trade(day, i, side, target):
    """第 i 根收盘进场，side=+1 先买后卖 / -1 先卖后买；挂单 target%，穿过 1 分钱算成交，否则 14:55 收盘出。返回毛收益 %。"""
    px, end = day['c'][i], day['i1455']
    if i >= end:
        return np.nan
    if target:
        tp = px * (1 + side * target / 100)
        for j in range(i + 1, end + 1):
            if side > 0 and day['h'][j] >= tp + 0.01:
                return side * (tp / px - 1) * 100
            if side < 0 and day['l'][j] <= tp - 0.01:
                return side * (tp / px - 1) * 100
    return side * (day['c'][end] / px - 1) * 100


def fwd(day, i, side, n):
    j = day['i1455'] if n is None else min(i + n, day['i1455'])
    return side * (day['c'][j] / day['c'][i] - 1) * 100 if j > i else np.nan


def event(day, test, variant, bucket, i, side, extra=None):
    r = dict(代码=day['code'], 日期=day['d'], 测试=test, 变体=variant, 分组=bucket, 时点=day['hm'][i],
             价=day['c'][i], 方向='先买后卖' if side > 0 else '先卖后买',
             r15=fwd(day, i, side, 3), r30=fwd(day, i, side, 6), r60=fwd(day, i, side, 12), 到收盘=fwd(day, i, side, None))
    for t in TARGETS:
        r['挂%.1f%%' % t] = trade(day, i, side, t)
    seg = slice(i + 1, min(i + 13, day['i1455']) + 1)
    if day['i1455'] > i:
        r['60分最大有利'] = max(0.0, (np.max(day['h'][seg]) / day['c'][i] - 1) * 100 if side > 0
                            else (1 - np.min(day['l'][seg]) / day['c'][i]) * 100)
        r['60分最大不利'] = min(0.0, (np.min(day['l'][seg]) / day['c'][i] - 1) * 100 if side > 0
                            else (1 - np.max(day['h'][seg]) / day['c'][i]) * 100)
    if extra:
        r.update(extra)
    return r


def gap_bucket(g, up):
    a = g if up else -g
    if a <= 0:
        return None
    return '0~1%' if a <= 1 else ('1~2%' if a <= 2 else '2%以上')


# ---------------------------------------------------------------- 三类测试

def test1(day, desc):
    b = gap_bucket(day['gap'], True)
    if b is None:
        return []
    am = day['hm'] <= '1130'
    ih = int(np.argmax(np.where(am, day['h'], -np.inf)))
    hi = day['h'][ih]
    after_low = np.min(day['l'][ih + 1:day['i1455'] + 1]) if ih < day['i1455'] else hi
    t = day['hm'][ih]
    desc.append(dict(代码=day['code'], 日期=day['d'], 分组=b,
                     上午高点时段='09:30-09:45' if t <= '0945' else ('09:45-10:00' if t <= '1000' else
                                                              ('10:00-10:30' if t <= '1030' else '10:30-11:30')),
                     **{'回撤≥%.1f%%' % x: (1 - after_low / hi) * 100 >= x for x in TARGETS},
                     收盘低于开盘=day['c'][day['i1455']] < day['o'][0]))
    ev = [event(day, '1 高开冲高回落', 'A 09:35卖', b, 0, -1)]
    run_hi = np.maximum.accumulate(day['h'])
    for i in range(1, len(day['hm'])):
        if day['hm'][i] < '0940' or day['hm'][i] > '1100':
            continue
        if day['c'][i] <= run_hi[i] * 0.995:
            ev.append(event(day, '1 高开冲高回落', 'B 回落0.5%再卖', b, i, -1))
            break
    return ev


def test2(day, desc):
    b = gap_bucket(day['gap'], False)
    if b is None:
        return []
    o = day['o'][0]
    i15, i30 = idx(day, '0945'), idx(day, '1000')
    if i15 is None or i30 is None:
        return []
    desc.append(dict(代码=day['code'], 日期=day['d'], 分组=b,
                     **{'15分跌破开盘': day['l'][:i15 + 1].min() < o, '30分跌破开盘': day['l'][:i30 + 1].min() < o,
                        '30分最大下探%': (day['l'][:i30 + 1].min() / o - 1) * 100,
                        '10点在开盘价上': day['c'][i30] > o, '10点在VWAP上': day['c'][i30] > day['vwap'][i30]}))
    ev = []
    for name, ref in (('收回开盘价', lambda i: o), ('收回VWAP', lambda i: day['vwap'][i])):
        been_below = False
        for i in range(len(day['hm'])):
            if day['hm'][i] > '1100':
                break
            if day['l'][i] < ref(i):
                been_below = True
            if i >= 1 and day['hm'][i] >= '0940' and been_below and day['c'][i] > ref(i) and day['c'][i - 1] <= ref(i - 1):
                ev.append(event(day, '2 低开探底回升', name, b, i, +1))
                break
    return ev


def test3(day):
    ev = []
    vah, val = day['vah'], day['val']
    c, l, h, hm = day['c'], day['l'], day['h'], day['hm']
    for up in (True, False):
        side = 1 if up else -1
        lvl = vah if up else val
        name = '上破VAH' if up else '下破VAL'
        for i in range(1, len(hm)):
            if hm[i] < '0940' or hm[i] > '1430':
                continue
            crossed = (c[i] > lvl >= c[i - 1]) if up else (c[i] < lvl <= c[i - 1])
            if not crossed:
                continue
            rv = day['rv'][i]
            vb = '-' if np.isnan(rv) else ('放量' if rv > 1.5 else ('缩量' if rv < 0.8 else '平量'))
            back = [j for j in range(i + 1, min(i + 6, day['i1455']) + 1) if (c[j] < lvl if up else c[j] > lvl)]
            ev.append(event(day, '3 成交密集区', name + ' 跟随', vb, i, side, {'30分内假突破': bool(back)}))
            if back:
                ev.append(event(day, '3 成交密集区', name + ' 假突破反做', vb, back[0], -side))
            for j in range(i + 1, min(i + 12, day['i1455']) + 1):
                touch = (l[j] <= lvl * 1.002 and c[j] > lvl) if up else (h[j] >= lvl * 0.998 and c[j] < lvl)
                if touch:
                    ev.append(event(day, '3 成交密集区', name + ' 回踩确认', vb, j, side))
                    break
            break
    return ev


# ---------------------------------------------------------------- 统计

def cost_pct(p, notional, px, slip):
    c = 2 * max(notional * p['commission'], p['min_commission']) + notional * (p['stamp_tax'] + 2 * p['transfer_fee'])
    return c / notional * 100 + 2 * slip * 0.01 / px * 100


def summarize(ev, col, cost_col):
    x = ev.dropna(subset=[col])
    if len(x) < 3:
        return '%3d 笔' % len(x)
    net = x[col] - x[cost_col]
    t = net.mean() / net.std() * np.sqrt(len(net)) if net.std() > 0 else np.nan
    return '%3d 笔 毛%+.2f 净%+.2f t%+.1f 胜%2.0f%%' % (len(x), x[col].mean(), net.mean(), t, (net > 0).mean() * 100)


def report(ev, desc1, desc2, split):
    pd.set_option('display.unicode.east_asian_width', True)
    pd.set_option('display.width', 250)
    long_ = ev['代码'] == '600436.SH'
    parts = [('600436 前60%', long_ & (ev['日期'] < split)), ('600436 后40%', long_ & (ev['日期'] >= split)),
             ('600366+605208', ~long_)]

    print('\n' + '=' * 100 + '\n【第一类】高开冲高回落 —— 描述（用了全天信息，只看规律，不能直接交易）')
    d1 = pd.DataFrame(desc1)
    for grp, x in (('600436', d1[d1['代码'] == '600436.SH']), ('600366+605208', d1[d1['代码'] != '600436.SH'])):
        if not len(x):
            continue
        t = x.groupby('分组').agg(天数=('日期', 'size'),
                                 **{k: (k, lambda s: s.mean() * 100) for k in ['回撤≥0.5%', '回撤≥1.0%', '回撤≥2.0%', '收盘低于开盘']})
        tm = pd.crosstab(x['分组'], x['上午高点时段'], normalize='index') * 100
        print('\n  %s：上午高点之后到收盘的回撤比例 %%' % grp)
        print('  ' + t.round(0).to_string().replace('\n', '\n  '))
        print('  上午最高价出现时段 %%：')
        print('  ' + tm.round(0).to_string().replace('\n', '\n  '))

    print('\n' + '=' * 100 + '\n【第二类】低开探底回升 —— 描述')
    d2 = pd.DataFrame(desc2)
    for grp, x in (('600436', d2[d2['代码'] == '600436.SH']), ('600366+605208', d2[d2['代码'] != '600436.SH'])):
        if not len(x):
            continue
        t = x.groupby('分组').agg(天数=('日期', 'size'), **{k: (k, lambda s: s.mean() * 100) for k in
                                                         ['15分跌破开盘', '30分跌破开盘', '10点在开盘价上', '10点在VWAP上']},
                                 平均30分下探=('30分最大下探%', 'mean'))
        print('\n  %s（%%）：' % grp)
        print('  ' + t.round(1).to_string().replace('\n', '\n  '))

    print('\n' + '=' * 100)
    print('【可交易检验】每格：笔数、扣费前平均 %、扣费后平均 %、t、扣费后胜率。挂 x% = 挂单目标，没成交 14:55 平')
    cols = ['r30', 'r60', '到收盘'] + ['挂%.1f%%' % t for t in TARGETS]
    for (test, var), g in ev.groupby(['测试', '变体'], sort=True):
        print('\n%s ｜ %s（%s）' % (test, var, g['方向'].iloc[0]))
        for bucket, gb in [('全部', g)] + sorted(g.groupby('分组'), key=lambda kv: kv[0]):
            if bucket == '-':
                continue
            for pname, pm in parts:
                x = gb[pm.loc[gb.index]]
                if len(x) < 3:
                    continue
                print('  %-6s %-14s %s' % (bucket, pname, ' ｜ '.join('%s %s' % (c, summarize(x, c, '成本%')) for c in cols[2:])))
        if '30分内假突破' in g.columns and g['30分内假突破'].notna().any():
            for pname, pm in parts:
                x = g[pm.loc[g.index]]
                if len(x):
                    print('  假突破率（30 分钟内收回区内） %-14s %.0f%%（%d 次）' % (pname, x['30分内假突破'].mean() * 100, len(x)))


def gap_rule_stats(d, p, notional, slip):
    """日线长样本：开盘先卖（高开）/ 先买（低开），挂 x% 了结，没成交收盘了结。返回每个（方向, 缺口档, 挂单）一行。
    开盘就进场，挂单是否成交只看当天最高 / 最低，跟先高后低的顺序无关，所以日线就够准，样本能放大到十年。
    d 是 dat_reader.read_daily 的 DataFrame（已按日期筛好）。stock_profile.py 也用它给每只股票的规则记账。"""
    gap = (d['open'] / d['preClose'] - 1) * 100
    cost = np.array([cost_pct(p, notional, px, slip) for px in d['open']])
    rows = []
    for up in (True, False):
        side = -1 if up else 1
        for lo, hi in ((0.3, 1), (1, 2), (2, 4), (4, 11)):
            m = ((gap > lo) & (gap <= hi)) if up else ((-gap > lo) & (-gap <= hi))
            x, cx = d[m], cost[m.values]
            if len(x) < 5:
                continue
            for tgt in TARGETS:
                if up:
                    hit = x['low'] <= x['open'] * (1 - tgt / 100) - 0.01
                else:
                    hit = x['high'] >= x['open'] * (1 + tgt / 100) + 0.01
                g = np.where(hit, tgt, side * (x['close'] / x['open'] - 1) * 100)
                net = g - cx
                n, h = len(net), len(net) // 2
                rows.append({'规则': '%s %g~%g%% 挂%.1f%%' % ('高开先卖' if up else '低开先买', lo, hi, tgt),
                             '方向': '高开先卖' if up else '低开先买', '缺口档': '%g~%g%%' % (lo, hi), '挂单%': tgt,
                             '天数': n, '成交率%': hit.mean() * 100, '净%': net.mean(),
                             't': net.mean() / net.std() * np.sqrt(n) if net.std() > 0 else np.nan,
                             '前半净%': net[:h].mean(), '后半净%': net[h:].mean()})
    return rows


def daily_gap_test(codes, p, notional, slip, since='20150101'):
    from qmt_bridge.dat_reader import read_daily
    print('\n' + '=' * 100)
    print('【日线长样本复核】第一、二类的开盘版：高开开盘先卖、挂低 x% 买回；低开开盘先买、挂高 x% 卖出；没成交收盘了结')
    for c in codes:
        code = c.upper() if '.' in c else c + ('.SH' if c[0] in '659' else '.SZ')
        try:
            d, _ = read_daily(code)
        except Exception as e:
            print('%s 日线读不到：%s' % (code, e))
            continue
        d = d[(d.index >= since) & (d['volume'] > 0)]
        print('\n  %s %s~%s 共 %d 天' % (code, d.index[0], d.index[-1], len(d)))
        rows = gap_rule_stats(d, p, notional, slip)
        for key in dict.fromkeys((r['方向'], r['缺口档']) for r in rows):
            rr = [r for r in rows if (r['方向'], r['缺口档']) == key]
            print('    %s %s %4d 天 |' % (key[0], key[1], rr[0]['天数']) + ''.join(
                ' 挂%.1f%% 成交%2.0f%% 净%+.2f t%+.1f 前/后半%+.2f/%+.2f |' % (
                    r['挂单%'], r['成交率%'], r['净%'], r['t'], r['前半净%'], r['后半净%']) for r in rr))


def main():
    ap = argparse.ArgumentParser(description='三类日内形态检验')
    ap.add_argument('codes', nargs='*', default=['600436', '600366', '605208'])
    ap.add_argument('--notional', type=float, default=30000, help='每笔成交额，用来算最低 5 元佣金占比，默认 3 万')
    ap.add_argument('--slip', type=float, default=0.5, help='每边滑点几个价位，默认 0.5')
    a = ap.parse_args()
    p = load_params()
    os.makedirs(OUT_DIR, exist_ok=True)
    evs, desc1, desc2, split = [], [], [], None
    for c in a.codes:
        code = c.upper() if '.' in c else c + ('.SH' if c[0] in '659' else '.SZ')
        days = days_of(code)
        print('%s：%d 天（%s ~ %s）' % (code, len(days), days[0]['d'] if days else '-', days[-1]['d'] if days else '-'))
        if code == '600436.SH' and days:
            split = days[int(len(days) * 0.6)]['d']
        for day in days:
            evs += test1(day, desc1) + test2(day, desc2) + test3(day)
    ev = pd.DataFrame(evs)
    ev['成本%'] = [cost_pct(p, a.notional, px, a.slip) for px in ev['价']]
    ev.to_csv(os.path.join(OUT_DIR, 'events.csv'), index=False, encoding='utf-8-sig')
    print('每笔 %.0f 元、滑点每边 %.1f 个价位：600436 成本约 %.3f%%，600366 约 %.3f%%，605208 约 %.3f%%（来回）' % (
        a.notional, a.slip, cost_pct(p, a.notional, 170, a.slip), cost_pct(p, a.notional, 11, a.slip),
        cost_pct(p, a.notional, 14, a.slip)))
    report(ev, desc1, desc2, split or '99999999')
    daily_gap_test(a.codes, p, a.notional, a.slip)


if __name__ == '__main__':
    main()
