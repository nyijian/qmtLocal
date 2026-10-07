# coding:utf-8
"""近两周热点扫描：连板梯队 + 龙虎榜资金动向 + 两融资金变化，汇总成一份文字报告。

**只在 .venv313 下运行**（跟 fetch_akshare.py 一样，要 akshare/pandas 新版）。

    .venv313\\Scripts\\python.exe tools\\hotspot_scan.py                    # 近 14 天（默认）
    .venv313\\Scripts\\python.exe tools\\hotspot_scan.py --days 21 --top 15
    .venv313\\Scripts\\python.exe tools\\hotspot_scan.py --no-save           # 只打印，不落盘

数据来源：只读本地缓存，不联网
--------------------------------
涨停/炸板池、龙虎榜、两融全部读 `tools/fetch_akshare.py` 已经下好的本地缓存
（`real_data/akshare/` 或 `.env` 的 `QMTLOCAL_DATA_DIR`），**这边不会自己联网下**。
跟持续维护的下载脚本分成两件事：数据新不新鲜是 `fetch_akshare.py` 的职责（它有
增量、重试、限流熔断那一套），这边只管读、算、拼报告。本地缓存覆盖不到窗口期的
天数会在输出里提示，照着提示跑一遍：

    .venv313\\Scripts\\python.exe tools\\fetch_akshare.py zt_pool lhb margin

字段名为什么是「关键词匹配」出来的
----------------------------------
akshare 这几个接口的列名在不同版本之间改过好几次（这仓库 `fetch_akshare.py` 的
开头注释也提过"网站一改版接口就可能坏"）。这边没有硬编码死列名，而是用 `pick_col()`
按关键词模糊找——找不到就老实抛错、把实际列名打出来，不会算出一个错的数字还装作
没事。真遇到报错，把打印出来的列名列表发回来对一下就能改。

口径上的近似，别较真
---------------------
* 涨停/连板认定直接拿 akshare 涨停池（`stock_zt_pool_em`）的结果，不是自己按涨跌幅
  阈值算的，跟交易所口径一致，这点不用担心。
* 活跃营业部（`lhb_yyb`）只是金额排行，不是"游资画像"——这边不维护知名游资席位名单，
  谁是谁自己判断。
* 两融"净买入"= 融资买入额 - 融资偿还额，不代表真实持仓变化（可能当天买也当天还）。
"""
import argparse
import datetime as dt
import os
import sys
import warnings

warnings.filterwarnings('ignore')

import akshare as ak
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from qmt_bridge.dotenv_lite import load_dotenv  # noqa: E402

load_dotenv()
DATA_DIR = os.environ.get('QMTLOCAL_DATA_DIR') or os.path.join(ROOT, 'real_data')
AK_DIR = os.path.join(DATA_DIR, 'akshare')


def log(msg):
    print('%s %s' % (dt.datetime.now().strftime('%H:%M:%S'), msg), flush=True)


def pick_col(df, *candidates, required=True):
    """按关键词找列名。candidates 每项是字符串或字符串元组（元组内关键词要同时出现）；
    按顺序尝试，命中第一个就返回。找不到且 required 时抛错，把实际列名列出来。
    """
    for cand in candidates:
        keys = (cand,) if isinstance(cand, str) else cand
        for col in df.columns:
            if all(k in col for k in keys):
                return col
    if required:
        raise KeyError('没找到匹配 %r 的列，现有列：%s' % (candidates, list(df.columns)))
    return None


def fetch_retry(label, fn, retries=3, pause=1.0):
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == retries:
                log('  ！%s 失败（%s: %s），跳过' % (label, type(e).__name__, e))
                return None
            import time
            time.sleep(pause * attempt)


# ---------------------------------------------------------------- 窗口期

def trading_days_in_window(days):
    """近 `days` 个日历日里有哪些交易日，升序。用新浪的交易日历接口，跟
    fetch_akshare.py 的 trading_days() 是同一个数据源。"""
    today = dt.date.today()
    start = today - dt.timedelta(days=days)
    cal = fetch_retry('交易日历', ak.tool_trade_date_hist_sina)
    if cal is None:
        raise SystemExit('取不到交易日历，检查网络后重试。')
    all_days = pd.to_datetime(cal['trade_date']).dt.date
    return sorted(d for d in all_days if start <= d <= today)


# ---------------------------------------------------------------- 涨停/炸板（读本地缓存）

def _to_int(x, default=0):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


def load_zt_day(folder, d):
    """读 fetch_akshare.py 存的 zt_pool / zt_pool_zbgc 某一天的文件，没有就 None。"""
    path = os.path.join(AK_DIR, folder, d.strftime('%Y%m%d') + '.csv')
    if not os.path.exists(path):
        return None
    return pd.read_csv(path, dtype=str)


def scan_zt_pool(trade_days):
    """逐日读本地涨停池 + 炸板池缓存。返回 (daily_rows, zt_frames)：
    daily_rows 是 [(日期, 涨停家数, 炸板家数, 炸板率, 最高连板数), ...]；
    zt_frames 是 {日期: 涨停池DataFrame}，留给后面统计个股涨停次数和最新连板梯队用。
    """
    daily_rows = []
    zt_frames = {}
    missing = []
    for d in trade_days:
        zt = load_zt_day('zt_pool', d)
        if zt is None:
            missing.append(d)
            continue
        zb = load_zt_day('zt_pool_zbgc', d)
        zt_frames[d] = zt
        zt_n = len(zt)
        zb_n = len(zb) if zb is not None else 0
        zb_rate = zb_n / (zt_n + zb_n) * 100 if (zt_n + zb_n) else 0.0
        lb_col = pick_col(zt, '连板', required=False)
        max_lb = max((_to_int(v, 1) for v in zt[lb_col]), default=0) if lb_col and zt_n else (1 if zt_n else 0)
        daily_rows.append((d, zt_n, zb_n, zb_rate, max_lb))
        log('  %s：涨停 %d 只，炸板 %d 只（炸板率 %.1f%%），最高连板 %d'
            % (d, zt_n, zb_n, zb_rate, max_lb))
    if missing:
        log('  ！本地没有这些交易日的涨停池缓存，先跑 fetch_akshare.py zt_pool（--zt-days 要覆盖到这些日子）：%s'
            % ', '.join(d.strftime('%Y-%m-%d') for d in missing))
    return daily_rows, zt_frames


def summarize_zt(zt_frames, top):
    """汇总窗口内涨停次数最多的个股、所属行业分布、最新一天的连板梯队。"""
    if not zt_frames:
        return [], [], []

    code_col = pick_col(next(iter(zt_frames.values())), '代码')
    name_col = pick_col(next(iter(zt_frames.values())), '名称')
    lb_col = pick_col(next(iter(zt_frames.values())), '连板', required=False)
    ind_col = pick_col(next(iter(zt_frames.values())), '所属行业', required=False)

    counts = {}      # code -> [次数, 名称, 最高连板, 行业]
    industry_hits = {}
    for d, df in zt_frames.items():
        for _, row in df.iterrows():
            code = str(row[code_col])
            name = str(row[name_col])
            lb = _to_int(row[lb_col], 1) if lb_col else 1
            ind = str(row[ind_col]) if ind_col else '未知'
            if code not in counts:
                counts[code] = [0, name, 0, ind]
            counts[code][0] += 1
            counts[code][2] = max(counts[code][2], lb)
            industry_hits[ind] = industry_hits.get(ind, 0) + 1

    top_stocks = sorted(counts.items(), key=lambda kv: kv[1][0], reverse=True)[:top]
    top_industries = sorted(industry_hits.items(), key=lambda kv: kv[1], reverse=True)[:top]

    last_day = max(zt_frames)
    last_df = zt_frames[last_day]
    ladder = []
    if lb_col:
        tail = last_df.assign(_lb=[_to_int(v, 1) for v in last_df[lb_col]]).sort_values('_lb', ascending=False)
        for _, row in tail.iterrows():
            if row['_lb'] < 2:
                continue
            ladder.append((str(row[code_col]), str(row[name_col]), int(row['_lb']),
                          str(row[ind_col]) if ind_col else '未知'))
    return top_stocks, top_industries, (last_day, ladder)


# ---------------------------------------------------------------- 本地缓存读取（龙虎榜/两融）

def month_range(start, end):
    return pd.period_range(start, end, freq='M')


def load_monthly_csvs(folder, start, end):
    """lhb / lhb_jg / lhb_yyb 都是按月存的文件，读窗口覆盖到的那几个月份，拼起来。"""
    frames = []
    missing = []
    for m in month_range(start, end):
        path = os.path.join(AK_DIR, folder, '%s.csv' % m)
        if not os.path.exists(path):
            missing.append(str(m))
            continue
        with open(path, 'rb') as f:
            head = f.read(2)
        df = pd.read_csv(path, dtype=str) if head == b'\xef\xbb' else pd.read_csv(path)
        frames.append(df)
    if missing:
        log('  ！%s 缺这些月份的本地缓存：%s（先跑 fetch_akshare.py 补）' % (folder, ', '.join(missing)))
    return pd.concat(frames, ignore_index=True) if frames else None


def filter_by_date(df, window_start, window_end):
    """按日期列过滤到窗口内。找不到看得懂的日期列就不过滤，原样返回并打一句提示。"""
    date_col = pick_col(df, '上榜日', ('上榜', '日期'), '交易日期', '日期', required=False)
    if date_col is None:
        log('  ！没找到日期列（现有列：%s），这部分按整月覆盖算，没按窗口精确过滤' % list(df.columns))
        return df
    dates = pd.to_datetime(df[date_col], errors='coerce').dt.date
    mask = (dates >= window_start) & (dates <= window_end)
    return df[mask]


def to_numeric(series):
    return pd.to_numeric(series, errors='coerce').fillna(0)


# ---------------------------------------------------------------- 龙虎榜汇总

def summarize_lhb(window_start, window_end, top):
    df = load_monthly_csvs('lhb', window_start, window_end)
    if df is None or not len(df):
        return None
    df = filter_by_date(df, window_start, window_end)
    if not len(df):
        return []
    code_col = pick_col(df, '代码')
    name_col = pick_col(df, '名称')
    net_col = pick_col(df, ('龙虎榜', '净买'), '净买额', required=False)
    reason_col = pick_col(df, '上榜原因', '原因', required=False)
    net_vals = to_numeric(df[net_col]) if net_col else pd.Series(0.0, index=df.index)

    agg = {}
    for (_, row), net in zip(df.iterrows(), net_vals):
        code = str(row[code_col])
        if code not in agg:
            agg[code] = [str(row[name_col]), 0.0, 0, set()]
        agg[code][1] += float(net)
        agg[code][2] += 1
        if reason_col:
            agg[code][3].add(str(row[reason_col]))
    ranked = sorted(agg.items(), key=lambda kv: abs(kv[1][1]), reverse=True)[:top]
    return [(code, name, net, cnt, '、'.join(list(reasons)[:3]))
            for code, (name, net, cnt, reasons) in ranked]


def summarize_lhb_jg(window_start, window_end, top):
    df = load_monthly_csvs('lhb_jg', window_start, window_end)
    if df is None or not len(df):
        return None
    df = filter_by_date(df, window_start, window_end)
    if not len(df):
        return []
    code_col = pick_col(df, '代码')
    name_col = pick_col(df, '名称')
    # 注意：不能只靠「净买额」这四个字模糊找——这张表里还有一列
    # 「机构净买额占总成交额比」，字面也带「净买额」，但那是百分比不是金额，
    # 之前就是被这列骗过。所有候选词都强制带「买入」，把比例列排除掉。
    net_col = pick_col(df, '机构买入净额', ('机构', '买入', '净额'), required=False)
    if net_col is None:
        log('  ！lhb_jg 没找到机构净买额相关列（现有列：%s），跳过这部分' % list(df.columns))
        return []
    net_vals = to_numeric(df[net_col])
    agg = {}
    for (_, row), net in zip(df.iterrows(), net_vals):
        code = str(row[code_col])
        if code not in agg:
            agg[code] = [str(row[name_col]), 0.0, 0]
        agg[code][1] += float(net)
        agg[code][2] += 1
    ranked = sorted(agg.items(), key=lambda kv: kv[1][1], reverse=True)[:top]
    return [(code, name, net, cnt) for code, (name, net, cnt) in ranked]


def summarize_lhb_yyb(window_start, window_end, top):
    df = load_monthly_csvs('lhb_yyb', window_start, window_end)
    if df is None or not len(df):
        return None
    df = filter_by_date(df, window_start, window_end)
    if not len(df):
        return []
    name_col = pick_col(df, '营业部')
    amt_col = pick_col(df, ('买入', '金额'), ('买入', '额'), '买入总额', required=False)
    if amt_col is None:
        log('  ！lhb_yyb 没找到买入金额相关列（现有列：%s），跳过这部分' % list(df.columns))
        return []
    amt_vals = to_numeric(df[amt_col])
    agg = {}
    for (_, row), amt in zip(df.iterrows(), amt_vals):
        name = str(row[name_col])
        agg[name] = agg.get(name, 0.0) + float(amt)
    return sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:top]


# ---------------------------------------------------------------- 两融汇总

def summarize_margin_trend(window_start, window_end):
    """沪深两市融资余额，窗口首尾对比。

    两边单位不一样：`stock_margin_sse` 给的是原始元，`stock_margin_szse` 给的是
    「亿元」（实测核对过：某天 SZSE 报 12519.88，对应的是 1.25 万亿元级别，跟
    SSE 当天的量级吻合）。这里统一换算成元，不然两行数字摆在一起会以为深市只有
    沪市的万分之一，误导人。
    """
    out = []
    specs = [('summary_sse.csv', '信用交易日期', 1), ('summary_szse.csv', '日期', 1e8)]
    for fname, date_col_hint, unit in specs:
        path = os.path.join(AK_DIR, 'margin', fname)
        if not os.path.exists(path):
            log('  ！没有 %s 本地缓存，先跑 fetch_akshare.py margin' % fname)
            continue
        df = pd.read_csv(path, dtype={date_col_hint: str})
        date_col = pick_col(df, date_col_hint, required=False) or date_col_hint
        bal_col = pick_col(df, '融资余额', required=False)
        if bal_col is None:
            log('  ！%s 没找到融资余额列（现有列：%s）' % (fname, list(df.columns)))
            continue
        dates = pd.to_datetime(df[date_col], format='%Y%m%d', errors='coerce')
        dates = dates.fillna(pd.to_datetime(df[date_col], errors='coerce'))
        mask = (dates.dt.date >= window_start) & (dates.dt.date <= window_end)
        sub = df[mask].copy()
        sub['_d'] = dates[mask]
        sub = sub.sort_values('_d')
        if len(sub) < 2:
            continue
        first = float(sub[bal_col].iloc[0]) * unit
        last = float(sub[bal_col].iloc[-1]) * unit
        out.append((fname.replace('summary_', '').replace('.csv', '').upper(),
                   sub['_d'].iloc[0].date(), sub['_d'].iloc[-1].date(), first, last))
    return out


def summarize_margin_detail(window_start, window_end, trade_days, top):
    """个股两融排行：沪深分开算、分开排，不混在一张榜里。

    深交所的两融个股明细接口（`stock_margin_detail_szse`）压根没有「融资偿还额」
    这一列，没法算真正的净买入，只能退到「融资买入额」（毛买入，没扣偿还）；
    沪交所有偿还额，算的是真净买入。这是两种不同的口径，混在一起排名会让"毛买入
    很大但偿还也很大"的深市标的显得比实际更强，所以干脆按市场分成两张榜，各自
    标清楚用的是哪种口径。
    """
    results = {}
    for market, folder in (('SSE', 'detail_sse'), ('SZSE', 'detail_szse')):
        agg = {}
        covered = 0
        has_repay = False
        for d in trade_days:
            path = os.path.join(AK_DIR, 'margin', folder, d.strftime('%Y%m%d') + '.csv')
            if not os.path.exists(path):
                continue
            df = pd.read_csv(path, dtype=str)
            code_col = pick_col(df, '证券代码', required=False)
            name_col = pick_col(df, '证券简称', '名称', required=False)
            buy_col = pick_col(df, '融资买入额', required=False)
            repay_col = pick_col(df, '融资偿还额', required=False)
            if not (code_col and buy_col):
                log('  ！%s/%s 列名对不上（现有列：%s），这天跳过'
                    % (folder, d, list(df.columns)))
                continue
            covered += 1
            buy = to_numeric(df[buy_col])
            if repay_col is not None:
                has_repay = True
                net = buy - to_numeric(df[repay_col])
            else:
                net = buy
            for code, name, n in zip(df[code_col], df[name_col] if name_col else df[code_col], net):
                if code not in agg:
                    agg[code] = [str(name), 0.0]
                agg[code][1] += float(n)
        if covered == 0:
            results[market] = None
            continue
        ranked = sorted(agg.items(), key=lambda kv: kv[1][1], reverse=True)[:top]
        results[market] = {
            'is_net': has_repay,
            'covered': covered,
            'rows': [(code, name, net) for code, (name, net) in ranked],
        }
    return results


# ---------------------------------------------------------------- 报告拼装

def build_report(args):
    trade_days = trading_days_in_window(args.days)
    if not trade_days:
        raise SystemExit('窗口期里没有交易日（周末/假期太长？），加大 --days 再试。')
    window_start, window_end = trade_days[0], trade_days[-1]
    lines = []
    lines.append('=' * 60)
    lines.append('A股热点扫描报告　窗口：%s ~ %s（%d 个交易日）' % (window_start, window_end, len(trade_days)))
    lines.append('生成时间：%s' % dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    lines.append('=' * 60)

    log('【1/3】涨停/炸板池（读本地缓存）')
    daily_rows, zt_frames = scan_zt_pool(trade_days)
    lines.append('\n## 一、涨停/连板梯队\n')
    if daily_rows:
        lines.append('日期        涨停  炸板  炸板率  最高连板')
        for d, zt_n, zb_n, zb_rate, max_lb in daily_rows:
            lines.append('%s  %4d  %4d  %5.1f%%   %d' % (d, zt_n, zb_n, zb_rate, max_lb))
        top_stocks, top_industries, (last_day, ladder) = summarize_zt(zt_frames, args.top)
        lines.append('\n窗口内涨停次数最多（Top %d）：' % args.top)
        for code, (n, name, max_lb, ind) in top_stocks:
            lines.append('  %-11s %-8s 涨停%2d次  最高%d连板  行业：%s' % (code, name, n, max_lb, ind))
        lines.append('\n涨停扎堆的行业（Top %d，按出现频次）：' % args.top)
        for ind, n in top_industries:
            lines.append('  %-10s %d 次' % (ind, n))
        lines.append('\n最新交易日 %s 的连板梯队（2 连板及以上）：' % last_day)
        if ladder:
            for code, name, lb, ind in ladder:
                lines.append('  %-11s %-8s %d 连板  行业：%s' % (code, name, lb, ind))
        else:
            lines.append('  没有 2 连板及以上的——情绪比较分散/冷淡')
    else:
        lines.append('没查到任何一天的涨停池数据（网络问题？），这部分跳过')

    log('【2/3】龙虎榜资金动向（读本地缓存）')
    lines.append('\n## 二、龙虎榜资金动向\n')
    lhb = summarize_lhb(window_start, window_end, args.top)
    if lhb is None:
        lines.append('本地没有龙虎榜缓存：.venv313\\Scripts\\python.exe tools\\fetch_akshare.py lhb')
    else:
        lines.append('上榜次数/净买额最高（Top %d，净买额单位：元）：' % args.top)
        for code, name, net, cnt, reasons in lhb:
            lines.append('  %-11s %-8s 净买额 %14.0f  上榜 %d 次  原因：%s' % (code, name, net, cnt, reasons))

    lhb_jg = summarize_lhb_jg(window_start, window_end, args.top)
    lines.append('\n机构净买入最高（Top %d）：' % args.top)
    if lhb_jg is None:
        lines.append('本地没有机构买卖缓存（同上，跑 fetch_akshare.py lhb）')
    elif not lhb_jg:
        lines.append('窗口内没有机构上榜记录，或者列名对不上（看上面的警告）')
    else:
        for code, name, net, cnt in lhb_jg:
            lines.append('  %-11s %-8s 机构净买额 %14.0f  上榜 %d 次' % (code, name, net, cnt))

    lhb_yyb = summarize_lhb_yyb(window_start, window_end, args.top)
    lines.append('\n活跃营业部买入金额最高（Top %d，不等于"知名游资"）：' % args.top)
    if lhb_yyb is None:
        lines.append('本地没有活跃营业部缓存（同上）')
    elif not lhb_yyb:
        lines.append('窗口内没有数据，或者列名对不上（看上面的警告）')
    else:
        for name, amt in lhb_yyb:
            lines.append('  %-20s 买入金额 %14.0f' % (name, amt))

    log('【3/3】两融资金变化（读本地缓存）')
    lines.append('\n## 三、两融资金变化\n')
    trend = summarize_margin_trend(window_start, window_end)
    if not trend:
        lines.append('本地没有两融汇总缓存：.venv313\\Scripts\\python.exe tools\\fetch_akshare.py margin')
    else:
        for market, d0, d1, first, last in trend:
            chg = last - first
            pct = chg / first * 100 if first else 0
            lines.append('  %s 融资余额：%s %.0f -> %s %.0f（%+.0f，%+.1f%%）'
                        % (market, d0, first, d1, last, chg, pct))

    detail = summarize_margin_detail(window_start, window_end, trade_days, args.top)
    for market in ('SSE', 'SZSE'):
        info = detail.get(market)
        if info is None:
            lines.append('\n%s 个股两融排行：本地没有明细缓存（fetch_akshare.py margin --detail-years ...）' % market)
            continue
        if info['is_net']:
            label, title = '净买入', '融资净买入'
        else:
            label = '买入额'
            title = '融资买入额（该所明细没有偿还额列，这是毛买入不是净值，别跟 SSE 的净买入直接比）'
        lines.append('\n%s 个股%s最高（Top %d，覆盖 %d 天本地明细）：'
                    % (market, title, args.top, info['covered']))
        if not info['rows']:
            lines.append('  窗口内没有数据，或者列名对不上（看上面的警告）')
        else:
            for code, name, net in info['rows']:
                lines.append('  %-11s %-8s %s %14.0f' % (code, name, label, net))

    lines.append('\n' + '=' * 60)
    lines.append('口径说明见脚本开头的文档字符串；营业部排行是金额近似值，别当成精确的游资画像用。')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='近两周 A 股热点扫描：连板梯队 + 龙虎榜 + 两融')
    parser.add_argument('--days', type=int, default=14, help='回看多少个日历日，默认 14（约两周）')
    parser.add_argument('--top', type=int, default=10, help='每个排行榜显示前几名，默认 10')
    parser.add_argument('--no-save', action='store_true', help='只打印，不落盘保存报告文件')
    args = parser.parse_args()

    report = build_report(args)
    print('\n' + report)

    if not args.no_save:
        out_dir = os.path.join(DATA_DIR, '_reports')
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, '热点报告_%s.md' % dt.date.today().strftime('%Y%m%d'))
        with open(path, 'w', encoding='utf-8-sig') as f:
            f.write(report)
        log('已保存到 %s' % path)


if __name__ == '__main__':
    main()
