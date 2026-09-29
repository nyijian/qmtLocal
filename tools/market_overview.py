# coding:utf-8
"""A股大盘整体情况快照：指数、涨跌家数、涨跌停、成交额、均线宽度、行业强弱。

    .venv313\\Scripts\\python.exe tools\\market_overview.py              # 盘中/盘后，走桥取实时数据
    .venv313\\Scripts\\python.exe tools\\market_overview.py --offline    # 不走桥，只读本地日线缓存
    .venv313\\Scripts\\python.exe tools\\market_overview.py --top 8      # 行业榜单显示前后各 8 名

数据从哪来：
  * 当日行情（指数、个股现价/昨收/成交额）——桥的 get_full_tick，前提跟 tick_subscribe.py
    一样：大QMT 开着、「模型交易」里「桥接服务」在运行。
  * 均线宽度、创新高新低、历史成交额——大QMT 本地日线缓存（dat_reader），不经过桥。
    不走 get_market_data_ex 是因为桥那边它带 subscribe=True，全市场五千只一起取等于
    在 QMT 里挂五千个订阅，太重。
  * 行业——桥的 get_stock_list_in_sector('SW1银行') 这类申万一级板块，按成分股等权汇总。

本地缓存会滞后（见 dat_reader 顶部说明）。脚本只拿「最后一条是上一个交易日」的股票
算均线类指标，覆盖了多少只会打出来；覆盖率低就去 QMT「数据管理」→「补充数据」把
沪深A股的日线补一遍。缓存是不复权价，除权那几天的均线会有点偏，看整体比例不影响。

--offline 模式下，「今天」就是本地缓存里最新的那个交易日；只算得了缓存新鲜的那部分股票，
没有 ST 名称所以 ST 股的涨跌停不计，也没有行业榜单。
"""
import argparse
import collections
import datetime
import os
import sys

# 脚本在 tools/ 下，直接跑时 sys.path[0] 是 tools/，把项目根目录加进去才找得到 qmt_bridge
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qmt_bridge import dat_reader

INDEXES = [
    ('000001.SH', '上证指数'),
    ('399001.SZ', '深证成指'),
    ('399006.SZ', '创业板指'),
    ('000688.SH', '科创50'),
    ('000300.SH', '沪深300'),
    ('000905.SH', '中证500'),
    ('000852.SH', '中证1000'),
]

# 申万一级行业（2021 版 31 个）。QMT 里的板块名是前缀 + 行业名，前缀用 --sector-prefix 改。
SW1_INDUSTRIES = [
    '农林牧渔', '基础化工', '钢铁', '有色金属', '电子', '家用电器', '食品饮料', '纺织服饰',
    '轻工制造', '医药生物', '公用事业', '交通运输', '房地产', '商贸零售', '社会服务', '综合',
    '建筑材料', '建筑装饰', '电力设备', '国防军工', '计算机', '传媒', '通信', '银行',
    '非银金融', '汽车', '机械设备', '煤炭', '石油石化', '环保', '美容护理',
]

A_SHARE_PREFIXES = {
    'SH': ('600', '601', '603', '605', '688', '689'),
    'SZ': ('000', '001', '002', '003', '300', '301'),
}
ST_LIMIT = 0.05              # 主板 ST 涨跌幅限制；规则变了改这里
HIST_DAYS = 62               # 本地缓存每只读多少条：MA60 要前 59 天，多留几条防当日半根K线
FULL_TICK_CHUNK = 800        # get_full_tick 一次请求多少只，太大单个响应几 MB

PCT_BINS = [(-100, -7), (-7, -5), (-5, -3), (-3, 0), (0, 0), (0, 3), (3, 5), (5, 7), (7, 100)]


# ---------------------------------------------------------------- 基础

def is_a_share(code):
    # 缓存目录里还有 000001_4002.DAT 这种带后缀的，不是普通日线格式，只认 6 位纯数字
    inst, _, market = code.partition('.')
    return (len(inst) == 6 and inst.isdigit()
            and inst.startswith(A_SHARE_PREFIXES.get(market.upper(), ())))


def local_a_share_codes(datadir):
    """桥不通时，从本地缓存目录里列出沪深A股代码（含已退市的，靠日期过滤掉）。"""
    codes = []
    for market in ('SH', 'SZ'):
        folder = os.path.join(datadir or dat_reader.DEFAULT_DATADIR, market, '86400')
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            if name.upper().endswith('.DAT'):
                code = '%s.%s' % (name[:-4], market)
                if is_a_share(code):
                    codes.append(code)
    return sorted(codes)


def limit_ratio(code, is_st):
    inst = code.split('.')[0]
    if inst.startswith(('688', '689', '300', '301')):
        return 0.20
    return ST_LIMIT if is_st else 0.10


def round_price(x):
    """交易所涨跌停价是四舍五入到分，不是银行家舍入。"""
    return int(x * 100 + 0.5 + 1e-9) / 100.0


def pct_of(s):
    return (s['last'] / s['pre'] - 1) * 100 if s['pre'] > 0 else 0.0


def mode_date(dates):
    return collections.Counter(dates).most_common(1)[0][0] if dates else None


def trading_fraction(now):
    """已交易时长占全天 240 分钟的比例，用来把盘中成交额粗略折算成全天。"""
    t = now.hour * 60 + now.minute
    am = min(max(t - (9 * 60 + 30), 0), 120)
    pm = min(max(t - 13 * 60, 0), 120)
    return (am + pm) / 240.0


# ---------------------------------------------------------------- 取数

def load_history(codes, datadir):
    hist = {}
    for code in codes:
        try:
            rows = dat_reader.read_daily_raw(code, datadir, last_n=HIST_DAYS)
        except (FileNotFoundError, dat_reader.DatFormatError):
            continue
        if rows:
            hist[code] = rows
    return hist


def live_snapshot(xtdata, codes):
    snap = {}
    for i in range(0, len(codes), FULL_TICK_CHUNK):
        ticks = xtdata.get_full_tick(codes[i:i + FULL_TICK_CHUNK]) or {}
        for code, t in ticks.items():
            pre = float(t.get('lastClose') or 0)
            if pre <= 0:
                continue
            last = float(t.get('lastPrice') or 0)
            amount = float(t.get('amount') or 0)
            suspended = last <= 0 or amount <= 0
            snap[code] = {
                'last': last if last > 0 else pre, 'pre': pre,
                'high': float(t.get('high') or 0), 'low': float(t.get('low') or 0),
                'amount': amount, 'suspended': suspended,
            }
    return snap


def find_st(xtdata, snap):
    """全推快照里没有名字。只对主板里涨跌幅落在 ±4%~6% 的那些查一下名称，
    判断是不是 ST（ST 涨跌停是 5%），不用挨个查五千只。"""
    st = set()
    for code, s in snap.items():
        if s['suspended'] or limit_ratio(code, False) != 0.10:
            continue
        if 4.0 <= abs(pct_of(s)) <= 6.0:
            try:
                detail = xtdata.get_instrument_detail(code) or {}
            except Exception:
                continue
            if 'ST' in (detail.get('InstrumentName') or '').upper():
                st.add(code)
    return st


def gather_live(args):
    from qmt_bridge import xtdata

    codes = [c for c in (xtdata.get_stock_list_in_sector('沪深A股') or []) if is_a_share(c)]
    if not codes:
        print('（桥上查不到「沪深A股」板块，改用本地缓存目录里的代码列表）')
        codes = local_a_share_codes(args.datadir)

    snap = live_snapshot(xtdata, codes)
    index_ticks = xtdata.get_full_tick([c for c, _ in INDEXES]) or {}
    indexes = []
    for code, name in INDEXES:
        t = index_ticks.get(code) or {}
        pre, last = float(t.get('lastClose') or 0), float(t.get('lastPrice') or 0)
        if pre > 0 and last > 0:
            indexes.append((name, last, (last / pre - 1) * 100))

    today = datetime.date.today()
    hist = load_history(list(snap), args.datadir)
    # 缓存里今天那根（可能是半根）K线不要，今天的数用实时的
    hist = {c: [r for r in rows if r['date'] < today] for c, rows in hist.items()}
    hist = {c: rows for c, rows in hist.items() if rows}
    prev_day = mode_date([rows[-1]['date'] for rows in hist.values()])
    fresh = [c for c, rows in hist.items() if rows[-1]['date'] == prev_day]

    now = datetime.datetime.now()
    frac = trading_fraction(now) if now.weekday() < 5 else 1.0

    return {
        'mode': '实时（桥）', 'asof': now.strftime('%Y-%m-%d %H:%M:%S'),
        'snap': snap, 'st': find_st(xtdata, snap), 'st_known': True,
        'indexes': indexes, 'hist': hist, 'fresh': fresh, 'prev_day': prev_day,
        'trade_frac': frac, 'ref_day': today,
        'sectors': sector_strength(xtdata, snap, args.sector_prefix),
    }


def gather_offline(args):
    codes = local_a_share_codes(args.datadir)
    if not codes:
        raise SystemExit('本地缓存目录里没找到沪深A股日线：%s' % (args.datadir or dat_reader.DEFAULT_DATADIR))
    raw = load_history(codes, args.datadir)
    latest = mode_date([rows[-1]['date'] for rows in raw.values()])

    snap, hist = {}, {}
    for code, rows in raw.items():
        last = rows[-1]
        if last['date'] != latest or last['preClose'] <= 0:
            continue
        snap[code] = {
            'last': last['close'], 'pre': last['preClose'], 'high': last['high'],
            'low': last['low'], 'amount': float(last['amount']),
            'suspended': last['volume'] <= 0,
        }
        if len(rows) > 1:
            hist[code] = rows[:-1]
    prev_day = mode_date([rows[-1]['date'] for rows in hist.values()])
    fresh = [c for c, rows in hist.items() if rows[-1]['date'] == prev_day]

    indexes = []
    for code, name in INDEXES:
        try:
            rows = dat_reader.read_daily_raw(code, args.datadir, last_n=1)
        except (FileNotFoundError, dat_reader.DatFormatError):
            continue
        if rows and rows[-1]['preClose'] > 0:
            r = rows[-1]
            # 指数缓存跟个股不一定同一天刷新，日期不同就标出来
            label = name if r['date'] == latest else '%s(%s)' % (name, r['date'].strftime('%m-%d'))
            indexes.append((label, r['close'], (r['close'] / r['preClose'] - 1) * 100))

    return {
        'mode': '离线（本地缓存）', 'asof': '%s 收盘' % latest,
        'snap': snap, 'st': set(), 'st_known': False,
        'indexes': indexes, 'hist': hist, 'fresh': fresh, 'prev_day': prev_day,
        'trade_frac': 1.0, 'sectors': None, 'ref_day': latest,
        'stale': len(raw) - len(snap), 'latest': latest,
    }


def sector_strength(xtdata, snap, prefix):
    out = []
    for name in SW1_INDUSTRIES:
        try:
            members = xtdata.get_stock_list_in_sector(prefix + name) or []
        except Exception:
            members = []
        pcts = [pct_of(snap[c]) for c in members if c in snap and not snap[c]['suspended']]
        if pcts:
            up = sum(1 for p in pcts if p > 0)
            out.append((name, sum(pcts) / len(pcts), up / float(len(pcts)), len(pcts)))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


# ---------------------------------------------------------------- 统计

def market_breadth(snap, st):
    active = {c: s for c, s in snap.items() if not s['suspended']}
    pcts = sorted(pct_of(s) for s in active.values())
    up = sum(1 for p in pcts if p > 0)
    down = sum(1 for p in pcts if p < 0)

    lu = ld = broken = 0
    for code, s in active.items():
        r = limit_ratio(code, code in st)
        up_px, down_px = round_price(s['pre'] * (1 + r)), round_price(s['pre'] * (1 - r))
        if s['last'] >= up_px - 1e-6:
            lu += 1
        elif s['high'] >= up_px - 1e-6:
            broken += 1          # 摸过涨停没封住
        if s['last'] <= down_px + 1e-6:
            ld += 1

    bins = []
    for lo, hi in PCT_BINS:
        if lo == hi == 0:
            n = sum(1 for p in pcts if p == 0)
        elif hi <= 0:
            n = sum(1 for p in pcts if lo <= p < hi)
        else:
            n = sum(1 for p in pcts if lo < p <= hi)
        bins.append(((lo, hi), n))

    return {
        'total': len(snap), 'active': len(active), 'suspended': len(snap) - len(active),
        'up': up, 'down': down, 'flat': len(pcts) - up - down,
        'median': pcts[len(pcts) // 2] if pcts else 0.0,
        'mean': sum(pcts) / len(pcts) if pcts else 0.0,
        'limit_up': lu, 'limit_down': ld, 'broken': broken, 'bins': bins,
    }


def trend_breadth(snap, hist, fresh):
    n20 = a20 = n60 = a60 = hi = lo = 0
    for code in fresh:
        s = snap.get(code)
        if not s or s['suspended']:
            continue
        rows = hist[code]
        closes = [r['close'] for r in rows]
        px = s['last']
        if len(closes) >= 19:
            n20 += 1
            if px > (sum(closes[-19:]) + px) / 20:
                a20 += 1
            if px > max(r['high'] for r in rows[-19:]):
                hi += 1
            if px < min(r['low'] for r in rows[-19:]):
                lo += 1
        if len(closes) >= 59:
            n60 += 1
            if px > (sum(closes[-59:]) + px) / 60:
                a60 += 1
    return {'n20': n20, 'a20': a20, 'n60': n60, 'a60': a60, 'high20': hi, 'low20': lo}


def turnover(snap, hist, fresh, frac):
    """总成交额用全部股票；跟历史比时只用缓存新鲜的那批，两边口径一致。"""
    total = sum(s['amount'] for s in snap.values())
    same_today = sum(snap[c]['amount'] for c in fresh if c in snap)
    by_date = collections.defaultdict(float)
    for c in fresh:
        for r in hist[c][-20:]:
            by_date[r['date']] += r['amount']
    days = [by_date[d] for d in sorted(by_date)[-20:]]
    avg5 = sum(days[-5:]) / len(days[-5:]) if days else 0
    avg20 = sum(days) / len(days) if days else 0
    projected = same_today / frac if frac > 0 else 0
    return {
        'total': total, 'same_today': same_today, 'projected': projected, 'frac': frac,
        'yesterday': days[-1] if days else 0, 'avg5': avg5, 'avg20': avg20,
    }


# ---------------------------------------------------------------- 输出

def yi(x):
    return '%.0f亿' % (x / 1e8)


def pct_str(x):
    return '%+.2f%%' % x


def ratio_str(a, n):
    return '%d/%d（%.0f%%）' % (a, n, 100.0 * a / n) if n else '-'


def report(d, top):
    snap, hist, fresh = d['snap'], d['hist'], d['fresh']
    b = market_breadth(snap, d['st'])
    t = trend_breadth(snap, hist, fresh)
    v = turnover(snap, hist, fresh, d['trade_frac'])
    # 历史缓存的「上一交易日」离今天太远（中间隔了周末/长假也就几天），
    # 说明缓存没补，均线和量比都是拿旧数据算的，不能当真
    hist_lag = (d['ref_day'] - d['prev_day']).days if d['prev_day'] else None
    hist_stale = hist_lag is None or hist_lag > 11
    if hist_stale:
        v['avg5'] = v['avg20'] = 0

    print('=' * 60)
    print('A股大盘快照  %s  数据：%s' % (d['asof'], d['mode']))
    print('=' * 60)

    print('\n【指数】')
    for name, last, p in d['indexes']:
        print('  %-14s %10.2f  %s' % (name, last, pct_str(p)))
    if not d['indexes']:
        print('  （没取到指数数据）')

    print('\n【涨跌家数】共 %d 只，停牌/无成交 %d 只' % (b['total'], b['suspended']))
    n = b['active'] or 1
    print('  上涨 %d（%.0f%%）  下跌 %d（%.0f%%）  平盘 %d'
          % (b['up'], 100.0 * b['up'] / n, b['down'], 100.0 * b['down'] / n, b['flat']))
    print('  个股涨跌幅中位数 %s，平均 %s' % (pct_str(b['median']), pct_str(b['mean'])))
    st_note = '' if d['st_known'] else '（离线模式不含 ST 股）'
    print('  涨停 %d  跌停 %d  炸板 %d%s' % (b['limit_up'], b['limit_down'], b['broken'], st_note))

    print('\n【涨跌幅分布】')
    peak = max(cnt for _, cnt in b['bins']) or 1
    for (lo, hi), cnt in b['bins']:
        if lo == hi == 0:
            label = '平盘'
        elif lo == -100:
            label = '< %d%%' % hi
        elif hi == 100:
            label = '> +%d%%' % lo
        else:
            label = '%+d%% ~ %+d%%' % (lo, hi)
        print('  %-12s %5d  %s' % (label, cnt, '#' * int(40.0 * cnt / peak)))

    print('\n【成交额】')
    print('  两市A股成交 %s' % yi(v['total']))
    if v['avg5']:
        base = v['projected']
        tag = ''
        if v['frac'] < 1.0:
            tag = '（盘中，已交易 %.0f%%，按时间线性折算全天约 %s，仅供参考）' % (100 * v['frac'], yi(base))
        print('  对比口径（缓存新鲜的 %d 只）：今日 %s%s' % (len(fresh), yi(v['same_today']), tag))
        print('  昨日 %s  5日均 %s  20日均 %s' % (yi(v['yesterday']), yi(v['avg5']), yi(v['avg20'])))
        print('  量比：对5日均 %.2f，对20日均 %.2f' % (base / v['avg5'], base / v['avg20'] if v['avg20'] else 0))

    if hist_stale:
        print('  ！本地日线缓存只到 %s，没法跟近几日比量能；补完「补充数据」再看' % d['prev_day'])

    print('\n【趋势结构】（基于本地缓存，上一交易日 %s，覆盖 %d 只）' % (d['prev_day'], len(fresh)))
    if hist_stale:
        print('  ！缓存滞后 %s 天，下面几项是拿旧历史算的，仅供参考' % hist_lag)
    print('  站上20日线 %s' % ratio_str(t['a20'], t['n20']))
    print('  站上60日线 %s' % ratio_str(t['a60'], t['n60']))
    print('  创20日新高 %d  创20日新低 %d' % (t['high20'], t['low20']))
    coverage = len(fresh) / float(len(snap)) if snap else 0
    if coverage < 0.8:
        print('  ！覆盖率只有 %.0f%%，去 QMT「数据管理」→「补充数据」补沪深A股日线再跑' % (100 * coverage))

    sectors = d['sectors']
    if sectors is not None:
        print('\n【行业强弱】（申万一级，成分股等权平均涨幅）')
        if not sectors:
            print('  （一个板块都没查到，板块名前缀可能不对，用 --sector-prefix 改）')
        else:
            k = min(top, len(sectors) // 2) or len(sectors)
            print('  领涨：')
            for name, avg, upr, cnt in sectors[:k]:
                print('    %-6s %s  上涨占比 %.0f%%（%d只）' % (name, pct_str(avg), 100 * upr, cnt))
            print('  领跌：')
            for name, avg, upr, cnt in sectors[-k:]:
                print('    %-6s %s  上涨占比 %.0f%%（%d只）' % (name, pct_str(avg), 100 * upr, cnt))

    print('\n【解读】')
    for line in interpret(d['indexes'], b, None if hist_stale else t, v):
        print('  - ' + line)
    print()


def interpret(indexes, b, t, v):
    out = []
    n = b['active'] or 1
    upr = b['up'] / float(n)
    if upr >= 0.7:
        out.append('赚钱效应强：七成以上个股上涨')
    elif upr >= 0.5:
        out.append('赚钱效应偏强：上涨家数过半')
    elif upr >= 0.3:
        out.append('赚钱效应偏弱：多数个股下跌')
    else:
        out.append('赚钱效应差：七成以上个股下跌')

    sh = dict((name, p) for name, _, p in indexes).get('上证指数')
    if sh is not None:
        if sh > 0.3 and b['median'] < 0:
            out.append('指数涨但中位数个股跌：权重拉指数，个股分化')
        elif sh < -0.3 and b['median'] > 0:
            out.append('指数跌但中位数个股涨：权重拖累，中小票相对活跃')

    if b['limit_up'] or b['limit_down']:
        if b['limit_up'] >= 3 * max(b['limit_down'], 1):
            out.append('涨停 %d 远多于跌停 %d，短线情绪偏热' % (b['limit_up'], b['limit_down']))
        elif b['limit_down'] >= 2 * max(b['limit_up'], 1):
            out.append('跌停 %d 多于涨停 %d，短线情绪偏冷' % (b['limit_down'], b['limit_up']))
        if b['broken'] and b['broken'] >= b['limit_up'] * 0.5:
            out.append('炸板 %d 家，封板率不高，追高要谨慎' % b['broken'])

    if v['avg5']:
        r = v['projected'] / v['avg5']
        if r >= 1.2:
            out.append('相对5日均量放量（%.2f倍）' % r)
        elif r <= 0.8:
            out.append('相对5日均量缩量（%.2f倍）' % r)
        else:
            out.append('量能跟近5日持平')

    if t and t['n20']:
        a20 = t['a20'] / float(t['n20'])
        if a20 >= 0.6:
            out.append('%.0f%% 个股站上20日线，短期趋势偏多' % (100 * a20))
        elif a20 <= 0.4:
            out.append('只有 %.0f%% 个股站上20日线，短期趋势偏空' % (100 * a20))
    return out


def main():
    parser = argparse.ArgumentParser(description='A股大盘整体情况快照')
    parser.add_argument('--offline', action='store_true',
                        help='不走桥，只用本地日线缓存（盘后/桥没开时用）')
    parser.add_argument('--datadir', default=None,
                        help='大QMT 日线缓存目录，默认 %s' % dat_reader.DEFAULT_DATADIR)
    parser.add_argument('--sector-prefix', default='SW1',
                        help="行业板块名前缀，默认 'SW1'（即 'SW1银行' 这种）")
    parser.add_argument('--top', type=int, default=5, help='行业榜单前后各显示几名')
    args = parser.parse_args()

    if args.offline:
        d = gather_offline(args)
        if d.get('stale'):
            print('（本地缓存里 %d 只最后一条不是 %s，停牌或缓存滞后，未计入）'
                  % (d['stale'], d['latest']))
        lag = (datetime.date.today() - d['latest']).days
        if lag > 4:
            print('！本地缓存最新只到 %s，距今 %d 天。要看最近的行情，先去 QMT「数据管理」→'
                  '「补充数据」补沪深A股日线，或者开桥不加 --offline 跑。' % (d['latest'], lag))
    else:
        from qmt_bridge.xtdata import BridgeError
        try:
            d = gather_live(args)
        except BridgeError as e:
            raise SystemExit('%s\n\n桥没开的话可以先用 --offline 看本地缓存里最近一个交易日的情况。' % e)
    report(d, args.top)


if __name__ == '__main__':
    sys.exit(main())
