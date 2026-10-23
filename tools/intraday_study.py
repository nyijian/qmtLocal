# coding:utf-8
"""日内价格路径研究：在盘中几个时点，只用「当时已经知道」的信息分组，看之后 15/30/60 分钟和到收盘的涨跌，
扣掉费用和滑点后有没有稳定的优势。用 5 分钟线做原型，tick 攒够了再换。

    .venv313\\Scripts\\python.exe tools\\intraday_study.py                  # 研究：600436 挑规律 + 验证，再拿 600366/605208 验证
    .venv313\\Scripts\\python.exe tools\\intraday_study.py --train 600436 --test 600366 605208

只在 .venv313 跑（用了 3.7+ 写法）。不下单。场景是「底仓做 T」：正T = 先卖底仓再买回，反T = 先买再卖掉底仓，
都在 14:55 前了结，所以两个方向都能做，数量受底仓限制。

决策时点（5 分钟 K 线收完那一刻，用这根的收盘价当成交价）：09:45 10:00 10:30 11:00 13:30 14:00。
每个时点的特征（全是那一刻已知的，没有未来函数）：
  开盘     高开/平开/低开（±0.3%）
  走势     到此刻为止 先高后低 / 先低后高（今天到现在的最高、最低谁先出现）
  区间位置 现价在今天到现在区间的 底部(<25%) / 中部 / 顶部(>75%)
  VWAP     现价在今天到现在的 VWAP 上方 / 下方
  昨价值区 现价在昨天成交密集区（围绕成交最多价位、占 70% 成交量的区间）上方 / 区内 / 下方
  量       最近 3 根的量 / 过去 20 天同一时段的平均：缩量(<0.8) / 平量 / 放量(>1.5)
  星期     周一~周五
  大盘     上证指数今天 高开/平开/低开（±0.2%），以及昨天 涨/跌
  时点     上面那几个时点
结果：之后 15/30/60 分钟、到 14:55 的收益（按反T方向记，正T 取负），以及之后 60 分钟最大有利 / 不利波动。

挑规律（防止「试了几百个组合，总有几个碰巧好看」）：
  1. 规则 = 1~2 个特征条件 × 方向（正T/反T） × 持有到（30 分钟 / 60 分钟 / 14:55）。
  2. 只用训练股票前 60% 的天数挑：同一天多个时点算一个样本（按天聚合再算 t），至少 25 天，扣费后均值 > 0，t ≥ 2.5。
  3. 后 40% 的天数（样本外）和其他股票再检验：扣费后均值仍 > 0 才算通过。
费用 = t0_sim 费率（约 0.15% 来回）+ 滑点（每边 1 个价位，10 元的股票每边就是 0.1%）。
结果写 intraday_study/：events_<代码>.csv（每个时点一行）、rules.csv（所有规则的训练/检验表现）。
2026-10-21 首轮：单个特征有均值回归（区间顶部/VWAP 上方/昨价值区上方之后偏跌，反之偏涨，t 3~4.7），
但幅度 0.1~0.3%，扣万5佣金+滑点后没有一条规则在样本外站得住。等有规则通过了再加盘中提示。
"""
import argparse
import itertools
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from t0_sim import DATA_DIR, load_params  # noqa: E402
from day_pattern import load_bars, load_preclose, profile, value_area, WEEK  # noqa: E402

OUT_DIR = os.path.join(DATA_DIR, 'intraday_study')
CHECKPOINTS = ['0945', '1000', '1030', '1100', '1330', '1400']
FEATURES = ['开盘', '走势', '区间位置', 'VWAP', '昨价值区', '量', '星期', '大盘开盘', '大盘昨日', '时点']
HOLDS = {'30分钟': 'r30', '60分钟': 'r60', '到14:55': 'rclose'}
TICK = 0.01


# ---------------------------------------------------------------- 特征

def index_env():
    """上证指数日线：今天开盘缺口、昨天涨跌。开盘那一刻就知道。"""
    try:
        from qmt_bridge.dat_reader import read_daily
        d, _ = read_daily('000001.SH')
    except Exception:
        return {}
    gap = (d['open'] / d['preClose'] - 1) * 100
    prev_ret = (d['close'] / d['preClose'] - 1).shift(1) * 100
    out = {}
    for day in d.index:
        g, p = gap.get(day), prev_ret.get(day)
        out[day] = ('高开' if g > 0.2 else ('低开' if g < -0.2 else '平开'),
                    '-' if pd.isna(p) else ('涨' if p > 0 else '跌'))
    return out


def bar_slot(hhmm):
    return hhmm  # 5 分钟 K 线的结束时刻就是同一时段的键


def build_events(code, df, preclose, env, flat=0.3):
    days = sorted(set(df.index.str[:8]))
    by_day = {d: df[df.index.str[:8] == d] for d in days}
    vol_hist = {}                               # 时段 -> 过去每天这一时段的量（滚动 20 天）
    rows = []
    prev = None
    for d in days:
        b = by_day[d]
        hhmm = np.array(b.index.str[8:12])
        if prev is None or len(b) < 45:
            prev = b
            continue
        pc = preclose.get(d, prev['close'].iloc[-1])
        o = b['open'].iloc[0]
        gap = (o / pc - 1) * 100
        step = max(0.01, round(pc * 0.001, 2))
        e, v = profile(prev, step)
        poc, val, vah = value_area(e, v)
        cum_v = b['volume'].cumsum().values
        cum_a = (b['amount'] if 'amount' in b else b['close'] * b['volume']).cumsum().values
        hi_run = b['high'].cummax().values
        lo_run = b['low'].cummin().values
        ih_run = pd.Series(b['high'].values).expanding().apply(lambda x: int(np.argmax(x)), raw=True).values
        il_run = pd.Series(b['low'].values).expanding().apply(lambda x: int(np.argmin(x)), raw=True).values
        closes = b['close'].values
        vols = b['volume'].values
        i1455 = int(np.where(hhmm <= '1455')[0][-1])
        env_g, env_p = env.get(d, ('-', '-'))
        for cp in CHECKPOINTS:
            idx = np.where(hhmm == cp)[0]
            if not len(idx):
                continue
            i = int(idx[0])
            px = closes[i]
            # 量：最近 3 根 vs 过去 20 天同时段平均
            slots = hhmm[max(0, i - 2):i + 1]
            hist = [np.mean(vol_hist[s][-20:]) for s in slots if s in vol_hist and len(vol_hist[s]) >= 10]
            rv = vols[max(0, i - 2):i + 1].sum() / sum(hist) if len(hist) == len(slots) and sum(hist) > 0 else np.nan
            # VWAP：amount 单位是元、volume 是手，比值是 100×价格，只用来比较大小，统一除掉
            vwap = cum_a[i] / cum_v[i] if cum_v[i] > 0 else px
            vwap = vwap / 100.0 if vwap > px * 50 else vwap
            rng = hi_run[i] - lo_run[i]
            pos = (px - lo_run[i]) / rng if rng > 0 else 0.5

            def fwd(n):
                j = min(i + n, i1455)
                return (closes[j] / px - 1) * 100 if j > i else np.nan
            j60 = min(i + 12, i1455)
            seg = b.iloc[i + 1:j60 + 1]
            rows.append({
                '代码': code, '日期': d, '时点': cp, '价': px,
                '开盘': '高开' if gap > flat else ('低开' if gap < -flat else '平开'),
                '走势': '先高后低' if ih_run[i] < il_run[i] else ('先低后高' if il_run[i] < ih_run[i] else '同根'),
                '区间位置': '底部' if pos < 0.25 else ('顶部' if pos > 0.75 else '中部'),
                'VWAP': '上方' if px >= vwap else '下方',
                '昨价值区': '上方' if px > vah else ('下方' if px < val else '区内'),
                '量': '-' if np.isnan(rv) else ('缩量' if rv < 0.8 else ('放量' if rv > 1.5 else '平量')),
                '星期': WEEK[pd.Timestamp(d).weekday()],
                '大盘开盘': env_g, '大盘昨日': env_p,
                'r15': fwd(3), 'r30': fwd(6), 'r60': fwd(12),
                'rclose': (closes[i1455] / px - 1) * 100 if i1455 > i else np.nan,
                '上MFE60': (seg['high'].max() / px - 1) * 100 if len(seg) else np.nan,
                '下MFE60': (seg['low'].min() / px - 1) * 100 if len(seg) else np.nan,
                '滑点%': TICK / px * 100,
            })
        for s, vv in zip(hhmm, vols):
            vol_hist.setdefault(s, []).append(vv)
        prev = b
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 统计

def day_stats(x, col, sign, cost):
    """同一天多个时点算一个样本：先按天平均，再算均值和 t。"""
    s = (x[col] * sign - cost - 2 * x['滑点%']).groupby(x['日期']).mean().dropna()
    if len(s) < 2 or s.std() == 0:
        return len(s), (s.mean() if len(s) else np.nan), np.nan, np.nan
    return len(s), s.mean(), s.mean() / s.std() * np.sqrt(len(s)), (s > 0).mean() * 100


def rule_iter(ev):
    vals = {f: sorted(v for v in ev[f].unique() if v != '-') for f in FEATURES}
    singles = [((f, v),) for f in FEATURES for v in vals[f]]
    yield from singles
    for f1, f2 in itertools.combinations(FEATURES, 2):
        for v1 in vals[f1]:
            for v2 in vals[f2]:
                yield ((f1, v1), (f2, v2))


def mask(ev, rule):
    m = np.ones(len(ev), dtype=bool)
    for f, v in rule:
        m &= (ev[f] == v).values
    return m


def search(train, tests, cost, min_days=25, t_min=2.5):
    rows, n_tested = [], 0
    for rule in rule_iter(train):
        m = mask(train, rule)
        if m.sum() < min_days:
            continue
        sub = train[m]
        for hold, col in HOLDS.items():
            for side, sign in (('反T', 1), ('正T', -1)):
                n_tested += 1
                n, mean, t, win = day_stats(sub, col, sign, cost)
                if n < min_days or not (mean > 0) or not (t >= t_min):
                    continue
                r = {'规则': ' & '.join('%s=%s' % fv for fv in rule), '方向': side, '持有': hold,
                     '训练天数': n, '训练净%': mean, '训练t': t, '训练胜率': win}
                for name, te in tests.items():
                    tm = mask(te, rule)
                    tn, tmean, tt, twin = day_stats(te[tm], col, sign, cost) if tm.sum() else (0, np.nan, np.nan, np.nan)
                    r[name + '天数'], r[name + '净%'], r[name + 't'] = tn, tmean, tt
                rows.append(r)
    return pd.DataFrame(rows), n_tested


def univariate(ev, cost):
    """每个特征单独看：反T 毛收益（正数 = 之后涨），按天聚合。"""
    out = []
    for f in FEATURES:
        for v, x in ev.groupby(f):
            if v == '-':
                continue
            row = {'特征': f, '取值': v, '天数': x['日期'].nunique(), '时点数': len(x)}
            for col, name in (('r15', '15分'), ('r30', '30分'), ('r60', '60分'), ('rclose', '到收盘')):
                s = x[col].groupby(x['日期']).mean().dropna()
                row[name + '%'] = s.mean()
                row[name + 't'] = s.mean() / s.std() * np.sqrt(len(s)) if len(s) > 2 and s.std() > 0 else np.nan
            row['上MFE60'] = x['上MFE60'].mean()
            row['下MFE60'] = x['下MFE60'].mean()
            out.append(row)
    return pd.DataFrame(out)


# ---------------------------------------------------------------- 入口

def code_of(c):
    return c.upper() if '.' in c else c + ('.SH' if c[0] in '659' else '.SZ')


def load_events(code, env):
    df = load_bars(code, use_sina=True)
    if df is None or not len(df):
        print('%s 没有 5 分钟线' % code)
        return None
    ev = build_events(code, df, load_preclose(code), env)
    ev.to_csv(os.path.join(OUT_DIR, 'events_%s.csv' % code), index=False, encoding='utf-8-sig')
    return ev


def cmd_study(a):
    p = load_params()
    cost = (p['commission'] * 2 + p['stamp_tax'] + p['transfer_fee'] * 2) * 100
    os.makedirs(OUT_DIR, exist_ok=True)
    env = index_env()
    pd.set_option('display.unicode.east_asian_width', True)
    pd.set_option('display.width', 220)
    pd.set_option('display.max_rows', 200)

    train_code = code_of(a.train)
    ev = load_events(train_code, env)
    days = sorted(ev['日期'].unique())
    cut = days[int(len(days) * 0.6)]
    tr, te = ev[ev['日期'] < cut], ev[ev['日期'] >= cut]
    print('%s：%d 天、%d 个决策时点。训练 %s~%s（%d 天），样本外 %s~%s（%d 天）' % (
        train_code, len(days), len(ev), days[0], tr['日期'].max(), tr['日期'].nunique(),
        cut, days[-1], te['日期'].nunique()))
    print('来回费用 %.3f%% + 滑点每边 1 个价位（%s 约 %.3f%%/边）' % (cost, train_code, ev['滑点%'].mean()))

    print('\n【单个特征】反T 视角的平均毛收益（正 = 之后涨，适合反T；负 = 之后跌，适合正T），全部天数，按天聚合')
    u = univariate(ev, cost)
    print(u.round(2).to_string(index=False))

    tests = {'样本外': te}
    for c in a.test:
        e2 = load_events(code_of(c), env)
        if e2 is not None and len(e2):
            tests[code_of(c)[:6]] = e2
            print('%s：%d 天（只做检验）' % (code_of(c), e2['日期'].nunique()))

    res, n_tested = search(tr, tests, cost)
    print('\n【规则搜索】训练集一共试了 %d 个（规则×方向×持有）组合；按 t≥2.5 碰巧过关的期望个数约 %.0f 个，'
          '实际过关 %d 个' % (n_tested, n_tested * 0.006, len(res)))
    if not len(res):
        print('没有规则在训练集过关。')
        return
    res['样本外通过'] = res['样本外净%'] > 0
    res = res.sort_values(['样本外通过', '训练t'], ascending=False)
    res.to_csv(os.path.join(OUT_DIR, 'rules.csv'), index=False, encoding='utf-8-sig')
    ok = res[res['样本外通过'] & (res['样本外天数'] >= 10)]
    print('样本外（%s 后 40%% 天数）扣费后仍为正、且样本外至少 10 天的：%d / %d' % (train_code, len(ok), len(res)))
    if len(ok):
        print(ok.round(2).head(30).to_string(index=False))
    bad = res[~res['样本外通过']]
    if len(bad):
        print('\n训练过关但样本外变负的（前 10 个）：')
        print(bad.round(2).head(10).to_string(index=False))


def main():
    ap = argparse.ArgumentParser(description='日内价格路径研究（5 分钟线原型）')
    ap.add_argument('--train', default='600436', help='挑规律用的股票（要有长历史）')
    ap.add_argument('--test', nargs='*', default=['600366', '605208'], help='只做检验的股票')
    a = ap.parse_args()
    cmd_study(a)


if __name__ == '__main__':
    main()
