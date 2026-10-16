# coding:utf-8
"""看持仓股票的行情：集合竞价、开盘、盘中每分钟一行。只读，不下单。

前提同 trade_monitor.py：大QMT已登录，「模型交易」里「桥接服务」在运行（周期「分笔线」），
本脚本跑在项目的 .venv（py3.6）里。

    .venv\\Scripts\\python.exe tools\\position_watch.py                    # 打一张快照就退（9:15~9:25 看到的就是竞价）
    .venv\\Scripts\\python.exe tools\\position_watch.py --watch            # 每分钟一行，到 15:00 自动退
    .venv\\Scripts\\python.exe tools\\position_watch.py --watch -i 30 --until 11:30
    .venv\\Scripts\\python.exe tools\\position_watch.py --watch --csv      # 顺手记到共享数据目录 position_watch\\<日期>.csv
    .venv\\Scripts\\python.exe tools\\position_watch.py 000001.SZ 600000.SH   # 持仓之外再加几只一起看
    .venv\\Scripts\\python.exe tools\\position_watch.py --no-positions 000001.SZ   # 只看指定代码，不查账号

竞价阶段（9:15~9:25）买一、卖一价量相同，那就是虚拟成交价和可匹配量；成交额要 9:25 撮合后才有。
--watch 时某只创当日新高/新低、或一个间隔内涨跌超过 --alert（默认 1%）会在行尾标 [新高]/[新低]/[急涨]/[急跌]。
午休 11:30~13:00 不打印。
"""
import argparse
import datetime as dt
import os
import sys
import time

# 脚本在 tools/ 下，直接跑时 sys.path[0] 是 tools/，把项目根目录加进去才找得到 qmt_bridge
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qmt_bridge import xtdata
from qmt_bridge.dotenv_lite import load_dotenv
from qmt_bridge.xttrader import XtQuantTrader
from qmt_bridge.xttype import StockAccount

load_dotenv()
xtdata.enable_hello = False
DEFAULT_ACCOUNT = os.environ.get('QMT_ACCOUNT_ID', '')
DEFAULT_ACCOUNT_TYPE = os.environ.get('QMT_ACCOUNT_TYPE', 'STOCK')

LUNCH = (dt.time(11, 30, 30), dt.time(12, 59, 30))


def _first(seq):
    return seq[0] if seq else 0


def _pct(a, b):
    return (a / b - 1) * 100 if a and b else 0.0


def resolve_account(trader, account_id, account_type):
    if account_id:
        return StockAccount(account_id, account_type)
    acc = trader.get_default_account()
    if acc is not None:
        return acc
    if DEFAULT_ACCOUNT:
        return StockAccount(DEFAULT_ACCOUNT, DEFAULT_ACCOUNT_TYPE)
    return None


def print_snapshot(codes, positions, ticks):
    now = dt.datetime.now().strftime('%H:%M:%S')
    print('==== %s ====' % now)
    print('%-10s %8s %8s %8s %8s %8s %8s %10s %16s %16s %8s %8s %8s' % (
        '代码', '昨收', '现价', '涨跌%', '开盘', '最高', '最低', '成交额万',
        '买一价/量', '卖一价/量', '持仓', '成本', '浮盈%'))
    for c in codes:
        t = ticks.get(c) or {}
        pre, last = t.get('lastClose') or 0, t.get('lastPrice') or 0
        p = positions.get(c)
        print('%-10s %8.2f %8.2f %+8.2f %8.2f %8.2f %8.2f %10.0f %16s %16s %8s %8s %8s' % (
            c, pre, last, _pct(last, pre), t.get('open') or 0, t.get('high') or 0, t.get('low') or 0,
            (t.get('amount') or 0) / 1e4,
            '%.2f/%d' % (_first(t.get('bidPrice')), _first(t.get('bidVol'))),
            '%.2f/%d' % (_first(t.get('askPrice')), _first(t.get('askVol'))),
            p.volume if p else '-', '%.3f' % p.open_price if p else '-',
            '%+.2f' % _pct(last, p.open_price) if p else '-'))


def watch(codes, interval, until, alert, csv_path):
    prev = {}
    csv = None
    if csv_path:
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        new = not os.path.exists(csv_path)
        csv = open(csv_path, 'a', encoding='utf-8-sig')
        if new:
            csv.write('time,code,last,pct,open,high,low,amount\n')
        print('记录到 %s' % csv_path)
    print('每 %d 秒一行，%s 自动退出，Ctrl+C 提前退……' % (interval, until.strftime('%H:%M')))
    try:
        while True:
            now = dt.datetime.now()
            if now.time() >= until:
                break
            if not (LUNCH[0] <= now.time() < LUNCH[1]):
                try:
                    ticks = xtdata.get_full_tick(codes)
                except Exception as e:
                    print('%s 取行情失败：%r' % (now.strftime('%H:%M:%S'), e))
                    ticks = {}
                parts = []
                for c in codes:
                    t = ticks.get(c)
                    if not t:
                        continue
                    pre, last = t.get('lastClose') or 0, t.get('lastPrice') or 0
                    hi, lo, amt = t.get('high') or 0, t.get('low') or 0, (t.get('amount') or 0) / 1e4
                    tags = []
                    old = prev.get(c)
                    if old:
                        if hi > old['high'] and last >= hi:
                            tags.append('新高')
                        if lo and lo < old['low'] and last <= lo:
                            tags.append('新低')
                        step = _pct(last, old['last'])
                        if step >= alert:
                            tags.append('急涨%+.1f%%' % step)
                        elif step <= -alert:
                            tags.append('急跌%+.1f%%' % step)
                    prev[c] = {'last': last, 'high': hi, 'low': lo}
                    parts.append('%s %.2f(%+.2f%%) 高%.2f 低%.2f 额%.0f万%s' % (
                        c[:6], last, _pct(last, pre), hi, lo, amt,
                        ''.join('[%s]' % x for x in tags)))
                    if csv:
                        csv.write('%s,%s,%s,%.2f,%s,%s,%s,%.0f\n' % (
                            now.strftime('%Y-%m-%d %H:%M:%S'), c, last, _pct(last, pre),
                            t.get('open') or 0, hi, lo, amt))
                if csv:
                    csv.flush()
                print('%s | %s' % (now.strftime('%H:%M'), ' | '.join(parts)), flush=True)
            time.sleep(interval - time.time() % interval)
    except KeyboardInterrupt:
        print('\n再见')
    finally:
        if csv:
            csv.close()


def main():
    parser = argparse.ArgumentParser(description='看持仓股票的竞价/盘中行情（只读）')
    parser.add_argument('codes', nargs='*', help='持仓之外额外要看的代码，如 000001.SZ')
    parser.add_argument('-a', '--account', help='资金账号，不传就用 .env 的 QMT_ACCOUNT_ID')
    parser.add_argument('-t', '--account-type', default=DEFAULT_ACCOUNT_TYPE,
                        help='账号类型：STOCK（默认）/ CREDIT / FUTURE')
    parser.add_argument('--no-positions', action='store_true', help='不查持仓，只看传入的代码')
    parser.add_argument('--watch', action='store_true', help='不退出，每个间隔打一行')
    parser.add_argument('-i', '--interval', type=int, default=60, help='--watch 的间隔秒数，默认 60')
    parser.add_argument('--until', default='15:00', help='--watch 到几点自动退，默认 15:00')
    parser.add_argument('--alert', type=float, default=1.0,
                        help='一个间隔内涨跌超过多少个百分点标 [急涨]/[急跌]，默认 1')
    parser.add_argument('--csv', action='store_true',
                        help='--watch 时记到 <共享数据目录>\\position_watch\\<日期>.csv')
    args = parser.parse_args()

    positions = {}
    if not args.no_positions:
        trader = XtQuantTrader('', 0)
        trader.start()
        if trader.connect() != 0:
            print(trader.last_error or
                  '连不上 QMT 桥。确认大QMT已启动，且「模型交易」里的「桥接服务」正在运行。')
            return 1
        account = resolve_account(trader, args.account, args.account_type)
        if account is None:
            print('没有可用账号：.env 里没有 QMT_ACCOUNT_ID，也没传 -a。')
            return 1
        positions = {p.stock_code: p for p in trader.query_stock_positions(account) or []
                     if p.volume > 0}

    codes = list(positions) + [c for c in args.codes if c not in positions]
    if not codes:
        print('没有持仓，也没传代码，没什么可看的。')
        return 1

    print_snapshot(codes, positions, xtdata.get_full_tick(codes))

    if args.watch:
        until = dt.datetime.strptime(args.until, '%H:%M').time()
        csv_path = None
        if args.csv:
            data_root = os.environ.get('QMTLOCAL_DATA_DIR') or os.path.join(ROOT, 'real_data')
            csv_path = os.path.join(data_root, 'position_watch',
                                    dt.date.today().strftime('%Y%m%d') + '.csv')
        print()
        watch(codes, max(args.interval, 3), until, args.alert, csv_path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
