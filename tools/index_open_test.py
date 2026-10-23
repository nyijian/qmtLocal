# coding:utf-8
"""看各指数开盘情况决定今天「开盘买、收盘卖」还是「开盘卖、收盘买回」，有没有规律、能不能赚钱。

    .venv313\\Scripts\\python.exe tools\\index_open_test.py                        # 默认 600366=800 605208=600 600436=100
    .venv313\\Scripts\\python.exe tools\\index_open_test.py 600366=4000            # 换股数（每笔金额影响最低 5 元佣金占比）

只在 .venv313 跑（用了 3.7+ 写法）。不下单。

执行假设（跟实盘能做到的一致）：指数和个股开盘价都是 09:25 集合竞价同时出来的，看到指数开盘再决定，
就只能在 09:30 连续竞价按对手价进场——进场价 = 个股开盘价 ± 半个价位（买加、卖减）；收盘走集合竞价按收盘价出。
费用按 sim_t0/params.json（万0.8、最低 5 元、印花税、过户费）按实际股数算。

开盘情况（都在 09:25 就知道）：
  8 个指数各自的开盘缺口：上证、深成、创业板、沪深300、中证500、中证1000、科创50、上证50；
  个股所属行业板块指数的缺口（东方财富，akshare/sector/ 缓存，factor_model.py 下的）；
  普涨普跌：8 个指数里高开（> +0.1%）的个数；大小盘分化：上证50 缺口 - 中证1000 缺口；
  个股相对缺口：个股缺口 - 上证缺口、个股缺口 - 板块缺口。

两步：
  1. 描述：按缺口分档（≤-1% / -1~-0.3% / ±0.3% / 0.3~1% / >1%；组合指标按训练期五分位），个股「开→收」平均、t、逐年同号。
  2. 模拟真实使用（按年滚动）：每年开始时只用之前的数据，算出每一档的平均「开→收」；
     平均涨幅 > 当档成本 → 这一档开盘买；平均跌幅 > 成本 → 开盘卖；否则不做。拿这套规则做下一年，扣费后跟
     固定开盘买 / 固定开盘卖 / 不做比。试了十几个开盘指标 × 3 只，t ≥ 2 的里面平均就有一两个是碰巧的，要看逐年。
结果写共享数据目录 index_open/。
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

OUT_DIR = os.path.join(DATA_DIR, 'index_open')
INDEXES = {'000001.SH': '上证', '399001.SZ': '深成', '399006.SZ': '创业板', '000300.SH': '沪深300',
           '000905.SH': '中证500', '000852.SH': '中证1000', '000688.SH': '科创50', '000016.SH': '上证50'}
SECTORS = {'600366.SH': '金属新材料', '605208.SH': '工业金属', '600436.SH': '中药Ⅱ'}
GAP_BINS = [-99, -1, -0.3, 0.3, 1, 99]
GAP_LABELS = ['≤-1%', '-1~-0.3%', '±0.3%', '0.3~1%', '>1%']
DEFAULT = ['600366=800', '605208=600', '600436=100']


def code_of(c):
    c = c.strip().upper()
    return c if '.' in c else c + ('.SH' if c[0] in '659' else '.SZ')


def fees_pct(p, amount):
    return (2 * max(amount * p['commission'], p['min_commission']) + amount * (p['stamp_tax'] + 2 * p['transfer_fee'])) / amount * 100


def index_gaps():
    from qmt_bridge.dat_reader import read_daily
    g = {}
    for c, n in INDEXES.items():
        d, _ = read_daily(c)
        d = d[d['volume'] > 0]
        g[n] = (d['open'] / d['preClose'] - 1) * 100
    g = pd.DataFrame(g)
    g['高开指数个数'] = (g[list(INDEXES.values())] > 0.1).sum(axis=1).where(g['上证'].notna())
    g['大小盘分化'] = g['上证50'] - g['中证1000']
    return g


def sector_gap(code):
    path = os.path.join(DATA_DIR, 'akshare', 'sector', '%s.csv' % SECTORS.get(code, ''))
    if not os.path.exists(path):
        return None
    s = pd.read_csv(path, dtype={'日期': str}, encoding='utf-8-sig').set_index('日期')
    return (s['开盘'] / s['收盘'].shift(1) - 1) * 100


def build(code, qty, p, ig):
    from qmt_bridge.dat_reader import read_daily
    d, _ = read_daily(code)
    d = d[(d['volume'] > 0) & (d['high'] > d['low']) & (d.index >= '20150101')].copy()
    x = ig.reindex(d.index).copy()
    x['板块'] = sector_gap(code).reindex(d.index) if sector_gap(code) is not None else np.nan
    gap = (d['open'] / d['preClose'] - 1) * 100
    x['相对上证'] = gap - x['上证']
    x['相对板块'] = gap - x['板块']
    half_tick = 0.005
    # 先买：进场价 = 开盘 + 半个价位；先卖：进场价 = 开盘 - 半个价位；出场 = 收盘
    x['买y'] = (d['close'] / (d['open'] + half_tick) - 1) * 100
    x['卖y'] = ((d['open'] - half_tick) / d['close'] - 1) * 100
    x['成本'] = [fees_pct(p, qty * o) for o in d['open']]
    x['开到收'] = (d['close'] / d['open'] - 1) * 100
    x['年'] = d.index.str[:4]
    x['代码'] = code
    return x


def bucket(s, feat, edges=None):
    if feat in INDEXES.values() or feat == '板块':
        return pd.cut(s, GAP_BINS, labels=GAP_LABELS)
    if feat == '高开指数个数':
        return pd.cut(s, [-1, 0, 2, 5, 7, 8], labels=['0个', '1~2个', '3~5个', '6~7个', '8个'])
    return pd.cut(s, edges, labels=['最低20%', '20~40%', '40~60%', '60~80%', '最高20%'], include_lowest=True)


FEATS = list(INDEXES.values()) + ['板块', '高开指数个数', '大小盘分化', '相对上证', '相对板块']


def describe(x, feat):
    s = x[[feat, '开到收', '年']].dropna()
    if len(s) < 200:
        return None
    edges = None
    if feat not in INDEXES.values() and feat not in ('板块', '高开指数个数'):
        edges = list(np.quantile(s[feat], [0, 0.2, 0.4, 0.6, 0.8, 1]))
        edges[0], edges[-1] = -1e9, 1e9
    s['档'] = bucket(s[feat], feat, edges)
    rows = []
    for b, g in s.groupby('档', observed=True):
        if len(g) < 20:
            continue
        yr = g.groupby('年')['开到收'].mean()
        m = g['开到收'].mean()
        rows.append({'档': b, '天数': len(g), '开到收%': m, 't': m / g['开到收'].std() * np.sqrt(len(g)),
                     '收阳率%': (g['开到收'] > 0).mean() * 100,
                     '同号年数': '%d/%d' % ((np.sign(yr) == np.sign(m)).sum(), len(yr))})
    return pd.DataFrame(rows)


def walk(x, feat, min_years=3):
    """按年滚动：用之前的年份算每一档的平均开→收，定规则，做下一年。"""
    s = x[[feat, '买y', '卖y', '成本', '开到收', '年']].dropna()
    years = sorted(s['年'].unique())
    trades = []
    for i, y in enumerate(years):
        if i < min_years:
            continue
        tr, te = s[s['年'] < y], s[s['年'] == y].copy()
        edges = None
        if feat not in INDEXES.values() and feat not in ('板块', '高开指数个数'):
            edges = list(np.quantile(tr[feat], [0, 0.2, 0.4, 0.6, 0.8, 1]))
            edges[0], edges[-1] = -1e9, 1e9
        tr = tr.assign(档=bucket(tr[feat], feat, edges))
        te['档'] = bucket(te[feat], feat, edges)
        stat = tr.groupby('档', observed=True).agg(m=('开到收', 'mean'), n=('开到收', 'size'), c=('成本', 'mean'))
        rule = {b: (1 if r['m'] > r['c'] else (-1 if r['m'] < -r['c'] else 0)) if r['n'] >= 20 else 0
                for b, r in stat.iterrows()}
        te['side'] = te['档'].map(rule).astype(float).fillna(0)
        t = te[te['side'] != 0].copy()
        t['net'] = np.where(t['side'] > 0, t['买y'], t['卖y']) - t['成本']
        trades.append(t)
    if not trades:
        return None, 0
    t = pd.concat(trades)
    test_days = s[s['年'] >= years[min_years]].shape[0] if len(years) > min_years else 0
    return t, test_days


def main():
    ap = argparse.ArgumentParser(description='看指数开盘决定开盘买还是开盘卖')
    ap.add_argument('codes', nargs='*', default=DEFAULT, help='代码=每次股数')
    a = ap.parse_args()
    p = load_params()
    ig = index_gaps()
    pd.set_option('display.unicode.east_asian_width', True)
    pd.set_option('display.width', 220)
    os.makedirs(OUT_DIR, exist_ok=True)
    allrows = []
    for c in a.codes:
        code, _, q = c.partition('=')
        code, qty = code_of(code), int(q or 100)
        x = build(code, qty, p, ig)
        print('\n' + '=' * 100)
        from qmt_bridge.dat_reader import read_daily
        opens = read_daily(code)[0]['open'].reindex(x.index)
        print('%s 每次 %d 股，%s~%s 共 %d 天，平均费用 %.3f%% + 进场半个价位（约 %.3f%%）' % (
            code, qty, x.index[0], x.index[-1], len(x), x['成本'].mean(), (0.005 / opens).mean() * 100))
        print('=' * 100)
        # 对照组（滚动期内）
        base_x = x[x['年'] >= sorted(x['年'].unique())[3]]
        b_buy = base_x['买y'] - base_x['成本']
        b_sell = base_x['卖y'] - base_x['成本']
        print('  对照（%s 年起）：固定开盘买 每天 %+.3f%%（t %+.1f）｜固定开盘卖 %+.3f%%（t %+.1f）｜不做 0' % (
            base_x['年'].min(), b_buy.mean(), b_buy.mean() / b_buy.std() * np.sqrt(len(b_buy)),
            b_sell.mean(), b_sell.mean() / b_sell.std() * np.sqrt(len(b_sell))))
        rows = []
        for f in FEATS:
            t, n_days = walk(x, f)
            if t is None or not n_days:
                continue
            net = t['net']
            ysum = t.groupby('年')['net'].sum()
            rows.append({'开盘指标': f, '交易天': len(t), '占比%': len(t) / n_days * 100,
                         '每笔净%': net.mean() if len(t) else np.nan,
                         't': net.mean() / net.std() * np.sqrt(len(net)) if len(t) > 2 and net.std() > 0 else np.nan,
                         '胜率%': (net > 0).mean() * 100 if len(t) else np.nan,
                         '摊到每天%': net.sum() / n_days,
                         '赚钱年数': '%d/%d' % ((ysum > 0).sum(), len(ysum)),
                         '先买占比%': (t['side'] > 0).mean() * 100 if len(t) else np.nan})
        r = pd.DataFrame(rows).sort_values('摊到每天%', ascending=False)
        r['代码'] = code
        allrows.append(r)
        print('\n  【按年滚动的规则，样本外】每一年都只用之前的数据定「哪一档买、哪一档卖、哪一档不做」：')
        print('  ' + r.drop(columns='代码').round(3).to_string(index=False).replace('\n', '\n  '))
        best = r.iloc[0]['开盘指标']
        print('\n  【描述】样本外最好的「%s」，按档看个股开→收（全部年份，扣费前）：' % best)
        dsc = describe(x, best)
        if dsc is not None:
            print('  ' + dsc.round(3).to_string(index=False).replace('\n', '\n  '))
        print('\n  【描述】上证指数开盘缺口分档：')
        print('  ' + describe(x, '上证').round(3).to_string(index=False).replace('\n', '\n  '))
    pd.concat(allrows).to_csv(os.path.join(OUT_DIR, 'walk_forward.csv'), index=False, encoding='utf-8-sig')


if __name__ == '__main__':
    main()
