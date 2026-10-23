# coding:utf-8
"""当日复盘对照：开盘、日内结构、价格路径、时间、量价分布五类 12 项，今天 / 昨天 / 一周前（5 个交易日前）并排比，附历史参照。

    .venv313\\Scripts\\python.exe tools\\day_compare.py                    # 最近一个存过 tick 的交易日，默认三只
    .venv313\\Scripts\\python.exe tools\\day_compare.py 20261021 600366

只在 .venv313 跑（用了 3.7+ 写法）。不下单。

数据：5 分钟线（QMT 本地 .DAT + 桥取的 + 新浪缓存，factor_model.bars_5m）给三天的价格路径；
      当天有 tick（tools/tick_archive.py）就用 tick 精确算高低点时刻和各价位成交量（逐笔成交量差分，记在成交价上）；
      没 tick 的日子各价位成交量用 5 分钟线近似（每根的量均匀摊到它的最低~最高价），会标「≈」。
      历史参照：日线 2015 年起（缺口档、星期），5 分钟线全部可用天数（高低点时段概率、路径类型分布；600436 约 800 天，另两只约两个月）。

12 项：
  开盘    1 高开 / 低开 / 平开（±0.3%）  2 缺口幅度 + 历史同档的开→收平均和收阴率
  结构    3 开盘到最高、开盘到最低的距离  4 收盘在当日区间的位置（0 = 最低，1 = 最高）  5 振幅
  路径    6 先高后低 / 先低后高  7 最高、最低出现的时刻  8 路径类型：直接上涨 / 直接下跌 / 先涨后跌 / 先跌后涨 / 多次反转
          （用 5 分钟收盘价数折返：回撤超过当天振幅 30% 算一次折返）
  时间    9 星期几 + 历史同星期的开→收、振幅  10 高 / 低点所在时段的历史概率，折返多出现在哪些时段
  量价    11 各价位成交量：成交最多的价位（POC）、成交密集区（围绕 POC 占 70% 的区间）、VWAP、成交量最大的 5 个价位
          12 跟昨天、一周前比：总量之比、POC / VWAP 迁移、密集区上移 / 下移 / 重叠、两天分布的重合度、今天落在昨天密集区里的量占比
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
WEEK = ['周一', '周二', '周三', '周四', '周五']
SLOTS = [('0930', '1000', '09:30-10:00'), ('1000', '1030', '10:00-10:30'), ('1030', '1130', '10:30-11:30'),
         ('1300', '1400', '13:00-14:00'), ('1400', '1500', '14:00-15:00')]


def code_of(c):
    c = c.strip().upper()
    return c if '.' in c else c + ('.SH' if c[0] in '659' else '.SZ')


def slot_of(hhmm):
    for a, b, name in SLOTS:
        if a < hhmm <= b or (a == '0930' and hhmm <= b):
            return name
    return SLOTS[-1][2]


# ---------------------------------------------------------------- 单日指标

def zigzag_legs(closes, th):
    """数折返：从起点出发，价格反向走超过 th（绝对价）就算一次转向。返回转向点下标列表。"""
    turns, direction, ext_i = [], 0, 0
    for i in range(1, len(closes)):
        if direction == 0:
            if closes[i] - closes[0] >= th:
                direction, ext_i = 1, i
            elif closes[0] - closes[i] >= th:
                direction, ext_i = -1, i
            continue
        if direction == 1:
            if closes[i] >= closes[ext_i]:
                ext_i = i
            elif closes[ext_i] - closes[i] >= th:
                turns.append(ext_i)
                direction, ext_i = -1, i
        else:
            if closes[i] <= closes[ext_i]:
                ext_i = i
            elif closes[i] - closes[ext_i] >= th:
                turns.append(ext_i)
                direction, ext_i = 1, i
    return turns


def path_type(b, turns):
    o = b['open'].iloc[0]
    hi, lo = b['high'].max(), b['low'].min()
    ih, il = int(np.argmax(b['high'].values)), int(np.argmin(b['low'].values))
    n = len(b)
    if len(turns) >= 3:
        return '多次反转（%d 次折返）' % len(turns)
    if (lo - o) / o > -0.003 and ih > n * 0.6 and len(turns) <= 1:
        return '直接上涨'
    if (hi - o) / o < 0.003 and il > n * 0.6 and len(turns) <= 1:
        return '直接下跌'
    return ('先涨后跌' if ih < il else '先跌后涨') + ('（%d 次折返）' % len(turns) if turns else '')


def day_metrics(b, pc, ticks=None):
    """b：当天 5 分钟线；pc：昨收；ticks：当天 tick（有的话更精确）。"""
    o, c = b['open'].iloc[0], b['close'].iloc[-1]
    hi, lo = b['high'].max(), b['low'].min()
    hm = b.index.str[8:12]
    ih, il = int(np.argmax(b['high'].values)), int(np.argmin(b['low'].values))
    t_hi, t_lo, exact = hm[ih], hm[il], False
    if ticks is not None:
        tc = ticks[(ticks['hms'] >= '092500') & (ticks['lastPrice'] > 0)]
        t_hi = tc.loc[tc['lastPrice'].idxmax(), 'hms'][:4]
        t_lo = tc.loc[tc['lastPrice'].idxmin(), 'hms'][:4]
        hi, lo = max(hi, tc['lastPrice'].max()), min(lo, tc['lastPrice'].min())
        exact = True
    amp = hi - lo
    turns = zigzag_legs(b['close'].values, max(amp * 0.3, o * 0.002))
    gap = (o / pc - 1) * 100
    return {
        '开盘': o, '昨收': pc, '收盘': c, '最高': hi, '最低': lo,
        '1 开盘类型': '高开' if gap > 0.3 else ('低开' if gap < -0.3 else '平开'), '2 缺口%': gap,
        '3 开到高%': (hi / o - 1) * 100, '3 开到低%': (lo / o - 1) * 100,
        '4 收盘位置': (c - lo) / amp if amp > 0 else 0.5, '5 振幅%': amp / pc * 100,
        '6 先后': '先高后低' if t_hi < t_lo else ('先低后高' if t_lo < t_hi else '同时'),
        '7 最高时刻': '%s:%s%s' % (t_hi[:2], t_hi[2:4], '' if exact else '≈'),
        '7 最低时刻': '%s:%s%s' % (t_lo[:2], t_lo[2:4], '' if exact else '≈'),
        '8 路径': path_type(b, turns),
        '_折返时刻': [hm[i] for i in turns], '_t_hi': t_hi, '_t_lo': t_lo,
        '开到收%': (c / o - 1) * 100,
    }


def vol_profile(b, step, ticks=None):
    """返回 (价位数组, 量数组, 是否精确)。"""
    if ticks is not None:
        t = ticks[ticks['lastPrice'] > 0].copy()
        dv = t['volume'].diff().fillna(t['volume']).clip(lower=0)
        px = (np.round(t['lastPrice'] / step) * step).round(4)
        s = dv.groupby(px).sum()
        s = s[s > 0].sort_index()
        return s.index.values.astype(float), s.values.astype(float), True
    from day_pattern import profile
    e, v = profile(b, step)
    return e, v, False


def profile_stats(e, v, b):
    from day_pattern import value_area
    poc, val, vah = value_area(e, v)
    amt = b['amount'].sum() if 'amount' in b else (b['close'] * b['volume']).sum()
    vol = b['volume'].sum()
    vwap = amt / vol if vol else np.nan
    if vwap > b['close'].iloc[-1] * 50:
        vwap /= 100.0
    top = sorted(zip(v, e), reverse=True)[:5]
    return {'POC': poc, 'VAL': val, 'VAH': vah, 'VWAP': vwap, '总量': v.sum(), 'top5': [(p, q) for q, p in top]}


def share(e, v):
    s = pd.Series(v, index=np.round(e, 4))
    s = s.groupby(level=0).sum()
    return s / s.sum()


def compare_profiles(today, other):
    (e1, v1, s1), (e2, v2, s2) = today, other
    a, b = share(e1, v1), share(e2, v2)
    idx = a.index.union(b.index)
    a, b = a.reindex(idx, fill_value=0), b.reindex(idx, fill_value=0)
    overlap = np.minimum(a, b).sum() * 100
    in_va = a[(a.index >= s2['VAL'] - 1e-9) & (a.index <= s2['VAH'] + 1e-9)].sum() * 100
    if s1['VAL'] > s2['VAH']:
        mig = '整体上移（今天密集区在对方之上）'
    elif s1['VAH'] < s2['VAL']:
        mig = '整体下移（今天密集区在对方之下）'
    else:
        ov = min(s1['VAH'], s2['VAH']) - max(s1['VAL'], s2['VAL'])
        w = max(s1['VAH'] - s1['VAL'], 1e-9)
        mig = '重叠 %.0f%%（%s）' % (ov / w * 100, '偏上' if s1['POC'] > s2['POC'] else ('偏下' if s1['POC'] < s2['POC'] else '同位'))
    return {'量比': s1['总量'] / s2['总量'] if s2['总量'] else np.nan,
            'POC迁移%': (s1['POC'] / s2['POC'] - 1) * 100, 'VWAP迁移%': (s1['VWAP'] / s2['VWAP'] - 1) * 100,
            '密集区': mig, '分布重合度%': overlap, '今量落在对方密集区%': in_va}


# ---------------------------------------------------------------- 历史参照

def history_refs(code, bars):
    from qmt_bridge.dat_reader import read_daily
    d, _ = read_daily(code)
    d = d[(d['volume'] > 0) & (d.index >= '20150101')]
    gap = (d['open'] / d['preClose'] - 1) * 100
    oc = (d['close'] / d['open'] - 1) * 100
    amp = (d['high'] - d['low']) / d['preClose'] * 100
    wd = pd.to_datetime(d.index).weekday
    days = sorted(set(bars.index.str[:8]))
    hs, ls, fs, ts = [], [], [], []
    for day in days:
        b = bars[bars.index.str[:8] == day]
        hm = b.index.str[8:12]
        ih, il = int(np.argmax(b['high'].values)), int(np.argmin(b['low'].values))
        hs.append(slot_of(hm[ih]))
        ls.append(slot_of(hm[il]))
        fs.append(ih < il)
        a = b['high'].max() - b['low'].min()
        ts += [slot_of(hm[i]) for i in zigzag_legs(b['close'].values, max(a * 0.3, b['open'].iloc[0] * 0.002))]
    return {'gap': gap, 'oc': oc, 'amp': amp, 'wd': wd, 'n5m': len(days),
            '高点时段': pd.Series(hs).value_counts(normalize=True) * 100,
            '低点时段': pd.Series(ls).value_counts(normalize=True) * 100,
            '折返时段': pd.Series(ts).value_counts(normalize=True) * 100 if ts else pd.Series(dtype=float),
            '先高后低%': np.mean(fs) * 100}


def gap_bucket_ref(h, g):
    edges = [-99, -2, -1, -0.3, 0.3, 1, 2, 99]
    names = ['低开>2%', '低开1~2%', '低开0.3~1%', '平开', '高开0.3~1%', '高开1~2%', '高开>2%']
    i = int(np.digitize([g], edges)[0]) - 1
    i = min(max(i, 0), len(names) - 1)
    m = (h['gap'] > edges[i]) & (h['gap'] <= edges[i + 1])
    x = h['oc'][m]
    return '%s：历史 %d 天，开→收平均 %+.2f%%，收阴 %.0f%%' % (names[i], len(x), x.mean(), (x < 0).mean() * 100)


# ---------------------------------------------------------------- 主流程

def run(code, day):
    from factor_model import bars_5m
    from tick_replay import load_ticks
    bars = bars_5m(code)
    if bars is None:
        print('%s 没有 5 分钟线' % code)
        return
    days = sorted(set(bars.index.str[:8]))
    if day not in days:
        print('%s 5 分钟线里没有 %s（最后是 %s）' % (code, day, days[-1]))
        return
    i = days.index(day)
    cmp_days = [('今天', day), ('昨天', days[i - 1] if i >= 1 else None), ('一周前', days[i - 5] if i >= 5 else None)]
    from qmt_bridge.dat_reader import read_daily
    daily = read_daily(code)[0]
    ticks = load_ticks(day, code)
    if ticks is not None:
        ticks = ticks.copy()
    res, profs = {}, {}
    step = None
    for label, d in cmp_days:
        if d is None:
            continue
        b = bars[bars.index.str[:8] == d]
        j = days.index(d)
        pc = daily['preClose'].get(d) or (bars[bars.index.str[:8] == days[j - 1]]['close'].iloc[-1] if j else b['open'].iloc[0])
        tk = ticks if d == day else None
        if tk is not None:
            pc = tk['lastClose'].iloc[-1]
        res[label] = day_metrics(b, pc, tk)
        if step is None:
            step = max(0.01, round(pc * 0.001, 2))
        e, v, exact = vol_profile(b, step, tk)
        profs[label] = (e, v, profile_stats(e, v, b), exact)
        res[label]['日期'] = d
        res[label]['9 星期'] = WEEK[pd.Timestamp(d).weekday()]
    h = history_refs(code, bars)

    pd.set_option('display.unicode.east_asian_width', True)
    pd.set_option('display.width', 220)
    print('\n' + '=' * 100)
    print('%s 当日复盘对照（今天 %s；昨天 %s；一周前 %s）' % (code, day, res.get('昨天', {}).get('日期', '-'),
                                                 res.get('一周前', {}).get('日期', '-')))
    print('=' * 100)
    keys = ['9 星期', '昨收', '开盘', '最高', '最低', '收盘', '1 开盘类型', '2 缺口%', '3 开到高%', '3 开到低%', '开到收%',
            '4 收盘位置', '5 振幅%', '6 先后', '7 最高时刻', '7 最低时刻', '8 路径']
    tab = pd.DataFrame({k: {kk: res[k][kk] for kk in keys} for k in res})
    print(tab.to_string(float_format=lambda x: '%.2f' % x))

    t = res['今天']
    print('\n【历史参照】')
    print('  2 缺口：' + gap_bucket_ref(h, t['2 缺口%']))
    wd = pd.Timestamp(day).weekday()
    m = h['wd'] == wd
    print('  9 星期：历史%s %d 天，开→收平均 %+.2f%%（全部日子 %+.2f%%），振幅平均 %.2f%%（全部 %.2f%%）' % (
        WEEK[wd], m.sum(), h['oc'][m].mean(), h['oc'].mean(), h['amp'][m].mean(), h['amp'].mean()))
    print('  6 先后：5 分钟线 %d 天里先高后低 %.0f%%%s' % (h['n5m'], h['先高后低%'], '（样本少）' if h['n5m'] < 120 else ''))
    for nm, key in (('最高', '_t_hi'), ('最低', '_t_lo')):
        dist = h['高点时段' if nm == '最高' else '低点时段']
        s = slot_of(t[key])
        print('  10 今天%s在 %s，历史上%s出现在这个时段的概率 %.0f%%；各时段：%s' % (
            nm, s, nm, dist.get(s, 0), '  '.join('%s %.0f%%' % (n, dist.get(n, 0)) for _, _, n in SLOTS)))
    if len(h['折返时段']):
        print('  10 折返多出现在：%s；今天的折返时刻：%s' % (
            '  '.join('%s %.0f%%' % (n, h['折返时段'].get(n, 0)) for _, _, n in SLOTS),
            '、'.join('%s:%s' % (x[:2], x[2:4]) for x in t['_折返时刻']) or '无'))

    print('\n【11 各价位成交量】（POC = 成交最多的价位；密集区 = 围绕 POC 占 70% 成交量的价格区间；≈ = 用 5 分钟线近似）')
    for label in res:
        e, v, s, exact = profs[label]
        print('  %-4s %s POC %.2f  密集区 %.2f~%.2f  VWAP %.3f  总量 %.0f 手  量最大的价位：%s' % (
            label, '' if exact else '≈', s['POC'], s['VAL'], s['VAH'], s['VWAP'], s['总量'],
            '，'.join('%.2f(%.0f)' % (p, q) for p, q in s['top5'])))
    print('\n【12 今天对比】')
    td = profs['今天']
    for label in ('昨天', '一周前'):
        if label not in profs:
            continue
        o = profs[label]
        r = compare_profiles((td[0], td[1], td[2]), (o[0], o[1], o[2]))
        print('  对%s：量比 %.2f，POC %+.2f%%，VWAP %+.2f%%，密集区%s，两天分布重合 %.0f%%，今天的量有 %.0f%% 落在%s的密集区里' % (
            label, r['量比'], r['POC迁移%'], r['VWAP迁移%'], r['密集区'], r['分布重合度%'], r['今量落在对方密集区%'], label))


def main():
    ap = argparse.ArgumentParser(description='当日复盘对照：今天 / 昨天 / 一周前')
    ap.add_argument('day', nargs='?')
    ap.add_argument('codes', nargs='*', default=['600366', '605208', '600436'])
    a = ap.parse_args()
    day = a.day
    if not day:
        ds = sorted(x for x in os.listdir(TICK_DIR) if x.isdigit())
        day = ds[-1]
    for c in a.codes:
        run(code_of(c), day)


if __name__ == '__main__':
    main()
