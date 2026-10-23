# coding:utf-8
"""日内形态统计：开盘缺口、先高还是先低、星期几、今天的量落在昨天哪些价位 —— 找能指导做 T 的规律。

    .venv313\\Scripts\\python.exe tools\\day_pattern.py                         # 默认 600436 600366 605208
    .venv313\\Scripts\\python.exe tools\\day_pattern.py 600436 --flat 0.3       # 平开阈值 ±0.3%
    .venv313\\Scripts\\python.exe tools\\day_pattern.py 600366 --no-sina        # 不从新浪补 5 分钟线

只在 .venv313 跑（用了 3.7+ 写法）。不下单。

每天算的东西（都只用 5 分钟 K 线，09:35 那根是开盘后第一根）：
  * 开盘 vs 昨收：高开 / 平开 / 低开（--flat 阈值，默认 ±0.3%）。昨收优先用 QMT 日线的 preClose（除权日已调整）。
  * 先高后低 / 先低后高：最高价那根 K 线在最低价那根之前还是之后；同一根算「同根」不参与比较。
    另记最高、最低出现在哪个时段，以及是不是开盘第一根就见顶 / 见底。
  * 星期几。
  * 量价对标：用昨天的 5 分钟线把成交量摊到价位上（每根的量均匀摊到 [low, high]），得到昨天的
    POC（成交最多的价位）和价值区（围绕 POC 占 70% 成交的区间，VAL~VAH）。今天：
      - 开盘在昨天价值区上方 / 里面 / 下方；
      - 今天全天成交里有多少落在昨天价值区内（接受度）；
      - 开盘 30 分钟量 / 昨天开盘 30 分钟量（10:00 就知道，能当盘中信号）。
做 T 的检验只用「决策时已经知道的信息」：开盘缺口、星期、开盘相对昨价值区、10:00 前的走势和量比。
结果看 10:00 价格之后：
  * 正T（10:00 卖、收盘前 14:55 买回）和反T（10:00 买、14:55 卖）的平均毛收益 %；
  * 10:00 之后还能往上走多少（MFE 上）、往下走多少（MFE 下）。
每个分组给样本数、均值、t 值、胜率，并按前半段 / 后半段分开看是否同号（防止只在一段时间里成立）。
来回费用按 t0_sim 的费率（万5 佣金×2 + 印花税 + 过户费，约 0.15%）扣除后看净值。

结果：控制台报告 + 共享数据目录 day_pattern/<代码>_days.csv（逐日特征，可以自己再切）。
新浪 5 分钟线最多回溯约 1970 根（两个多月），每次跑会合并缓存到 day_pattern/sina5m/，越跑历史越长。
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

BAR_DIR = os.path.join(DATA_DIR, 'bars5m')
OUT_DIR = os.path.join(DATA_DIR, 'day_pattern')
SINA_DIR = os.path.join(OUT_DIR, 'sina5m')
WEEK = ['周一', '周二', '周三', '周四', '周五']


# ---------------------------------------------------------------- 数据

def _sina(code):
    """新浪 5 分钟线（不复权），合并进本地缓存后返回。成交量换算成手，跟 QMT 一致。"""
    os.makedirs(SINA_DIR, exist_ok=True)
    path = os.path.join(SINA_DIR, '%s.csv' % code)
    old = None
    if os.path.exists(path):
        old = pd.read_csv(path, dtype={'time': str}, encoding='utf-8-sig').set_index('time')
    try:
        import akshare as ak
        sym = ('sh' if code.endswith('.SH') else 'sz') + code[:6]
        d = ak.stock_zh_a_minute(symbol=sym, period='5', adjust='')
        d['time'] = d['day'].str.replace(r'[-: ]', '', regex=True)
        d = d.set_index('time')[['open', 'high', 'low', 'close', 'volume', 'amount']].astype(float)
        d['volume'] = d['volume'] / 100.0
        new = d if old is None else pd.concat([old[~old.index.isin(d.index)], d])
        new = new.sort_index()
        new.to_csv(path, encoding='utf-8-sig')
        return new
    except Exception as e:                    # 网络不通就用缓存
        print('  新浪 5 分钟线没拿到（%s），用缓存' % e)
        return old


def load_bars(code, use_sina=True):
    parts = []
    p = os.path.join(BAR_DIR, '%s.csv' % code)
    if os.path.exists(p):
        q = pd.read_csv(p, dtype={'time': str}, encoding='utf-8-sig').set_index('time')
        parts.append(q)
    if use_sina:
        s = _sina(code)
        if s is not None and len(s):
            have = set(i[:8] for i in parts[0].index) if parts else set()
            parts.append(s[~s.index.str[:8].isin(have)])      # 同一天 QMT 优先
    if not parts:
        return None
    df = pd.concat(parts).sort_index()
    df = df[(df['high'] >= df['low']) & (df['low'] > 0)]
    df = df[~df.index.duplicated(keep='first')]
    # 只留完整交易日（≥45 根），盘中跑时当天没走完的丢掉
    cnt = df.groupby(df.index.str[:8]).size()
    return df[df.index.str[:8].isin(cnt[cnt >= 45].index)]


def load_preclose(code):
    try:
        from qmt_bridge.dat_reader import read_daily
        d, _ = read_daily(code)
        return d['preClose'].to_dict()
    except Exception:
        return {}


# ---------------------------------------------------------------- 量价分布

def profile(bars, step):
    """把每根 K 线的量均匀摊到 [low, high]，返回 (价位数组, 量数组)。"""
    lo = np.floor(bars['low'].min() / step) * step
    hi = np.ceil(bars['high'].max() / step) * step
    edges = np.arange(lo, hi + step * 1.5, step)
    vol = np.zeros(len(edges))
    for l, h, v in zip(bars['low'].values, bars['high'].values, bars['volume'].values):
        i0 = int(round((l - lo) / step))
        i1 = int(round((h - lo) / step))
        vol[i0:i1 + 1] += v / (i1 - i0 + 1)
    return edges, vol


def value_area(edges, vol, frac=0.7):
    poc = int(np.argmax(vol))
    lo = hi = poc
    tot, acc = vol.sum(), vol[poc]
    while acc < frac * tot and (lo > 0 or hi < len(vol) - 1):
        up = vol[hi + 1] if hi < len(vol) - 1 else -1
        dn = vol[lo - 1] if lo > 0 else -1
        if up >= dn:
            hi += 1
            acc += up
        else:
            lo -= 1
            acc += dn
    return edges[poc], edges[lo], edges[hi]


# ---------------------------------------------------------------- 逐日特征

def day_features(df, preclose_map, flat):
    days = sorted(set(df.index.str[:8]))
    rows = []
    prev = None
    for d in days:
        b = df[df.index.str[:8] == d]
        hhmm = b.index.str[8:12]
        o, h, l, c = b['open'].iloc[0], b['high'].max(), b['low'].min(), b['close'].iloc[-1]
        ih, il = int(np.argmax(b['high'].values)), int(np.argmin(b['low'].values))
        r = {'日期': d, '星期': WEEK[pd.Timestamp(d).weekday()], '开': o, '高': h, '低': l, '收': c,
             '量': b['volume'].sum()}
        pc = preclose_map.get(d)
        if pc is None and prev is not None:
            pc = prev['bars']['close'].iloc[-1]
        if pc is None:
            prev = {'bars': b}
            continue
        r['昨收'] = pc
        r['缺口%'] = (o / pc - 1) * 100
        r['开盘'] = '高开' if r['缺口%'] > flat else ('低开' if r['缺口%'] < -flat else '平开')
        r['振幅%'] = (h - l) / pc * 100
        r['开到高%'] = (h / o - 1) * 100
        r['开到低%'] = (l / o - 1) * 100
        r['收盘位置'] = (c - l) / (h - l) if h > l else 0.5
        r['阴阳'] = '阳' if c > o else ('阴' if c < o else '平')
        r['路径'] = '先高后低' if ih < il else ('先低后高' if il < ih else '同根')
        r['高点时刻'] = hhmm[ih]
        r['低点时刻'] = hhmm[il]
        r['开盘即高'] = ih == 0
        r['开盘即低'] = il == 0

        # 10:00 决策点
        early = b[hhmm <= '1000']
        late = b[(hhmm > '1000') & (hhmm <= '1455')]
        p10 = early['close'].iloc[-1]
        p1455 = late['close'].iloc[-1]
        r['10点价'] = p10
        r['开盘30分%'] = (p10 / o - 1) * 100
        r['10点后最高%'] = (late['high'].max() / p10 - 1) * 100
        r['10点后最低%'] = (late['low'].min() / p10 - 1) * 100
        r['正T毛%'] = (p10 / p1455 - 1) * 100            # 10:00 卖、14:55 买回
        r['反T毛%'] = (p1455 / p10 - 1) * 100

        if prev is not None:
            pb = prev['bars']
            step = max(0.01, round(pc * 0.001, 2))
            edges, vol = profile(pb, step)
            poc, val, vah = value_area(edges, vol)
            r['昨POC'], r['昨VAL'], r['昨VAH'] = poc, val, vah
            r['开盘vs昨价值区'] = '上方' if o > vah else ('下方' if o < val else '区内')
            e2, v2 = profile(b, step)
            r['今量在昨价值区%'] = v2[(e2 >= val - 1e-9) & (e2 <= vah + 1e-9)].sum() / v2.sum() * 100
            r['回到昨POC'] = float(l <= poc <= h)
            r['量比'] = b['volume'].sum() / pb['volume'].sum()
            pe = pb[pb.index.str[8:12] <= '1000']['volume'].sum()
            r['开盘30分量比'] = early['volume'].sum() / pe if pe > 0 else np.nan
            r['高点离昨VAH%'] = (h / vah - 1) * 100
            r['低点离昨VAL%'] = (l / val - 1) * 100
        prev = {'bars': b}
        rows.append(r)
    return pd.DataFrame(rows).set_index('日期')


# ---------------------------------------------------------------- 统计

def tstat(x):
    x = x.dropna()
    if len(x) < 3 or x.std() == 0:
        return np.nan
    return x.mean() / x.std() * np.sqrt(len(x))


def crosstab_pct(t, row, col):
    ct = pd.crosstab(t[row], t[col])
    pct = ct.div(ct.sum(axis=1), axis=0) * 100
    out = pct.round(0).astype(int).astype(str) + '%'
    out['样本'] = ct.sum(axis=1)
    return out


def group_test(t, key, cost):
    """按 key 分组，看 10:00 做正T/反T 的毛、净收益和前后半段是否同号。"""
    half = t.index[len(t) // 2]
    rows = []
    for g, x in t.groupby(key):
        if len(x) < 5:
            continue
        a, b = x[x.index < half], x[x.index >= half]
        rows.append({
            key: g, '样本': len(x),
            '正T毛%': x['正T毛%'].mean(), 't': tstat(x['正T毛%']),
            '正T胜率': (x['正T毛%'] > cost).mean() * 100,
            '前半段': a['正T毛%'].mean() if len(a) else np.nan,
            '后半段': b['正T毛%'].mean() if len(b) else np.nan,
            '10点后上%': x['10点后最高%'].mean(), '10点后下%': x['10点后最低%'].mean(),
        })
    out = pd.DataFrame(rows).set_index(key)
    return out


def flag(row, cost):
    m = row['正T毛%']
    if abs(m) <= cost or abs(row['t']) < 2:
        return ''
    if np.sign(row['前半段']) != np.sign(m) or np.sign(row['后半段']) != np.sign(m) \
            or min(abs(row['前半段']), abs(row['后半段'])) < cost / 3:
        return ''
    return '  <- 正T' if m > 0 else '  <- 反T'


def report(code, t, cost):
    pd.set_option('display.unicode.east_asian_width', True)
    pd.set_option('display.width', 200)
    fmt = lambda v: '%.2f' % v       # noqa: E731
    n = len(t)
    print('\n' + '=' * 90)
    print('%s  %s ~ %s  共 %d 天%s' % (code, t.index[0], t.index[-1], n,
                                   '（样本太少，下面的结论只当参考）' if n < 120 else ''))
    print('=' * 90)

    print('\n【1】开盘 vs 昨收')
    s = t.groupby('开盘').agg(天数=('收', 'size'), 平均缺口=('缺口%', 'mean'), 振幅=('振幅%', 'mean'),
                            开到高=('开到高%', 'mean'), 开到低=('开到低%', 'mean'),
                            收阳率=('阴阳', lambda x: (x == '阳').mean() * 100),
                            收盘位置=('收盘位置', 'mean'))
    print(s.round(2).to_string())

    print('\n【2】先高后低还是先低后高（按开盘方式）')
    print(crosstab_pct(t, '开盘', '路径').to_string())
    print('  开盘第一根就是全天最高：%.0f%%，就是全天最低：%.0f%%' % (t['开盘即高'].mean() * 100, t['开盘即低'].mean() * 100))
    for col in ['高点时刻', '低点时刻']:
        bucket = pd.cut(t[col].astype(int), [0, 1000, 1130, 1400, 1500],
                        labels=['9:30-10:00', '10:00-11:30', '13:00-14:00', '14:00-15:00'])
        print('  %s分布：%s' % (col[:2], '  '.join('%s %.0f%%' % (k, v * 100)
                                                 for k, v in bucket.value_counts(normalize=True).sort_index().items())))

    print('\n【3】星期几')
    s = t.groupby('星期').agg(天数=('收', 'size'), 振幅=('振幅%', 'mean'), 缺口=('缺口%', 'mean'),
                            先高后低=('路径', lambda x: (x == '先高后低').mean() * 100),
                            收阳率=('阴阳', lambda x: (x == '阳').mean() * 100),
                            正T毛=('正T毛%', 'mean'))
    print(s.reindex([w for w in WEEK if w in s.index]).round(2).to_string())

    v = t.dropna(subset=['昨POC'])
    print('\n【4】量价对标昨天（昨天的成交密集区 = 价值区 VAL~VAH，POC = 成交最多的价位）')
    s = v.groupby('开盘vs昨价值区').agg(天数=('收', 'size'), 回到昨POC=('回到昨POC', lambda x: x.mean() * 100),
                                   今量在昨区=('今量在昨价值区%', 'mean'),
                                   先高后低=('路径', lambda x: (x == '先高后低').mean() * 100),
                                   收阳率=('阴阳', lambda x: (x == '阳').mean() * 100),
                                   正T毛=('正T毛%', 'mean'))
    print(s.round(1).to_string())
    print('  今天最高价离昨 VAH 的中位数 %+.2f%%，最低价离昨 VAL 的中位数 %+.2f%%' % (
        v['高点离昨VAH%'].median(), v['低点离昨VAL%'].median()))
    near_h = (v['高点离昨VAH%'].abs() < 0.5).mean() * 100
    near_l = (v['低点离昨VAL%'].abs() < 0.5).mean() * 100
    print('  最高价落在昨 VAH ±0.5%% 内的天数 %.0f%%，最低价落在昨 VAL ±0.5%% 内 %.0f%%' % (near_h, near_l))

    print('\n【5】拿 10:00 前已知信息分组，检验 10:00 做 T（正T=10点卖14:55买回；负数说明反T更好）')
    print('    来回费用约 %.2f%%；标「<-」的是 |均值|>费用、|t|≥2、前后半段都同向且不接近 0 的分组；分组很多，t≥2 也会有偶然的' % cost)
    v = v.copy()
    v['开盘30分走势'] = np.where(v['开盘30分%'] > 0.3, '涨', np.where(v['开盘30分%'] < -0.3, '跌', '平'))
    v['开盘30分量'] = pd.cut(v['开盘30分量比'], [0, 0.8, 1.25, 99], labels=['缩量', '平量', '放量'])
    v['开盘+30分'] = v['开盘'] + '/' + v['开盘30分走势']
    for key in ['开盘', '星期', '开盘vs昨价值区', '开盘30分走势', '开盘30分量', '开盘+30分']:
        g = group_test(v, key, cost)
        if not len(g):
            continue
        g['结论'] = [flag(r, cost) for _, r in g.iterrows()]
        print('\n  按%s：' % key)
        print('  ' + g.to_string(float_format=fmt).replace('\n', '\n  '))
    allm = v['正T毛%']
    print('\n  全部天数：正T毛 %.2f%%（t=%.1f），10:00 后平均还能上 %.2f%%、下 %.2f%%' % (
        allm.mean(), tstat(allm), v['10点后最高%'].mean(), v['10点后最低%'].mean()))


def main():
    ap = argparse.ArgumentParser(description='日内形态统计（缺口/先高先低/星期/量价对标）')
    ap.add_argument('codes', nargs='*', default=['600436', '600366', '605208'])
    ap.add_argument('--flat', type=float, default=0.3, help='平开阈值 %%，默认 0.3')
    ap.add_argument('--no-sina', action='store_true', help='不从新浪补 5 分钟线')
    a = ap.parse_args()

    p = load_params()
    cost = (p['commission'] * 2 + p['stamp_tax'] + p['transfer_fee'] * 2) * 100
    os.makedirs(OUT_DIR, exist_ok=True)
    for c in a.codes:
        code = c.upper() if '.' in c else c + ('.SH' if c[0] in '659' else '.SZ')
        df = load_bars(code, not a.no_sina)
        if df is None or not len(df):
            print('%s 没有 5 分钟线' % code)
            continue
        t = day_features(df, load_preclose(code), a.flat)
        t.to_csv(os.path.join(OUT_DIR, '%s_days.csv' % code), encoding='utf-8-sig')
        report(code, t, cost)


if __name__ == '__main__':
    main()
