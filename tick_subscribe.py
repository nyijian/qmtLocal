# coding:utf-8
"""订阅实时推送的 tick（分笔）行情。

前提：
  1. 大QMT（国金证券QMT交易端）已启动并登录；
  2. 「模型交易」里「桥接服务」那个策略的状态是「运行中」
     （不是「策略编辑器」里点运行，见 qmt_bridge/README.md 的「怎么跑」）；
  3. 本脚本跑在项目的 .venv（py3.6）里：
     .venv\\Scripts\\python.exe tick_subscribe.py 000001.SZ 600000.SH

两种模式：
  --mode quote  （默认，推荐）按指定股票逐个 subscribe_quote(period='tick')，
                只推感兴趣的标的，回调里能拿到完整逐笔盘口。
  --mode whole  全推 subscribe_whole_quote，可按市场整体订阅（全市场），
                但每次只给最新快照，不回补历史。

    .venv\\Scripts\\python.exe tick_subscribe.py --positions        # 订阅当前持仓的所有标的
    .venv\\Scripts\\python.exe tick_subscribe.py --positions -a 你的资金账号

注意：tick 推送只在交易时段产生；非交易时间订阅成功但不会有回调。
"""
import argparse
import datetime as dt
import os

# 走桥。miniQMT 恢复之后，把下面这几行换成 xtquant 对应模块即可，
# 本文件其余部分一个字都不用动 —— 两者签名和返回值形状是对齐的。
from qmt_bridge import xtdata
from qmt_bridge.xttrader import XtQuantTrader
from qmt_bridge.xttype import StockAccount

# --positions 不传 -a 时用哪个账号，跟 trade_monitor.py 保持一致：真实账号不写进代码，
# 从环境变量拿。设置方式见 trade_monitor.py 里 DEFAULT_ACCOUNT 那段注释。
DEFAULT_ACCOUNT = os.environ.get('QMT_ACCOUNT_ID', '')
DEFAULT_ACCOUNT_TYPE = os.environ.get('QMT_ACCOUNT_TYPE', 'STOCK')


def _fmt_time(ms):
    """xtdata 的 tick 时间是毫秒时间戳。"""
    return dt.datetime.fromtimestamp(ms / 1000.0).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]


def _print_tick(code, tick):
    ask_p = tick.get('askPrice') or []
    bid_p = tick.get('bidPrice') or []
    ask_v = tick.get('askVol') or []
    bid_v = tick.get('bidVol') or []
    line = '{} {} last={} open={} high={} low={} vol={} amt={}'.format(
        _fmt_time(tick.get('time', 0)),
        code,
        tick.get('lastPrice'),
        tick.get('open'),
        tick.get('high'),
        tick.get('low'),
        tick.get('volume'),
        tick.get('amount'),
    )
    if bid_p and ask_p:
        line += ' | 买一 {}x{}  卖一 {}x{}'.format(
            bid_p[0], bid_v[0] if bid_v else '-',
            ask_p[0], ask_v[0] if ask_v else '-',
        )
    print(line)


def run_quote(codes):
    """逐标的订阅，回调参数形如 {code: [tick1, tick2, ...]}。"""
    def on_data(datas):
        for code, ticks in datas.items():
            for tick in ticks:
                _print_tick(code, tick)

    seqs = []
    for code in codes:
        seq = xtdata.subscribe_quote(code, period='tick', count=0, callback=on_data)
        print('已订阅 {} tick，订阅号={}'.format(code, seq))
        seqs.append(seq)
    return seqs


def run_whole(codes):
    """全推模式，回调参数形如 {code: tick}；codes 可为 ['SH','SZ'] 这类市场代码。"""
    def on_data(datas):
        for code, tick in datas.items():
            _print_tick(code, tick)

    seq = xtdata.subscribe_whole_quote(codes, callback=on_data)
    print('已全推订阅 {}，订阅号={}'.format(codes, seq))
    return [seq]


def positions_codes(account_id, account_type):
    """查一遍持仓，返回有仓位的代码列表。走的是同一条桥连接，跟行情共用一个 TCP。"""
    trader = XtQuantTrader('', 0)
    trader.start()
    if trader.connect() != 0:
        raise SystemExit(trader.last_error or '连不上 QMT 桥。')
    account = StockAccount(account_id, account_type)
    codes = [p.stock_code for p in trader.query_stock_positions(account) if p.volume > 0]
    return codes


def main():
    parser = argparse.ArgumentParser(description='订阅实时 tick 行情示例')
    parser.add_argument('codes', nargs='*',
                        help='股票代码如 000001.SZ；全推模式下也可传 SH / SZ')
    parser.add_argument('--mode', choices=['quote', 'whole'], default='quote')
    parser.add_argument('--snapshot', action='store_true',
                        help='订阅前先打印一次当前盘口快照（get_full_tick）')
    parser.add_argument('--positions', action='store_true',
                        help='不手动填代码，改成订阅当前持仓的所有标的')
    parser.add_argument('-a', '--account', default=DEFAULT_ACCOUNT,
                        help='--positions 用哪个资金账号，不传就用 QMT_ACCOUNT_ID 环境变量')
    parser.add_argument('-t', '--account-type', default=DEFAULT_ACCOUNT_TYPE)
    args = parser.parse_args()

    if args.positions:
        if not args.account:
            raise SystemExit('--positions 需要账号：传 -a，或者设 QMT_ACCOUNT_ID 环境变量'
                             '（见 trade_monitor.py 里 DEFAULT_ACCOUNT 那段注释）。')
        codes = positions_codes(args.account, args.account_type)
        if not codes:
            print('账号 %s 当前没有持仓，没有可订阅的标的。' % args.account)
            return
        print('持仓 %d 只，订阅：%s' % (len(codes), ', '.join(codes)))
    else:
        codes = args.codes or ['000001.SZ']

    if args.snapshot:
        snap = xtdata.get_full_tick(codes)
        for code, tick in snap.items():
            _print_tick(code, tick)

    seqs = run_whole(codes) if args.mode == 'whole' else run_quote(codes)

    try:
        # 阻塞当前线程，持续接收推送；行情断开时会抛异常
        xtdata.run()
    except KeyboardInterrupt:
        pass
    finally:
        for seq in seqs:
            try:
                xtdata.unsubscribe_quote(seq)
            except Exception:
                pass
        print('已退订')


if __name__ == '__main__':
    main()
