# coding:utf-8
"""ETF 盘中快照：按类别、主题汇总涨跌和成交额，加上一交易日的份额变化（净申购/赎回）。

    .venv313\\Scripts\\python.exe tools\\etf_overview.py                 # 默认
    .venv313\\Scripts\\python.exe tools\\etf_overview.py --top 15        # 榜单多显示几名
    .venv313\\Scripts\\python.exe tools\\etf_overview.py --min-amount 1  # 主题成交额门槛（亿），默认 0.5
    .venv313\\Scripts\\python.exe tools\\etf_overview.py --no-save-share # 不往份额目录里补数

数据从哪来：
  * 行情——桥的 get_full_tick，「沪深ETF」板块全部 ETF。要大QMT 开着、「桥接服务」在运行。
  * 名称和份额——桥的 get_instrument_detail。份额是 TotalVolume 字段，对应 TradingDay
    那天收盘（盘中查到的是上一交易日），实测跟上交所/深交所官方公布的数一致。
  * 前一天的份额——共享数据目录 akshare/etf_share/<日期>.csv，由
    `fetch_akshare.py etf_share` 下载（上交所能回补历史，深交所只能每天攒）。
    没有前一天的文件就不出份额那一段。
  * 跑的时候会把 QMT 拿到的份额补进 etf_share/<TradingDay>.csv，只补文件里缺的代码，
    不覆盖交易所官方的数；跟官方数对不上的超过 5% 就不补并提示（--no-save-share 关掉）。

口径：
  * 分类靠名称关键词（货币 > 债券 > 商品 > 宽基 > 跨境 > 策略 > 行业主题；「认购款」占位代码归「其他」
    不参与统计），新出的名字可能分错，
    改 classify() 里的关键词。主题 = 名称里「ETF」前面那段，比如「电池ETF广发」「电池ETF招商」都归「电池」。
  * 类别/主题涨跌幅按成交额加权，成交大的 ETF 权重大。
  * 净申赎（亿元）≈ 份额变化 × 当天收盘价，不是基金公司公布的申赎金额。份额一天变动超过 50%
    多半是拆分/折算，不计入。
  * 债券、货币 ETF 成交额很大但大多是机构 T+0 做现金管理，单列出来，不参与主题排名。

本脚本保持 3.6 兼容（不用 pandas），.venv 和 .venv313 都能跑。
"""
import argparse
import collections
import csv
import datetime
import os
import re
import sys
import time

# 脚本在 tools/ 下，直接跑时 sys.path[0] 是 tools/，把项目根目录加进去才找得到 qmt_bridge
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qmt_bridge.dotenv_lite import load_dotenv  # noqa: E402

load_dotenv()
DATA_DIR = os.environ.get('QMTLOCAL_DATA_DIR') or os.path.join(ROOT, 'real_data')
SHARE_DIR = os.path.join(DATA_DIR, 'akshare', 'etf_share')
SHARE_COLS = ['代码', '名称', '市场', '份额', '来源']

ETF_SECTOR = '沪深ETF'
FULL_TICK_CHUNK = 800
SPLIT_RATIO = 0.5            # 份额一天变化超过这个比例，当拆分/折算处理
MISMATCH_LIMIT = 0.05        # QMT 份额跟官方对不上的比例超过这个就不往文件里补

CATEGORIES = ['宽基', '策略', '行业主题', '跨境', '商品', '债券', '货币']
EQUITY_LIKE = ('宽基', '策略', '行业主题', '跨境', '商品')

# 名称前缀「XD」= 除息日，「DR」= 除权除息
NAME_PREFIX = re.compile(r'^(XD|DR|XR)')
BROAD = {
    '上证50', '上证180', '上证380', '上证指数', '上证综指', '沪深300', '中证500', '中证800',
    '中证1000', '中证2000', '中证A500', 'A500', '中证A50', 'A50', 'MSCI中国A50', '中证100',
    '中证A100', '深证100', '深证成指', '深证50', '创业板', '创业板指', '创业板50', '创业板200',
    '科创50', '科创100', '科创200', '科创板', '科创综指', '科创创业50', '双创', '双创50',
    '北证50', '中小100', '50', '180', '300', '500', '1000', '2000', 'A100', '科创创业',
}
BROAD_ALIAS = {
    '50': '上证50', '180': '上证180', '300': '沪深300', '500': '中证500', '1000': '中证1000',
    '2000': '中证2000', '中证A500': 'A500', '中证A50': 'A50', 'MSCI中国A50': 'A50', '创业板指': '创业板',
    '上证综指': '上证指数', '双创': '双创50', '科创创业50': '双创50', '科创创业': '双创50', 'A100': '中证A100',
}
ENHANCED = re.compile('(增强|指增)$')     # 「A500增强」这种指数增强也算宽基，归到原指数
KW_OTHER = re.compile('认购|申购')        # 新基金认购期的占位代码，不是真在交易的 ETF
KW_MONEY = re.compile('货币|日利|添益|快线|现金(?!流)|保证金|理财')    # 「自由现金流」是股票策略
KW_BOND = re.compile('债|短融|利率|政金|城投')
KW_COMMODITY = re.compile('黄金|上海金|白银|豆粕|期货|商品|有色金属期|能源化工')
KW_CROSS = re.compile('港|恒生|H股|中概|纳斯达克|纳指|标普|道琼斯|日经|东证|德国|法国|中韩|沙特|亚太|'
                      '美国|美股|全球|海外|越南|印度|新加坡|东南亚|欧洲|巴西|日本|韩国|中东|国际')
KW_STRATEGY = re.compile('红利|低波|价值|成长|质量|现金流|股息|央企|国企|基本面|等权|动量|ESG')


# ---------------------------------------------------------------- 分类

def theme_of(name):
    core = NAME_PREFIX.sub('', name or '').split('ETF')[0].strip() or name
    return BROAD_ALIAS.get(core, core)


def classify(name):
    """返回 (类别, 主题)。顺序有讲究：「黄金股」是股票不是商品，「港股通红利」先算跨境。"""
    theme = theme_of(name)
    if KW_OTHER.search(name):
        return '其他', theme
    base = ENHANCED.sub('', theme)
    if base != theme and (base in BROAD or base in BROAD_ALIAS.values()):
        theme = BROAD_ALIAS.get(base, base)
    if KW_MONEY.search(name):
        return '货币', theme
    if KW_BOND.search(name):
        return '债券', theme
    if KW_COMMODITY.search(name) and '股' not in theme:
        return '商品', theme
    if theme in BROAD or theme in BROAD_ALIAS.values():
        return '宽基', theme
    if KW_CROSS.search(name):
        return '跨境', theme
    if KW_STRATEGY.search(name):
        return '策略', theme
    return '行业主题', theme


# ---------------------------------------------------------------- 取数

def gather(xtdata):
    codes = xtdata.get_stock_list_in_sector(ETF_SECTOR) or []
    if not codes:
        raise SystemExit('桥上查不到「%s」板块' % ETF_SECTOR)

    etfs = {}
    for i in range(0, len(codes), FULL_TICK_CHUNK):
        ticks = xtdata.get_full_tick(codes[i:i + FULL_TICK_CHUNK]) or {}
        for code, t in ticks.items():
            pre = float(t.get('lastClose') or 0)
            last = float(t.get('lastPrice') or 0)
            if pre <= 0:
                continue
            etfs[code] = {
                'code': code, 'pre': pre, 'last': last if last > 0 else pre,
                'high': float(t.get('high') or 0), 'low': float(t.get('low') or 0),
                'amount': float(t.get('amount') or 0) / 1e8,
                'traded': last > 0 and float(t.get('amount') or 0) > 0,
            }

    trading_days = collections.Counter()
    for code, e in etfs.items():
        try:
            d = xtdata.get_instrument_detail(code) or {}
        except Exception:
            d = {}
        e['name'] = d.get('InstrumentName') or code
        e['share'] = float(d.get('TotalVolume') or 0)
        e['cat'], e['theme'] = classify(e['name'])
        p = lambda x: (x / e['pre'] - 1) * 100 if x > 0 else 0.0
        e['pct'], e['low_pct'], e['high_pct'] = p(e['last']), p(e['low']), p(e['high'])
        if d.get('TradingDay'):
            trading_days[str(d['TradingDay'])] += 1
    share_day = trading_days.most_common(1)[0][0] if trading_days else None
    return etfs, share_day


# ---------------------------------------------------------------- 份额文件

def share_path(day):
    return os.path.join(SHARE_DIR, day + '.csv')


def read_share(day):
    """返回 {'510300.SH': (份额, 来源)}。"""
    out = {}
    path = share_path(day)
    if not os.path.exists(path):
        return out
    with open(path, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            try:
                out['%s.%s' % (row['代码'].zfill(6), row['市场'])] = (float(row['份额']), row.get('来源', ''))
            except (KeyError, ValueError):
                continue
    return out


def prev_share_day(day):
    if not os.path.isdir(SHARE_DIR):
        return None
    days = sorted(n[:8] for n in os.listdir(SHARE_DIR)
                  if re.match(r'^\d{8}\.csv$', n) and n[:8] < day)
    return days[-1] if days else None


def save_share(etfs, day):
    """把 QMT 份额补进 etf_share/<day>.csv：只补缺的代码，不覆盖官方数；对不上太多就不写。"""
    old = read_share(day)
    overlap = [c for c in old if c in etfs and etfs[c]['share'] > 0]
    bad = [c for c in overlap if abs(etfs[c]['share'] - old[c][0]) > 1]
    if overlap and len(bad) > MISMATCH_LIMIT * len(overlap):
        return '！QMT 份额跟 %s 文件里的官方数有 %d/%d 只对不上，没往里补（可能 TradingDay 跟份额不同步）' \
               % (day, len(bad), len(overlap))
    add = [e for c, e in etfs.items() if c not in old and e['share'] > 0]
    if not add:
        return None

    rows = []
    path = share_path(day)
    if os.path.exists(path):
        with open(path, encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
    for e in add:
        inst, _, market = e['code'].partition('.')
        rows.append({'代码': inst, '名称': e['name'], '市场': market, '份额': '%.0f' % e['share'], '来源': 'qmt'})
    rows.sort(key=lambda r: (r['市场'], r['代码']))

    if not os.path.isdir(SHARE_DIR):
        os.makedirs(SHARE_DIR)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=SHARE_COLS, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    for attempt in range(6):              # OneDrive 正在上传时会短暂锁住目标文件
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(1 + attempt)
    return '（从 QMT 往 %s 补了 %d 只的份额）' % (os.path.relpath(path, DATA_DIR), len(add))


def share_flows(etfs, day, prev_day):
    """份额变化和净申赎（亿元）。返回 {code: (Δ份额, 净申赎亿)} 和疑似拆分的个数。"""
    prev = read_share(prev_day)
    flows, splits = {}, 0
    for code, e in etfs.items():
        if code not in prev or e['share'] <= 0 or prev[code][0] <= 0:
            continue
        delta = e['share'] - prev[code][0]
        if abs(delta) / prev[code][0] > SPLIT_RATIO:
            splits += 1
            continue
        flows[code] = (delta, delta * e['pre'] / 1e8)
    return flows, splits


# ---------------------------------------------------------------- 统计

def weighted(items):
    """成交额加权涨跌、低点、高点；没成交的按等权兜底。"""
    amt = sum(e['amount'] for e in items)
    if amt <= 0:
        n = float(len(items)) or 1
        return tuple(sum(e[k] for e in items) / n for k in ('pct', 'low_pct', 'high_pct'))
    return tuple(sum(e[k] * e['amount'] for e in items) / amt for k in ('pct', 'low_pct', 'high_pct'))


def group(etfs, key):
    out = collections.defaultdict(list)
    for e in etfs:
        out[key(e)].append(e)
    return out


def summarize(items):
    pct, low, high = weighted(items)
    return {
        'n': len(items), 'up': sum(1 for e in items if e['pct'] > 0),
        'down': sum(1 for e in items if e['pct'] < 0),
        'amount': sum(e['amount'] for e in items), 'pct': pct, 'low': low, 'high': high,
        'lead': max(items, key=lambda e: e['amount']),
    }


# ---------------------------------------------------------------- 输出

def pct_str(x):
    return '%+.2f%%' % x


def yi(x):
    return '%.1f亿' % x if abs(x) < 100 else '%.0f亿' % x


def print_theme(name, s):
    print('  %-10s %s  低%s 高%s  成交%7s  %d只  代表 %s %s'
          % (name, pct_str(s['pct']), pct_str(s['low']), pct_str(s['high']), yi(s['amount']),
             s['n'], s['lead']['code'], s['lead']['name']))


def report(etfs, share_day, args):
    now = datetime.datetime.now()
    traded = [e for e in etfs.values() if e['traded']]
    print('=' * 70)
    print('ETF 快照  %s  数据：实时（桥），「%s」%d 只，有成交 %d 只'
          % (now.strftime('%Y-%m-%d %H:%M:%S'), ETF_SECTOR, len(etfs), len(traded)))
    print('=' * 70)

    up = sum(1 for e in traded if e['pct'] > 0)
    down = sum(1 for e in traded if e['pct'] < 0)
    equity = [e for e in traded if e['cat'] in EQUITY_LIKE]
    print('\n【概况】上涨 %d  下跌 %d  平 %d   总成交 %s（其中股票/商品类 %s）'
          % (up, down, len(traded) - up - down, yi(sum(e['amount'] for e in traded)),
             yi(sum(e['amount'] for e in equity))))

    print('\n【分类】（涨跌按成交额加权）')
    cats = group(traded, lambda e: e['cat'])
    for cat in CATEGORIES:
        if cat not in cats:
            continue
        s = summarize(cats[cat])
        print('  %-6s %4d只  上涨 %3d 下跌 %3d  成交%7s  %s  盘中低%s 高%s'
              % (cat, s['n'], s['up'], s['down'], yi(s['amount']), pct_str(s['pct']),
                 pct_str(s['low']), pct_str(s['high'])))

    print('\n【宽基】（按指数归并，成交额从大到小）')
    broad = group([e for e in traded if e['cat'] == '宽基'], lambda e: e['theme'])
    for name, items in sorted(broad.items(), key=lambda kv: -sum(e['amount'] for e in kv[1]))[:args.top]:
        print_theme(name, summarize(items))

    themes = {name: summarize(items) for name, items in
              group([e for e in traded if e['cat'] in ('策略', '行业主题', '跨境', '商品')],
                    lambda e: e['theme']).items()}
    liquid = [(n, s) for n, s in themes.items() if s['amount'] >= args.min_amount]
    liquid.sort(key=lambda kv: -kv[1]['pct'])
    k = min(args.top, len(liquid) // 2)
    print('\n【主题强弱】（行业/策略/跨境/商品，主题成交额 >= %s 亿的 %d 个）' % (args.min_amount, len(liquid)))
    print(' 领涨：')
    for name, s in liquid[:k]:
        print_theme(name, s)
    print(' 领跌：')
    for name, s in liquid[-k:]:
        print_theme(name, s)

    print('\n【盘中异动】（同上范围，按主题）')
    rebound = sorted(liquid, key=lambda kv: -(kv[1]['pct'] - kv[1]['low']))[:min(args.top, 8)]
    print(' 从低点反弹最多：')
    for name, s in rebound:
        print('  %-10s 低%s → 现%s（收回 %.2f 个点）  成交%s'
              % (name, pct_str(s['low']), pct_str(s['pct']), s['pct'] - s['low'], yi(s['amount'])))
    fade = sorted(liquid, key=lambda kv: -(kv[1]['high'] - kv[1]['pct']))[:min(args.top, 8)]
    print(' 从高点回落最多：')
    for name, s in fade:
        print('  %-10s 高%s → 现%s（回落 %.2f 个点）  成交%s'
              % (name, pct_str(s['high']), pct_str(s['pct']), s['high'] - s['pct'], yi(s['amount'])))

    print('\n【成交额前 %d】（股票/商品类）' % args.top)
    for e in sorted(equity, key=lambda e: -e['amount'])[:args.top]:
        print('  %s %-14s %s  低%s 高%s  成交%s  [%s]'
              % (e['code'], e['name'], pct_str(e['pct']), pct_str(e['low_pct']), pct_str(e['high_pct']),
                 yi(e['amount']), e['theme']))

    report_shares(etfs, share_day, args)


def report_shares(etfs, share_day, args):
    if not share_day:
        print('\n【份额】QMT 合约信息里没有 TradingDay，算不了')
        return
    note = None if args.no_save_share else save_share(etfs, share_day)
    prev_day = prev_share_day(share_day)
    print('\n【份额变化】%s → %s 收盘（净申赎 ≈ 份额变化 × 收盘价）' % (prev_day or '?', share_day))
    if note:
        print('  ' + note)
    if not prev_day:
        print('  ！%s 里没有 %s 之前的份额文件，先跑：' % (SHARE_DIR, share_day))
        print('     .venv313\\Scripts\\python.exe tools\\fetch_akshare.py etf_share')
        return
    flows, splits = share_flows(etfs, share_day, prev_day)
    gap = (datetime.datetime.strptime(share_day, '%Y%m%d')
           - datetime.datetime.strptime(prev_day, '%Y%m%d')).days
    if gap > 5:
        print('  ！两个文件隔了 %d 天，下面是这段时间的累计变化' % gap)
    covered = collections.Counter(c.split('.')[1] for c in flows)
    print('  覆盖 %d 只（沪 %d 深 %d）%s'
          % (len(flows), covered['SH'], covered['SZ'],
             '，%d 只份额变动超过 %.0f%% 疑似拆分/折算，未计入' % (splits, SPLIT_RATIO * 100) if splits else ''))
    if not covered['SZ']:
        print('  ！前一天的文件里没有深市，深市 ETF 没算进来（深交所没有历史，要每天跑 fetch_akshare 攒）')

    print('  按类别：')
    by_cat = collections.defaultdict(float)
    for code, (_, flow) in flows.items():
        by_cat[etfs[code]['cat']] += flow
    for cat in CATEGORIES:
        if cat in by_cat:
            print('    %-6s %+9.1f 亿' % (cat, by_cat[cat]))

    by_theme = collections.defaultdict(float)
    for code, (_, flow) in flows.items():
        if etfs[code]['cat'] in EQUITY_LIKE:
            by_theme[(etfs[code]['cat'], etfs[code]['theme'])] += flow
    ranked = sorted(by_theme.items(), key=lambda kv: -kv[1])
    k = min(args.top, len(ranked) // 2)
    print('  净申购最多的主题（不含债券/货币）：')
    for (cat, theme), flow in ranked[:k]:
        print('    %-10s %+8.2f 亿  [%s]' % (theme, flow, cat))
    print('  净赎回最多的主题：')
    for (cat, theme), flow in reversed(ranked[-k:]):
        print('    %-10s %+8.2f 亿  [%s]' % (theme, flow, cat))

    single = sorted(((c, f) for c, f in flows.items() if etfs[c]['cat'] in EQUITY_LIKE),
                    key=lambda kv: -abs(kv[1][1]))[:args.top]
    print('  单只变动最大：')
    for code, (delta, flow) in single:
        print('    %s %-14s 份额 %+8.2f 亿份  ≈ %+7.2f 亿元' % (code, etfs[code]['name'], delta / 1e8, flow))


def main():
    parser = argparse.ArgumentParser(description='ETF 盘中快照：类别/主题强弱 + 份额变化')
    parser.add_argument('--top', type=int, default=10, help='各榜单显示几名，默认 10')
    parser.add_argument('--min-amount', type=float, default=0.5,
                        help='主题强弱只看成交额不低于这么多亿的主题，默认 0.5')
    parser.add_argument('--no-save-share', action='store_true',
                        help='不把 QMT 份额补进 etf_share 目录')
    args = parser.parse_args()

    from qmt_bridge import xtdata
    from qmt_bridge.xtdata import BridgeError
    xtdata.enable_hello = False
    try:
        etfs, share_day = gather(xtdata)
    except BridgeError as e:
        raise SystemExit('%s\n\nETF 快照要走桥，桥没开就看不了。' % e)
    report(etfs, share_day, args)


if __name__ == '__main__':
    sys.exit(main())
