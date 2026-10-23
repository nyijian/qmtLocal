# coding:utf-8
"""六因子做 T 模型：把「开盘、价格路径、价格位置、成交量、时间、市场环境」六类因子提出来，建模，滚动样本外检验。

    .venv313\\Scripts\\python.exe tools\\factor_model.py                 # 模型 A（开盘决策，日线）+ 模型 B（盘中决策，5 分钟线）
    .venv313\\Scripts\\python.exe tools\\factor_model.py --only A
    .venv313\\Scripts\\python.exe tools\\factor_model.py --refresh-sector # 重新下载板块指数日线

只在 .venv313 跑（用了 3.7+ 写法和 scikit-learn）。不下单。

模型 A（开盘决策）：每天开盘那一刻，只用当时已知的因子，预测「开盘 → 收盘」涨跌 %，
  预测涨幅 > 来回成本 → 开盘先买、收盘卖；预测跌幅 > 成本 → 开盘先卖、收盘买回；其余不做。
  数据：QMT 日线（2015 年起，前复权算均线和涨跌）+ 上证指数日线 + 东方财富行业板块指数日线（akshare，缓存到 akshare/sector/）。
  因子：① 缺口；③ 开盘价在昨日高低区间的位置、昨收在昨日区间的位置、离 20 日均线多远、均线斜率、近 5/20 日涨跌；
        ④ 昨日量比、近 20 日振幅；⑤ 星期；⑥ 大盘今天缺口、昨日涨跌、离均线多远，板块同样三项，个股相对板块的昨日强弱；
        外加昨日「开→收」（日内方向有没有惯性）。② 价格路径日线看不到，放在模型 B。
  检验：按年滚动——用某年之前的全部数据训练，预测这一年；三只股票合在一起训练（样本多），分开结算。

模型 B（盘中决策）：10:00 / 10:30 / 11:00 / 13:30 / 14:00 五个时点，预测「此刻 → 14:55」涨跌 %，规则同上，每天最多一笔。
  数据：600436 QMT 本地 5 分钟线（2023-05 起）+ 桥取的 5 分钟线；沪深300 5 分钟线当大盘；
        600366 / 605208 只有新浪两个多月的 5 分钟线，只拿 600436 训练好的模型去检验（迁移）。
  因子：① 缺口；② 开盘以来涨跌、最近 30 分钟涨跌、到目前先高后低还是先低后高、离今日高 / 低点过了多久、目前振幅 / 平常振幅；
        ③ 在今日区间的位置、离 VWAP、离昨收 / 昨高 / 昨低、离昨日成交密集区（POC / VAH / VAL）；
        ④ 累计成交量 / 过去 20 天同一时刻的累计量、最近 3 根量比、今日成交落在昨日密集区的比例；
        ⑤ 时点、星期；⑥ 沪深300 开盘以来涨跌、缺口、最近 30 分钟涨跌，个股相对沪深300 的强弱。板块没有分钟数据，模型 B 不含。
  检验：按季度滚动（至少 120 天训练）。

两个模型都用两种算法：岭回归（线性、稳）和梯度提升树（能抓非线性，容易过拟合）。
每个因子另外给「单因子 IC」：因子和目标的秩相关，全部样本 + 逐年同号的比例——这是「提取因子」那一步，看哪个因子真的有信息。
最后都要跟对照组比：固定开盘买收盘卖、固定开盘卖收盘买、不做（0）。费用按每笔 --notional 元套万0.8 最低 5 元；
模型 A 开收盘走集合竞价不加滑点，模型 B 盘中每边加 0.5 个价位。结果写共享数据目录 factor_model/。
"""
import argparse
import os
import sys
import warnings

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from t0_sim import DATA_DIR, load_params  # noqa: E402
from pattern_tests import cost_pct  # noqa: E402

warnings.filterwarnings('ignore')
OUT_DIR = os.path.join(DATA_DIR, 'factor_model')
SECTOR_DIR = os.path.join(DATA_DIR, 'akshare', 'sector')
SECTORS = {'600366.SH': '金属新材料', '605208.SH': '工业金属', '600436.SH': '中药Ⅱ'}   # 东方财富行业（2026-10-21 查）
CODES = list(SECTORS)
CHECKPOINTS = ['1000', '1030', '1100', '1330', '1400']


# ---------------------------------------------------------------- 数据

def daily_adj(code):
    """日线 + 前复权收盘 / 高 / 低（用 preClose 推除权因子）。"""
    from qmt_bridge.dat_reader import read_daily
    d, _ = read_daily(code)
    d = d[d['volume'] > 0].copy()
    f = (d['preClose'] / d['close'].shift(1)).fillna(1.0)
    k = f[::-1].cumprod()[::-1].shift(-1).fillna(1.0)
    for c in ('open', 'high', 'low', 'close'):
        d['a' + c] = d[c] * k
    return d


def sector_daily(name, refresh=False):
    os.makedirs(SECTOR_DIR, exist_ok=True)
    path = os.path.join(SECTOR_DIR, '%s.csv' % name)
    if os.path.exists(path) and not refresh:
        s = pd.read_csv(path, dtype={'日期': str}, encoding='utf-8-sig')
    else:
        import akshare as ak
        s = ak.stock_board_industry_hist_em(symbol=name, start_date='20150101', end_date='20991231',
                                            period='日k', adjust='')
        s['日期'] = s['日期'].astype(str).str.replace('-', '')
        s.to_csv(path, index=False, encoding='utf-8-sig')
    s = s.set_index('日期')[['开盘', '收盘', '最高', '最低']].astype(float)
    s.columns = ['open', 'close', 'high', 'low']
    return s


def env_feats(df, prefix):
    """大盘 / 板块：今天缺口、昨日涨跌、昨收离 20 日均线。开盘时都已知。"""
    pc = df['close'].shift(1)
    ma = df['close'].rolling(20).mean()
    return pd.DataFrame({
        prefix + '缺口': (df['open'] / pc - 1) * 100,
        prefix + '昨涨跌': (pc / df['close'].shift(2) - 1) * 100,
        prefix + '昨离均线': (pc / ma.shift(1) - 1) * 100,
    }, index=df.index)


# ---------------------------------------------------------------- 模型 A：开盘决策

A_FEATS = {
    '① 开盘': ['缺口'],
    '③ 价格位置': ['开盘在昨区间位置', '昨收在昨区间位置', '昨离均线', '均线斜率', '近5日涨跌', '近20日涨跌'],
    '④ 成交量': ['昨量比', '近20日振幅'],
    '② 日内惯性': ['昨开到收'],
    '⑤ 时间': ['周一', '周二', '周四', '周五'],
    '⑥ 市场环境': ['大盘缺口', '大盘昨涨跌', '大盘昨离均线', '板块缺口', '板块昨涨跌', '板块昨离均线', '昨相对板块'],
}


def build_a(code, idx_env, refresh):
    d = daily_adj(code)
    o, h, l, c = d['open'], d['high'], d['low'], d['close']
    ac, ah, al = d['aclose'], d['ahigh'], d['alow']
    f = d['preClose'] / c.shift(1)                      # 今天的除权因子，把昨天的价折到今天口径
    ph, pl, pcl = h.shift(1) * f, l.shift(1) * f, d['preClose']
    ma = ac.rolling(20).mean()
    x = pd.DataFrame(index=d.index)
    x['缺口'] = (o / pcl - 1) * 100
    x['开盘在昨区间位置'] = ((o - pl) / (ph - pl)).clip(-2, 3)
    x['昨收在昨区间位置'] = ((ac - al) / (ah - al)).shift(1)
    x['昨离均线'] = (ac / ma - 1).shift(1) * 100
    x['均线斜率'] = (ma / ma.shift(5) - 1).shift(1) * 100
    x['近5日涨跌'] = (ac / ac.shift(5) - 1).shift(1) * 100
    x['近20日涨跌'] = (ac / ac.shift(20) - 1).shift(1) * 100
    x['昨量比'] = (d['volume'] / d['volume'].rolling(20).mean()).shift(1)
    x['近20日振幅'] = ((ah - al) / ac.shift(1)).rolling(20).mean().shift(1) * 100
    x['昨开到收'] = (c / o - 1).shift(1) * 100
    wd = pd.to_datetime(d.index).weekday
    for i, n in ((0, '周一'), (1, '周二'), (3, '周四'), (4, '周五')):
        x[n] = (wd == i).astype(float)
    x = x.join(idx_env)
    sec = env_feats(sector_daily(SECTORS[code], refresh), '板块')
    x = x.join(sec)
    sret = sector_daily(SECTORS[code])['close'].pct_change() * 100
    x['昨相对板块'] = (ac.pct_change() * 100).shift(1) - sret.shift(1).reindex(x.index)
    x['y'] = (c / o - 1) * 100
    x['价'] = o
    x['代码'] = code
    ok = (h > l) & x['近20日涨跌'].notna() & (x.index >= '20150101')
    return x[ok]


# ---------------------------------------------------------------- 模型 B：盘中决策

B_FEATS = {
    '① 开盘': ['缺口'],
    '② 价格路径': ['开盘以来', '近30分钟', '先高后低', '离高点根数', '离低点根数', '振幅比平常'],
    '③ 价格位置': ['区间位置', '离VWAP', '离昨收', '离昨高', '离昨低', '离POC', '离VAH', '离VAL', '昨离均线'],
    '④ 成交量': ['累计量比', '近3根量比', '今量在昨区比例'],
    '⑤ 时间': ['t1000', 't1030', 't1100', 't1330', '周一', '周五'],
    '⑥ 市场环境': ['大盘开盘以来', '大盘缺口', '大盘近30分钟', '相对大盘'],
}


def bars_5m(code):
    from qmt_bridge.dat_reader import read_bars
    from day_pattern import load_bars
    parts = []
    try:
        parts.append(read_bars(code, '5m'))
    except FileNotFoundError:
        pass
    try:
        b = load_bars(code, use_sina=True)
        if b is not None:
            parts.append(b[['open', 'high', 'low', 'close', 'volume', 'amount']])
    except Exception:
        pass
    if not parts:
        return None
    df = pd.concat(parts)
    df = df[~df.index.duplicated(keep='first')].sort_index()
    df = df[(df['high'] >= df['low']) & (df['low'] > 0)]
    cnt = df.groupby(df.index.str[:8]).size()
    return df[df.index.str[:8].isin(cnt[cnt >= 48].index)]   # 48 根 = 完整一天，盘中跑时当天不算


def build_b(code, idx_bars):
    from day_pattern import profile, value_area
    df = bars_5m(code)
    if df is None:
        return None
    daily = daily_adj(code)
    ma = daily['aclose'].rolling(20).mean()
    ma_dist = ((daily['aclose'] / ma - 1) * 100).shift(1)
    amp20 = ((daily['ahigh'] - daily['alow']) / daily['aclose'].shift(1) * 100).rolling(20).mean().shift(1)
    ib = {d: g for d, g in idx_bars.groupby(idx_bars.index.str[:8])} if idx_bars is not None else {}
    rows, prev, cum_hist = [], None, {}
    for d, b in df.groupby(df.index.str[:8]):
        hm = np.array(b.index.str[8:12])
        v = b['volume'].values.astype(float)
        cv = np.cumsum(v)
        if prev is not None:
            pc = daily['preClose'].get(d, prev['close'].iloc[-1])
            fx = pc / prev['close'].iloc[-1]
            step = max(0.01, round(pc * 0.001, 2))
            e, pv = profile(prev, step)
            poc, val, vah = (z * fx for z in value_area(e, pv))
            ph, pl = prev['high'].max() * fx, prev['low'].min() * fx
            o = b['open'].iloc[0]
            c, h, l = b['close'].values, b['high'].values, b['low'].values
            amt = b['amount'].values.astype(float)
            vw = np.cumsum(amt) / np.maximum(cv, 1)
            if np.nanmedian(vw / c) > 50:
                vw = vw / 100.0
            inva = np.cumsum(np.where((c >= val) & (c <= vah), v, 0)) / np.maximum(cv, 1)
            i1455 = int(np.where(hm <= '1455')[0][-1])
            x_idx = ib.get(d)
            for cp in CHECKPOINTS:
                w = np.where(hm == cp)[0]
                if not len(w):
                    continue
                i = int(w[0])
                px = c[i]
                hi, lo = h[:i + 1].max(), l[:i + 1].min()
                ih, il = int(np.argmax(h[:i + 1])), int(np.argmin(l[:i + 1]))
                hist = cum_hist.get(cp, [])
                r = {'日期': d, '时点': cp, '代码': code, '价': px,
                     '缺口': (o / pc - 1) * 100,
                     '开盘以来': (px / o - 1) * 100,
                     '近30分钟': (px / c[max(0, i - 6)] - 1) * 100,
                     '先高后低': 1.0 if ih < il else (-1.0 if il < ih else 0.0),
                     '离高点根数': (i - ih) / (i + 1), '离低点根数': (i - il) / (i + 1),
                     '振幅比平常': ((hi - lo) / pc * 100) / amp20.get(d, np.nan) if amp20.get(d, 0) else np.nan,
                     '区间位置': (px - lo) / (hi - lo) if hi > lo else 0.5,
                     '离VWAP': (px / vw[i] - 1) * 100,
                     '离昨收': (px / pc - 1) * 100, '离昨高': (px / ph - 1) * 100, '离昨低': (px / pl - 1) * 100,
                     '离POC': (px / poc - 1) * 100, '离VAH': (px / vah - 1) * 100, '离VAL': (px / val - 1) * 100,
                     '昨离均线': ma_dist.get(d, np.nan),
                     '累计量比': cv[i] / np.mean(hist[-20:]) if len(hist) >= 10 else np.nan,
                     '近3根量比': v[max(0, i - 2):i + 1].sum() / (cv[i] / (i + 1) * min(3, i + 1)),
                     '今量在昨区比例': inva[i],
                     't1000': float(cp == '1000'), 't1030': float(cp == '1030'), 't1100': float(cp == '1100'),
                     't1330': float(cp == '1330'),
                     '周一': float(pd.Timestamp(d).weekday() == 0), '周五': float(pd.Timestamp(d).weekday() == 4),
                     'y': (c[i1455] / px - 1) * 100, 'y30': (c[min(i + 6, i1455)] / px - 1) * 100}
                if x_idx is not None:
                    xh = np.array(x_idx.index.str[8:12])
                    wi = np.where(xh == cp)[0]
                    if len(wi):
                        j = int(wi[0])
                        xc = x_idx['close'].values
                        ipc = x_idx['preClose'].iloc[0] if 'preClose' in x_idx and x_idx['preClose'].iloc[0] > 0 else np.nan
                        r['大盘开盘以来'] = (xc[j] / x_idx['open'].iloc[0] - 1) * 100
                        r['大盘缺口'] = (x_idx['open'].iloc[0] / ipc - 1) * 100 if ipc == ipc else np.nan
                        r['大盘近30分钟'] = (xc[j] / xc[max(0, j - 6)] - 1) * 100
                        r['相对大盘'] = r['开盘以来'] - r['大盘开盘以来']
                rows.append(r)
        for cp in CHECKPOINTS:
            w = np.where(hm == cp)[0]
            if len(w):
                cum_hist.setdefault(cp, []).append(cv[int(w[0])])
        prev = b
    return pd.DataFrame(rows)


def index_5m():
    """沪深300 5 分钟线：QMT 本地 + 新浪补近两个月。preClose 用前一天最后一根收盘。"""
    from qmt_bridge.dat_reader import read_bars
    parts = []
    try:
        parts.append(read_bars('000300.SH', '5m')[['open', 'high', 'low', 'close', 'volume', 'amount']])
    except FileNotFoundError:
        pass
    try:
        import akshare as ak
        s = ak.stock_zh_a_minute(symbol='sh000300', period='5', adjust='')
        s.index = s['day'].str.replace(r'[-: ]', '', regex=True)
        parts.append(s[['open', 'high', 'low', 'close', 'volume', 'amount']].astype(float))
    except Exception as e:
        print('  新浪沪深300 5 分钟线没拿到（%s），只用 QMT 本地的' % str(e)[:60])
    if not parts:
        return None
    df = pd.concat(parts)
    df = df[~df.index.duplicated(keep='first')].sort_index()
    last = df.groupby(df.index.str[:8])['close'].last()
    df['preClose'] = df.index.str[:8].map(last.shift(1))
    return df


# ---------------------------------------------------------------- 建模与检验

def feat_list(groups):
    return [f for g in groups.values() for f in g]


def ic_table(x, feats, groups, by='年'):
    """单因子 IC：因子和目标的秩相关；逐期同号比例。"""
    rows = []
    for g, fs in groups.items():
        for f in fs:
            if f not in x or x[f].nunique() < 2:
                continue
            s = x[[f, 'y', by]].dropna()
            if len(s) < 50:
                continue
            ic = s[f].rank().corr(s['y'].rank())
            per = s.groupby(by).apply(lambda z: z[f].rank().corr(z['y'].rank()) if len(z) > 20 else np.nan).dropna()
            rows.append({'类': g, '因子': f, 'IC': ic, 't': ic * np.sqrt(len(s)),
                         '同号期数': '%d/%d' % ((np.sign(per) == np.sign(ic)).sum(), len(per))})
    return pd.DataFrame(rows)


def walk_forward(x, feats, period_col, min_train, models):
    """按 period_col 滚动：训练用之前所有期，预测这一期。返回带各模型预测的测试集。"""
    from sklearn.linear_model import Ridge
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.impute import SimpleImputer
    periods = sorted(x[period_col].unique())
    out = []
    for p in periods:
        tr, te = x[x[period_col] < p], x[x[period_col] == p]
        if tr['日期'].nunique() < min_train or not len(te):
            continue
        te = te.copy()
        y = tr['y'].clip(tr['y'].quantile(0.01), tr['y'].quantile(0.99))     # 去掉极端值，免得几天涨跌停主导
        if 'ridge' in models:
            m = make_pipeline(SimpleImputer(strategy='median'), StandardScaler(), Ridge(alpha=50.0))
            m.fit(tr[feats], y)
            te['pred_ridge'] = m.predict(te[feats])
        if 'gbm' in models:
            m = HistGradientBoostingRegressor(max_depth=3, learning_rate=0.05, max_iter=150, min_samples_leaf=40,
                                              l2_regularization=1.0, random_state=0)
            m.fit(tr[feats], y)
            te['pred_gbm'] = m.predict(te[feats])
        out.append(te)
    return pd.concat(out) if out else pd.DataFrame()


def settle(te, pred_col, cost_col, mult=1.0, one_per_day=False):
    """预测 > 成本×mult 先买，< -成本×mult 先卖，否则不做。返回每笔净收益（不做 = 不计入笔数，但计入每天平均）。"""
    t = te.copy()
    t['side'] = np.where(t[pred_col] > t[cost_col] * mult, 1, np.where(t[pred_col] < -t[cost_col] * mult, -1, 0))
    t = t[t['side'] != 0]
    if one_per_day:
        t = t.sort_values(['代码', '日期', '时点']).groupby(['代码', '日期']).head(1)
    t['net'] = t['side'] * t['y'] - t[cost_col]
    return t


def summary(label, net, n_days):
    n = len(net)
    if n < 3:
        return {'方式': label, '交易笔数': n}
    return {'方式': label, '交易笔数': n, '交易天占比%': n / n_days * 100, '每笔净%': net.mean(),
            't': net.mean() / net.std() * np.sqrt(n) if net.std() > 0 else np.nan, '胜率%': (net > 0).mean() * 100,
            '摊到每天净%': net.sum() / n_days}


def compare(te, cost_col, title, period_col, one_per_day=False, base_rows=None):
    pd.set_option('display.unicode.east_asian_width', True)
    pd.set_option('display.width', 220)
    for code, g in te.groupby('代码'):
        days = g['日期'].nunique()
        base = base_rows[base_rows['代码'] == code] if base_rows is not None else g
        rows = [summary('固定先买后卖', base['y'] - base[cost_col], days),
                summary('固定先卖后买', -base['y'] - base[cost_col], days),
                {'方式': '不做', '交易笔数': 0, '摊到每天净%': 0.0}]
        for pc in [c for c in g.columns if c.startswith('pred_')]:
            for mult in (1.0, 2.0):
                s = settle(g, pc, cost_col, mult, one_per_day)
                rows.append(summary('%s 门槛%.0f倍成本' % (pc[5:], mult), s['net'], days))
        print('\n  %s %s（样本外 %d 天，%s ~ %s）' % (title, code, days, g['日期'].min(), g['日期'].max()))
        print('  ' + pd.DataFrame(rows).round(3).to_string(index=False).replace('\n', '\n  '))
        s = settle(g, 'pred_ridge', cost_col, 1.0, one_per_day)
        if len(s):
            s = s.copy()
            s['期'] = s[period_col]
            yr = s.groupby('期')['net'].agg(['size', 'mean'])
            print('  岭回归（门槛 1 倍）逐期：' + '  '.join('%s:%d笔%+.2f' % (k, r['size'], r['mean']) for k, r in yr.iterrows()))


def run_a(p, notional, refresh):
    print('\n' + '=' * 100 + '\n【模型 A】开盘决策（日线）：预测开盘 → 收盘')
    from qmt_bridge.dat_reader import read_daily
    idx, _ = read_daily('000001.SH')
    idx_env = env_feats(idx, '大盘')
    x = pd.concat([build_a(c, idx_env, refresh) for c in CODES])
    x['日期'] = x.index
    x['年'] = x.index.str[:4]
    x['成本'] = [cost_pct(p, notional, px, 0.0) for px in x['价']]
    feats = feat_list(A_FEATS)
    print('  样本：%s' % '，'.join('%s %d 天' % (c, (x['代码'] == c).sum()) for c in CODES))
    print('\n  单因子 IC（因子和「开→收」的秩相关，三只合并；同号年数 = 逐年 IC 跟总体同号的年数）：')
    ic = ic_table(x, feats, A_FEATS)
    print('  ' + ic.round(3).to_string(index=False).replace('\n', '\n  '))
    te = walk_forward(x, feats, '年', 500, ('ridge', 'gbm'))
    compare(te, '成本', '模型 A', '年')
    os.makedirs(OUT_DIR, exist_ok=True)
    te.to_csv(os.path.join(OUT_DIR, 'A_oos.csv'), encoding='utf-8-sig')
    ic.to_csv(os.path.join(OUT_DIR, 'A_ic.csv'), index=False, encoding='utf-8-sig')
    return x, te


def run_b(p, notional):
    print('\n' + '=' * 100 + '\n【模型 B】盘中决策（5 分钟线）：预测此刻 → 14:55')
    ib = index_5m()
    xs = {c: build_b(c, ib) for c in CODES}
    xs = {c: v for c, v in xs.items() if v is not None and len(v)}
    for c, v in xs.items():
        v['季'] = v['日期'].str[:4] + 'Q' + ((v['日期'].str[4:6].astype(int) - 1) // 3 + 1).astype(str)
        v['年'] = v['日期'].str[:4]
        v['成本'] = [cost_pct(p, notional, px, 0.5) for px in v['价']]
        print('  %s：%d 天，%d 个时点，有大盘数据的时点 %.0f%%' % (c, v['日期'].nunique(), len(v),
                                                       v['大盘开盘以来'].notna().mean() * 100 if '大盘开盘以来' in v else 0))
    feats = feat_list(B_FEATS)
    train = xs.get('600436.SH')
    if train is None:
        print('  600436 没有 5 分钟线，模型 B 跳过')
        return
    for f in feats:
        if f not in train:
            train[f] = np.nan
    print('\n  单因子 IC（600436，因子和「此刻 → 14:55」的秩相关；同号季度数）：')
    ic = ic_table(train, feats, B_FEATS, by='季')
    print('  ' + ic.round(3).to_string(index=False).replace('\n', '\n  '))
    te = walk_forward(train, feats, '季', 120, ('ridge', 'gbm'))
    compare(te, '成本', '模型 B', '季', one_per_day=True,
            base_rows=te[te['时点'] == '1000'].assign(y=te.loc[te['时点'] == '1000', 'y']))
    # 迁移：用 600436 全部数据训练，去预测另外两只最近两个月
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.impute import SimpleImputer
    m = make_pipeline(SimpleImputer(strategy='median'), StandardScaler(), Ridge(alpha=50.0))
    m.fit(train[feats], train['y'].clip(train['y'].quantile(0.01), train['y'].quantile(0.99)))
    for c in ('600366.SH', '605208.SH'):
        v = xs.get(c)
        if v is None:
            continue
        for f in feats:
            if f not in v:
                v[f] = np.nan
        v = v.copy()
        v['pred_ridge'] = m.predict(v[feats])
        compare(v, '成本', '模型 B 迁移（600436 训练）', '季', one_per_day=True, base_rows=v[v['时点'] == '1000'])
    os.makedirs(OUT_DIR, exist_ok=True)
    te.to_csv(os.path.join(OUT_DIR, 'B_oos.csv'), index=False, encoding='utf-8-sig')
    ic.to_csv(os.path.join(OUT_DIR, 'B_ic.csv'), index=False, encoding='utf-8-sig')


def main():
    ap = argparse.ArgumentParser(description='六因子做 T 模型')
    ap.add_argument('--only', choices=['A', 'B'])
    ap.add_argument('--notional', type=float, default=30000, help='每笔成交额（算最低 5 元佣金），默认 3 万')
    ap.add_argument('--refresh-sector', action='store_true')
    a = ap.parse_args()
    p = load_params()
    print('费用：每笔 %.0f 元，佣金万%.1f 最低 %.0f 元 + 印花税 + 过户费；模型 A 不加滑点，模型 B 每边 0.5 个价位' % (
        a.notional, p['commission'] * 1e4, p['min_commission']))
    if a.only != 'B':
        run_a(p, a.notional, a.refresh_sector)
    if a.only != 'A':
        run_b(p, a.notional)


if __name__ == '__main__':
    main()
