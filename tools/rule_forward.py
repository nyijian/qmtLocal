# coding:utf-8
"""规则前向记录：还在「观察」阶段的做 T 规则，每天收盘后用当天存的 tick 按真实盘口记一笔假账，攒样本外战绩。

    .venv313\\Scripts\\python.exe tools\\rule_forward.py                 # 记今天（tick_archive.py 收工后会自动调）
    .venv313\\Scripts\\python.exe tools\\rule_forward.py 20261021        # 补记某天
    .venv313\\Scripts\\python.exe tools\\rule_forward.py --report        # 看累计战绩

只在 .venv313 跑（用了 3.7+ 写法）。不下单。规则都是「开盘进、收盘出」，收盘后就能精确算，不用盘中盯。

现在记的规则（2026-10-21 起）：
  相对板块弱开买：个股缺口 - 所属行业板块缺口 ≤ 历史 20% 分位（只用前一天及以前的数据算）→ 09:30 按第一笔卖一价买，
                  15:00 收盘集合竞价卖。来源 index_open_test.py，按年滚动每笔净 +0.19~+0.40%，t 1.2~1.6，看完数据才挑的，先观察。
  账本缺口规则：股性档案「开盘缺口」账本里状态是「可模拟」的规则（现在只有 600436 小幅低开先买、挂高 x% 卖）：
                09:30 第一笔对手价进场，挂单穿过 1 分钱算成交，没成交收盘了结。
  对照：固定开盘买收盘卖、固定开盘卖收盘买回（09:30 对手价进场、收盘出），每天都记，用来比。
股数：600366 800、605208 600（实盘额度），其余按底仓 20%。费用按 sim_t0/params.json。
记录写共享数据目录 rule_forward/log.csv（同一天重跑会覆盖那天的行）。股性档案（stock_profile.py）会读它。
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

from t0_sim import DATA_DIR, load_params  # noqa: E402
from tick_replay import LIVE_CAPS, load_ticks, round_pnl, base_of, TICK_DIR  # noqa: E402

OUT_DIR = os.path.join(DATA_DIR, 'rule_forward')
LOG = os.path.join(OUT_DIR, 'log.csv')
PROF_DIR = os.path.join(DATA_DIR, 'stock_profiles')
SECTORS = {'600366.SH': '金属新材料', '605208.SH': '工业金属', '600436.SH': '中药Ⅱ'}
REL_Q = 0.2                    # 相对板块缺口的触发分位


def first_quote(t, side):
    """09:30 起第一笔有对手价的 tick：买用卖一，卖用买一。"""
    c = t[(t['hms'] >= '093000') & (t['bid1'] > 0) & (t['ask1'] > 0)]
    r = c.iloc[0]
    return (r['ask1'] if side == 'buy_first' else r['bid1']), r['hms']


def sector_gap_today(code, day):
    """板块今天缺口 + 历史「个股缺口 - 板块缺口」的 20% 分位（只用 day 之前）。板块日线从 akshare 刷新。"""
    from factor_model import sector_daily
    from qmt_bridge.dat_reader import read_daily
    name = SECTORS.get(code)
    if not name:
        return None, None
    s = sector_daily(name, refresh=True)
    sg = (s['open'] / s['close'].shift(1) - 1) * 100
    if day not in sg.index:
        return None, None
    d, _ = read_daily(code)
    d = d[(d['volume'] > 0) & (d.index < day)]
    rel = ((d['open'] / d['preClose'] - 1) * 100 - sg.reindex(d.index)).dropna()
    return sg[day], rel.quantile(REL_Q) if len(rel) > 100 else None


def ledger_rules(code):
    path = os.path.join(PROF_DIR, '%s.json' % code)
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        return [r for r in json.load(f)['规则账本']['开盘缺口'] if r['状态'] == '可模拟']


def limit_exit(t, side, px_in, tgt, t_in):
    c = t[(t['hms'] > t_in) & (t['hms'] < '150000') & (t['lastPrice'] > 0)]
    lim = round(px_in * (1 + tgt / 100), 2) if side == 'buy_first' else round(px_in * (1 - tgt / 100), 2)
    hit = c[c['lastPrice'] >= lim + 0.01] if side == 'buy_first' else c[c['lastPrice'] <= lim - 0.01]
    if len(hit):
        return lim, hit['hms'].iloc[0]
    return t['lastPrice'].iloc[-1], '150000'


def record(day, code, p):
    t = load_ticks(day, code)
    if t is None or not (t['open'] > 0).any():
        print('%s %s 没有 tick，跳过' % (day, code))
        return []
    o = t.loc[t['open'] > 0, 'open'].iloc[0]
    pc = t['lastClose'].iloc[-1]
    close = t['lastPrice'].iloc[-1]
    gap = (o / pc - 1) * 100
    base = base_of(code) or 0
    qty = LIVE_CAPS.get(code) or max(100, int(base * p['qty_ratio'] // 100 * 100))
    rows = []

    def add(rule, trig, side, cond, thr, px_out=None, t_out='150000', note=''):
        r = {'日期': day, '代码': code, '规则': rule, '触发': int(trig), '条件值': round(cond, 3) if cond is not None else '',
             '阈值': round(thr, 3) if thr is not None else '', '股数': qty, '方向': '', '进场时间': '', '进场价': '',
             '出场时间': '', '出场价': '', '毛利': 0.0, '费用': 0.0, '净利': 0.0, '备注': note}
        if trig:
            px_in, t_in = first_quote(t, side)
            px_out = close if px_out is None else px_out
            g, c_, n = round_pnl(p, side, qty, px_in, px_out)
            r.update({'方向': '先买后卖' if side == 'buy_first' else '先卖后买', '进场时间': t_in, '进场价': px_in,
                      '出场时间': t_out, '出场价': px_out, '毛利': round(g, 2), '费用': round(c_, 2), '净利': round(n, 2)})
        rows.append(r)

    add('对照：固定开盘买', True, 'buy_first', gap, None)
    add('对照：固定开盘卖', True, 'sell_first', gap, None)
    try:
        sg, thr = sector_gap_today(code, day)
    except Exception as e:
        sg, thr = None, None
        print('%s 板块数据取不到：%s' % (code, str(e)[:60]))
    if sg is not None and thr is not None:
        rel = gap - sg
        add('相对板块弱开买', rel <= thr, 'buy_first', rel, thr, note='个股缺口 %+.2f%% 板块 %+.2f%%' % (gap, sg))
    else:
        add('相对板块弱开买', False, 'buy_first', None, None, note='板块数据缺')
    for r in ledger_rules(code):
        lo, hi = (float(z) for z in r['缺口档'].rstrip('%').split('~'))
        up = r['方向'] == '高开先卖'
        side = 'sell_first' if up else 'buy_first'
        trig = lo < (gap if up else -gap) <= hi
        if trig:
            px_in, t_in = first_quote(t, side)
            px_out, t_out = limit_exit(t, side, px_in, r['挂单%'], t_in)
            add('账本：' + r['规则'], True, side, gap, None, px_out, t_out)
        else:
            add('账本：' + r['规则'], False, side, gap, None)
    return rows


def save(rows, day):
    os.makedirs(OUT_DIR, exist_ok=True)
    new = pd.DataFrame(rows)
    if os.path.exists(LOG):
        old = pd.read_csv(LOG, dtype={'日期': str}, encoding='utf-8-sig')
        old = old[old['日期'] != day]
        new = pd.concat([old, new])
    new.sort_values(['日期', '代码', '规则']).to_csv(LOG, index=False, encoding='utf-8-sig')


def run(day=None, codes=None):
    p = load_params()
    day = day or dt.date.today().strftime('%Y%m%d')
    codes = codes or list(SECTORS)
    rows = []
    for c in codes:
        rows += record(day, c, p)
    if rows:
        save(rows, day)
        for r in rows:
            if r['触发']:
                print('  %s %s %-28s %s %s@%s → %s@%s 净 %+.0f %s' % (
                    day, r['代码'], r['规则'], r['方向'], r['进场时间'][:4], r['进场价'], r['出场时间'][:4],
                    r['出场价'], r['净利'], r['备注']))
    return rows


def summary(code=None):
    """按（代码, 规则）汇总触发次数和净利，stock_profile 也用。"""
    if not os.path.exists(LOG):
        return pd.DataFrame()
    t = pd.read_csv(LOG, dtype={'日期': str}, encoding='utf-8-sig')
    if code:
        t = t[t['代码'] == code]
    days = t.groupby('代码')['日期'].nunique()
    g = t[t['触发'] == 1].groupby(['代码', '规则']).agg(触发次数=('净利', 'size'), 净利合计=('净利', 'sum'),
                                                       每次平均=('净利', 'mean'), 赚钱次数=('净利', lambda s: (s > 0).sum()))
    g['记录天数'] = [days[c] for c, _ in g.index]
    return g.reset_index()


def main():
    ap = argparse.ArgumentParser(description='观察中规则的前向记录')
    ap.add_argument('day', nargs='?')
    ap.add_argument('--report', action='store_true')
    a = ap.parse_args()
    if a.report:
        s = summary()
        pd.set_option('display.unicode.east_asian_width', True)
        pd.set_option('display.width', 200)
        print(s.round(1).to_string(index=False) if len(s) else '还没有记录')
        return
    if a.day:
        run(a.day)
        return
    days = sorted(x for x in os.listdir(TICK_DIR) if x.isdigit())
    run(days[-1] if days else None)


if __name__ == '__main__':
    main()
