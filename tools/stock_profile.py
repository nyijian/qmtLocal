# coding:utf-8
"""股性档案：每只股票单独算「它是什么脾气」，记下哪些做 T 规则对它有效、哪些被否决，再给模拟盘出这只股票专用的设置。
每周（或补完 QMT 日线后）重算一次，档案会记历史，能看出股性在变。

    .venv313\\Scripts\\python.exe tools\\stock_profile.py                     # 重算默认三只（600366 605208 600436）并打印股性卡
    .venv313\\Scripts\\python.exe tools\\stock_profile.py 600366 --base 600366=4000
    .venv313\\Scripts\\python.exe tools\\stock_profile.py --show              # 只看现有档案，不重算

只在 .venv313 跑（用了 3.7+ 写法）。不下单。

档案放共享数据目录 stock_profiles/：
  <代码>.json     当前档案（股性、规则账本、建议设置）。t0_sim.py 开盘时读它的 t0_overrides，按只股票覆盖设置。
  history.csv     每次重算追加一行关键指标，看股性漂移。
  manual.json     （可选）手工覆盖，优先级最高，例如 {"600366.SH": {"qty": 800, "sides": ["sell_first"]}}。

股性（日线 = QMT 本地 .DAT，2015 年起；近期 = 最近 60 个交易日；日内 = 5 分钟线，样本天数会标出来）：
  价位成本   一个价位占股价的比例（滑点）、按建议股数算的来回成本
  流动性     近 20 日日均成交额
  波动       日振幅（近 20 / 近 60 / 长期），开盘后往上 / 往下能走多远（开盘到最高、开盘到最低的中位数）
  从开盘出发 当天最高 ≥ 开盘 +0.5/1/2% 的比例、最低 ≤ 开盘 -0.5/1/2% 的比例（挂单能成交的概率）
  缺口       高开 / 低开的频率；缺口当天回补（碰到昨收）的比例；缺口方向和当天开→收是否反着走（回补型）还是顺着走（延续型）
  日内性格   先高后低比例、上午高点在开盘 15 分钟内的比例、开盘 30 分钟和之后的走势相关性（正 = 趋势型，负 = 回归型）
  涨跌停     近一年触及涨跌停的天数
规则账本：
  * 开盘缺口规则（高开先卖 / 低开先买 × 缺口档 × 挂单）：用日线长样本算（pattern_tests.gap_rule_stats），
    t ≥ 2、前后两半都赚、至少 60 天 → 「可模拟」；均值 < 0 且 t ≤ -2 → 「否决」；其余「观察」。
  * 模拟盘反馈（sim_t0/trades.csv 里 live 模式的战绩）：某个方向（正T / 反T）累计 ≥ 8 轮且净亏 → 这只股票关掉这个方向；
    不到 8 轮只记账不动作（样本太少不下结论）。
建议设置（t0_overrides，模拟盘自动用；实盘 t0_live 不读，实盘额度另外由用户定）：
  qty    每次股数 = 先按底仓 × qty_ratio，再至少凑到每笔 3 万元（摊薄最低 5 元佣金），
         但不超过「每日亏损上限 ÷（股价 × 近 60 日开盘后不利波动中位数）」和底仓的一半；成本和风险冲突时取风险那头并标出来。
  sides  允许的方向（模拟盘反馈关掉的方向去掉）。
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

from t0_sim import DATA_DIR, SIM_DIR, load_params  # noqa: E402

PROF_DIR = os.path.join(DATA_DIR, 'stock_profiles')
HIST_FILE = os.path.join(PROF_DIR, 'history.csv')
MANUAL_FILE = os.path.join(PROF_DIR, 'manual.json')
DEFAULT_CODES = ['600366', '605208', '600436']
MIN_NOTIONAL = 30000          # 每笔至少这么多钱，最低 5 元佣金才摊得薄
SIM_MIN_ROUNDS = 8            # 模拟盘某方向至少这么多轮才下结论
RULE_MIN_DAYS = 60            # 规则至少这么多天样本才能「可模拟」：每只要试 24 条，t≥2 平均就有 1 条是碰巧的
SIDE_NAME = {'sell_first': '正T(先卖后买)', 'buy_first': '反T(先买后卖)'}


def code_of(c):
    c = c.strip().upper()
    return c if '.' in c else c + ('.SH' if c[0] in '659' else '.SZ')


def r2(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), 2)


# ---------------------------------------------------------------- 股性

def daily_traits(d):
    o, h, l, c, pc = d['open'], d['high'], d['low'], d['close'], d['preClose']
    amp = (h - l) / pc * 100
    up, dn = (h / o - 1) * 100, (1 - l / o) * 100
    gap = (o / pc - 1) * 100
    oc = (c / o - 1) * 100
    rec = d.tail(60)
    gu, gd = gap > 0.3, gap < -0.3
    t = {
        '股价': r2(c.iloc[-1]),
        '一个价位%': r2(0.01 / c.iloc[-1] * 100),
        '近20日日均成交额万': r2(d['amount'].tail(20).mean() / 1e4),
        '振幅%_近20': r2(amp.tail(20).mean()), '振幅%_近60': r2(amp.tail(60).mean()), '振幅%_长期': r2(amp.mean()),
        '开盘到最高%_中位_近60': r2(up.tail(60).median()), '开盘到最低%_中位_近60': r2(dn.tail(60).median()),
        '高开天数%': r2(gu.mean() * 100), '低开天数%': r2(gd.mean() * 100),
        '高开当天回补%': r2((l[gu] <= pc[gu]).mean() * 100), '低开当天回补%': r2((h[gd] >= pc[gd]).mean() * 100),
        '缺口与开收相关': r2(np.corrcoef(gap[gu | gd], oc[gu | gd])[0, 1]) if (gu | gd).sum() > 10 else None,
        '近一年涨跌停天数': int(((c / pc - 1).abs().tail(250) > 0.095).sum()),
    }
    for x in (0.5, 1, 2):
        t['最高≥开+%g%%_近60' % x] = r2((up.tail(60) >= x).mean() * 100)
        t['最低≤开-%g%%_近60' % x] = r2((dn.tail(60) >= x).mean() * 100)
        t['最高≥开+%g%%_长期' % x] = r2((up >= x).mean() * 100)
        t['最低≤开-%g%%_长期' % x] = r2((dn >= x).mean() * 100)
    t['开盘后不利波动%_近60'] = r2(max(up.tail(60).median(), dn.tail(60).median()))
    t['_近60天'] = len(rec)
    return t


def intraday_traits(code):
    from day_pattern import load_bars
    try:
        df = load_bars(code, use_sina=True)
    except Exception:
        df = None
    if df is None or not len(df):
        return {'日内样本天数': 0}
    hi_first, early_hi, a, b = [], [], [], []
    for day, x in df.groupby(df.index.str[:8]):
        hm = x.index.str[8:12]
        ih, il = int(np.argmax(x['high'].values)), int(np.argmin(x['low'].values))
        if ih != il:
            hi_first.append(ih < il)
        am = x[hm <= '1130']
        early_hi.append(am.index[int(np.argmax(am['high'].values))][8:12] <= '0945')
        p10 = x[hm <= '1000']['close'].iloc[-1]
        p1455 = x[hm <= '1455']['close'].iloc[-1]
        a.append(p10 / x['open'].iloc[0] - 1)
        b.append(p1455 / p10 - 1)
    n = len(early_hi)
    return {'日内样本天数': n,
            '先高后低%': r2(np.mean(hi_first) * 100),
            '上午高点在前15分钟%': r2(np.mean(early_hi) * 100),
            '开盘30分与之后相关': r2(np.corrcoef(a, b)[0, 1]) if n > 10 else None}


def character(t, it):
    """把数字翻成几句话。"""
    out = []
    corr = it.get('开盘30分与之后相关')
    if corr is not None and it['日内样本天数'] >= 100:
        out.append('日内%s（开盘 30 分钟和之后相关 %+.2f）' % ('偏趋势：开盘往哪走之后容易接着走' if corr > 0.1 else
                                                     ('偏回归：开盘冲多了之后容易回来' if corr < -0.1 else '趋势和回归都不明显'), corr))
    elif corr is not None:
        out.append('日内性格样本只有 %d 天，暂不下结论' % it['日内样本天数'])
    gc = t.get('缺口与开收相关')
    if gc is not None:
        out.append('缺口%s（缺口和开→收相关 %+.2f；高开当天回补 %.0f%%，低开当天回补 %.0f%%）' % (
            '偏回补' if gc < -0.1 else ('偏延续' if gc > 0.1 else '没有明显方向'), gc, t['高开当天回补%'], t['低开当天回补%']))
    out.append('波动：近 20 日振幅 %.1f%%（长期 %.1f%%）%s' % (
        t['振幅%_近20'], t['振幅%_长期'], '，最近比平时活跃' if t['振幅%_近20'] > 1.2 * t['振幅%_长期'] else
        ('，最近比平时安静' if t['振幅%_近20'] < 0.8 * t['振幅%_长期'] else '')))
    out.append('从开盘出发：近 60 天最高 ≥ 开 +1%% 的 %.0f%%，最低 ≤ 开 -1%% 的 %.0f%%' % (
        t['最高≥开+1%_近60'], t['最低≤开-1%_近60']))
    if t['一个价位%'] >= 0.08:
        out.append('股价低，一个价位就是 %.2f%%，市价进出的滑点很贵，要挂单' % t['一个价位%'])
    return out


# ---------------------------------------------------------------- 规则账本

def gap_ledger(d, p, notional):
    from pattern_tests import gap_rule_stats
    rows = gap_rule_stats(d[d.index >= '20150101'], p, notional, 0.5)
    for r in rows:
        if r['t'] >= 2 and r['前半净%'] > 0 and r['后半净%'] > 0 and r['天数'] >= RULE_MIN_DAYS:
            r['状态'] = '可模拟'
        elif r['净%'] < 0 and r['t'] <= -2:
            r['状态'] = '否决'
        else:
            r['状态'] = '观察'
        for k in ('成交率%', '净%', 't', '前半净%', '后半净%'):
            r[k] = r2(r[k])
    return rows


def rel_sector_ledger(code, p, qty):
    """「相对板块弱开买」：个股缺口 - 板块缺口 ≤ 之前年份的 20% 分位 → 开盘买、收盘卖。按年滚动样本外。
    2026-10-21 看了描述统计才挑出来的，状态固定「观察」，等 rule_forward.py 的前向记录攒够再改。"""
    try:
        import index_open_test as T
        x = T.build(code, qty, p, T.index_gaps())
    except Exception:
        return []
    s = x[['相对板块', '买y', '成本', '年']].dropna()
    years = sorted(s['年'].unique())
    out = []
    for i, y in enumerate(years):
        if i < 3:
            continue
        tr, te = s[s['年'] < y], s[s['年'] == y]
        t = te[te['相对板块'] <= tr['相对板块'].quantile(0.2)]
        out.append(t.assign(net=t['买y'] - t['成本']))
    if not out:
        return []
    t = pd.concat(out)
    n = t['net']
    ys = t.groupby('年')['net'].mean()
    return [{'规则': '相对板块弱开买（开盘买、收盘卖）', '状态': '观察', '笔数': len(n), '每笔净%': r2(n.mean()),
             't': r2(n.mean() / n.std() * np.sqrt(len(n))) if len(n) > 2 else None,
             '赚钱年数': '%d/%d' % ((ys > 0).sum(), len(ys))}]


def sim_feedback(code):
    path = os.path.join(SIM_DIR, 'trades.csv')
    out = {}
    if not os.path.exists(path):
        return out
    t = pd.read_csv(path, encoding='utf-8-sig', dtype={'日期': str})
    t = t[(t['模式'] == 'live') & (t['代码'] == code)]
    for side, name in (('sell_first', '正T'), ('buy_first', '反T')):
        x = t[t['方向'] == name]
        n = len(x)
        out[side] = {'轮数': n, '毛利': r2(x['毛利'].sum()), '净利': r2(x['净利'].sum()),
                     '止损轮数': int((x['原因'] == '止损').sum()),
                     '结论': ('关掉（%d 轮净亏）' % n if n >= SIM_MIN_ROUNDS and x['净利'].sum() < 0 else
                            ('保留（%d 轮净赚）' % n if n >= SIM_MIN_ROUNDS else '样本不足（%d/%d 轮），先不动' % (n, SIM_MIN_ROUNDS)))}
    return out


# ---------------------------------------------------------------- 建议设置

def latest_base(code):
    path = os.path.join(SIM_DIR, 'daily.csv')
    if not os.path.exists(path):
        return None
    d = pd.read_csv(path, encoding='utf-8-sig', dtype={'日期': str})
    d = d[d['代码'] == code]
    return int(d.sort_values('日期')['底仓'].iloc[-1]) if len(d) else None


def suggest(t, base, p, loss_limit, fb):
    px = t['股价']
    out, notes = {}, []
    sides = [s for s in ('sell_first', 'buy_first') if not fb.get(s, {}).get('结论', '').startswith('关掉')]
    out['sides'] = sides
    for s in ('sell_first', 'buy_first'):
        if s not in sides:
            notes.append('模拟盘反馈关掉 %s' % SIDE_NAME[s])
    if not base:
        notes.append('不知道底仓，股数不给建议（--base 代码=股数）')
        return out, notes
    adverse = max(t['开盘后不利波动%_近60'] or 1.0, 0.3) / 100
    q_ratio = base * p['qty_ratio']
    q_cost = np.ceil(MIN_NOTIONAL / px / 100) * 100
    q_risk = loss_limit / (px * adverse)
    q_cap = base * 0.5
    want = max(q_ratio, q_cost)
    q = min(want, q_risk, q_cap)
    q = int(q // 100 * 100)
    if q_cost > min(q_risk, q_cap):
        notes.append('成本和风险冲突：每笔凑够 %d 元要 %d 股，但亏损上限 %d 元只允许 %d 股、底仓一半 %d 股 → 取 %d 股，'
                     '最低佣金占比会偏高' % (MIN_NOTIONAL, q_cost, loss_limit, q_risk, q_cap, q))
    out['qty'] = q
    notional = q * px
    if q > 0:
        c = 2 * max(notional * p['commission'], p['min_commission']) + notional * (p['stamp_tax'] + 2 * p['transfer_fee'])
        out['_来回成本%'] = r2(c / notional * 100 + 2 * 0.5 * t['一个价位%'])
    notes.append('股数：底仓 %d × %.0f%% = %d；凑 3 万元要 %d；风险上限 %d（亏损上限 %d ÷ 股价 %.2f ÷ 不利波动 %.2f%%）；'
                 '底仓一半 %d → 建议 %d 股' % (base, p['qty_ratio'] * 100, q_ratio, q_cost, q_risk, loss_limit, px,
                                         adverse * 100, q_cap, q))
    return out, notes


# ---------------------------------------------------------------- 主流程

def build(code, p, base, loss_limit):
    from qmt_bridge.dat_reader import read_daily
    d, lag = read_daily(code)
    d = d[d['volume'] > 0]
    t = daily_traits(d)
    it = intraday_traits(code)
    fb = sim_feedback(code)
    ov, notes = suggest(t, base, p, loss_limit, fb)
    notional = max(MIN_NOTIONAL, (ov.get('qty') or 0) * t['股价'])
    ledger = gap_ledger(d, p, notional)
    rel = rel_sector_ledger(code, p, ov.get('qty') or 100)
    try:
        from rule_forward import summary as fwd_summary
        fwd = fwd_summary(code).round(2).to_dict('records')
    except Exception:
        fwd = []
    prof = {
        '代码': code, '更新于': dt.datetime.now().strftime('%Y-%m-%d %H:%M'), '日线截至': d.index[-1],
        '日线滞后天数': lag, '底仓': base, '每日亏损上限': loss_limit, '参数版本': p['version'],
        '股性': t, '日内': it, '性格': character(t, it),
        '规则账本': {'开盘缺口': ledger, '相对板块': rel, '模拟盘反馈': fb, '前向记录': fwd},
        't0_overrides': {k: v for k, v in ov.items() if not k.startswith('_')},
        '建议说明': notes, '来回成本%': ov.get('_来回成本%'),
    }
    os.makedirs(PROF_DIR, exist_ok=True)
    with open(os.path.join(PROF_DIR, '%s.json' % code), 'w', encoding='utf-8') as f:
        json.dump(prof, f, ensure_ascii=False, indent=2)
    hist = {'日期': dt.date.today().strftime('%Y%m%d'), '代码': code, '股价': t['股价'],
            '振幅近20': t['振幅%_近20'], '振幅长期': t['振幅%_长期'], '开到高中位近60': t['开盘到最高%_中位_近60'],
            '开到低中位近60': t['开盘到最低%_中位_近60'], '缺口与开收相关': t['缺口与开收相关'],
            '先高后低%': it.get('先高后低%'), '日内样本天数': it.get('日内样本天数'),
            '建议股数': ov.get('qty'), '允许方向': '/'.join(ov.get('sides', [])),
            '可模拟规则数': sum(r['状态'] == '可模拟' for r in ledger), '否决规则数': sum(r['状态'] == '否决' for r in ledger)}
    old = pd.read_csv(HIST_FILE, encoding='utf-8-sig', dtype=str) if os.path.exists(HIST_FILE) else None
    new = pd.DataFrame([hist]).astype(str)
    if old is not None:
        old = old[~((old['日期'] == hist['日期']) & (old['代码'] == code))]
        new = pd.concat([old, new])
    new.to_csv(HIST_FILE, index=False, encoding='utf-8-sig')
    return prof


def show(prof):
    t, it = prof['股性'], prof['日内']
    print('\n' + '=' * 90)
    print('%s 股性卡（%s 更新，日线截至 %s%s）' % (prof['代码'], prof['更新于'], prof['日线截至'],
                                         '，日线滞后 %d 天，去 QMT 补数据' % prof['日线滞后天数'] if (prof['日线滞后天数'] or 0) > 3 else ''))
    print('=' * 90)
    for s in prof['性格']:
        print('  · ' + s)
    print('  价格 %.2f，一个价位 %.2f%%，近 20 日日均成交额 %.0f 万；近一年涨跌停 %d 天' % (
        t['股价'], t['一个价位%'], t['近20日日均成交额万'], t['近一年涨跌停天数']))
    print('  日内（%d 天 5 分钟线）：先高后低 %s%%，上午高点在开盘 15 分钟内 %s%%' % (
        it.get('日内样本天数', 0), it.get('先高后低%'), it.get('上午高点在前15分钟%')))
    print('  挂单成交概率（近 60 天 / 长期）：' + '  '.join(
        '+%g%% %.0f/%.0f  -%g%% %.0f/%.0f' % (x, t['最高≥开+%g%%_近60' % x], t['最高≥开+%g%%_长期' % x],
                                              x, t['最低≤开-%g%%_近60' % x], t['最低≤开-%g%%_长期' % x]) for x in (0.5, 1, 2)))
    led = prof['规则账本']['开盘缺口']
    ok = [r for r in led if r['状态'] == '可模拟']
    no = [r for r in led if r['状态'] == '否决']
    print('  规则账本（开盘缺口，日线长样本）：可模拟 %d 条，否决 %d 条，观察 %d 条' % (len(ok), len(no), len(led) - len(ok) - len(no)))
    for r in ok:
        print('    可模拟  %s：%d 天 净 %+.2f%% t %+.1f（前/后半 %+.2f/%+.2f）' % (r['规则'], r['天数'], r['净%'], r['t'], r['前半净%'], r['后半净%']))
    for r in no:
        print('    否决    %s：%d 天 净 %+.2f%% t %+.1f' % (r['规则'], r['天数'], r['净%'], r['t']))
    for r in prof['规则账本'].get('相对板块', []):
        print('    %s    %s：按年滚动样本外 %d 笔 每笔净 %+.2f%% t %+.1f，赚钱年数 %s' % (
            r['状态'], r['规则'], r['笔数'], r['每笔净%'], r['t'], r['赚钱年数']))
    for r in prof['规则账本'].get('前向记录', []):
        print('  前向记录 %s：%d 天里触发 %d 次，净 %+.0f 元（每次 %+.0f，赚 %d 次）' % (
            r['规则'], r['记录天数'], r['触发次数'], r['净利合计'], r['每次平均'], r['赚钱次数']))
    for side, f in prof['规则账本']['模拟盘反馈'].items():
        print('  模拟盘 %s：%d 轮 毛 %+.0f 净 %+.0f（止损 %d）→ %s' % (SIDE_NAME[side], f['轮数'], f['毛利'] or 0,
                                                           f['净利'] or 0, f['止损轮数'], f['结论']))
    print('  建议设置（模拟盘自动使用）：%s，来回成本约 %s%%' % (json.dumps(prof['t0_overrides'], ensure_ascii=False), prof['来回成本%']))
    for n in prof['建议说明']:
        print('    ' + n)


def load_overrides(code):
    """t0_sim 用：档案里的 t0_overrides，再叠 manual.json 的手工覆盖。没有就返回 {}。"""
    out = {}
    path = os.path.join(PROF_DIR, '%s.json' % code)
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            out.update(json.load(f).get('t0_overrides', {}))
    if os.path.exists(MANUAL_FILE):
        with open(MANUAL_FILE, encoding='utf-8-sig') as f:
            out.update(json.load(f).get(code, {}))
    return out


def main():
    ap = argparse.ArgumentParser(description='股性档案')
    ap.add_argument('codes', nargs='*', default=DEFAULT_CODES)
    ap.add_argument('--base', nargs='*', default=[], help='底仓，形如 600366=4000；不给就用模拟盘最近一天的底仓')
    ap.add_argument('--loss', type=float, default=300, help='每只每日亏损上限（元），用来算股数上限，默认 300')
    ap.add_argument('--show', action='store_true', help='只打印现有档案')
    a = ap.parse_args()
    p = load_params()
    bases = {code_of(kv.split('=')[0]): int(kv.split('=')[1]) for kv in a.base}
    for c in a.codes:
        code = code_of(c)
        if a.show:
            path = os.path.join(PROF_DIR, '%s.json' % code)
            if not os.path.exists(path):
                print('%s 还没有档案' % code)
                continue
            with open(path, encoding='utf-8') as f:
                show(json.load(f))
            continue
        show(build(code, p, bases.get(code) or latest_base(code), a.loss))


if __name__ == '__main__':
    main()
