# coding:utf-8
"""查看账号的资金、持仓、委托、成交，也可以盯着实时变化。

前提：
  1. 大QMT（国金证券QMT交易端）已启动并登录；
  2. 「模型交易」里「桥接服务」那个策略的状态是「运行中」
     （不是「策略编辑器」里点运行 —— 那样起的实例转眼就被拆掉，见 qmt_bridge/README.md）；
  3. 本脚本跑在项目的 .venv（py3.6）里。

    .venv\\Scripts\\python.exe trade_monitor.py                 # 打一次快照就退
    .venv\\Scripts\\python.exe trade_monitor.py --watch         # 盯着不走，有变化就打
    .venv\\Scripts\\python.exe trade_monitor.py -a 55004374     # 指定账号
    .venv\\Scripts\\python.exe trade_monitor.py -t CREDIT       # 融资融券账号

不传 -a 就问桥要策略在 QMT 界面上绑的那个账号。

这个脚本**只读**，不下单。下单的例子见 qmt_bridge/xttrader.py 的模块注释，
另外桥接服务里的 ALLOW_ORDER 默认是 False，下单本来也发不出去。
"""
import argparse
import datetime as dt
import os
import sys

# 走桥。miniQMT 恢复之后，把下面三行换成 xtquant 的对应模块即可，
# 本文件其余部分一个字都不用动 —— 签名和字段名是对齐的。
from qmt_bridge import xtconstant
from qmt_bridge.dotenv_lite import load_dotenv
from qmt_bridge.xttrader import XtQuantTrader, XtQuantTraderCallback
from qmt_bridge.xttype import StockAccount

# 不传 -a 时用哪个账号。
# 本来想问桥要策略绑定的那个（trader.get_default_account()），但这个版本的
# ContextInfo 上没有 accountid，拿不到，所以退而求其次找环境变量。
#
# 真实资金账号不写进代码——这仓库要传GitHub，account_id 硬编码进源码提交
# 历史里就删不干净了。放项目根目录的 .env 里（复制 .env.example 改一份），
# 换机器把 .env 文件复制过去就行，不用在每台机器上重新设置系统环境变量；
# 系统里真设了同名环境变量的话那个优先，.env 只是补上没设置的。
load_dotenv()
DEFAULT_ACCOUNT = os.environ.get('QMT_ACCOUNT_ID', '')
DEFAULT_ACCOUNT_TYPE = os.environ.get('QMT_ACCOUNT_TYPE', 'STOCK')


def _fmt_time(sec):
    if not sec:
        return '--:--:--'
    return dt.datetime.fromtimestamp(sec).strftime('%H:%M:%S')


def _side(order_type):
    return {xtconstant.STOCK_BUY: '买', xtconstant.STOCK_SELL: '卖'}.get(order_type, '?')


def print_snapshot(trader, account):
    asset = trader.query_stock_asset(account)
    if asset is None:
        print('账号 %s 查不到资金 —— 账号是不是填错了？' % account.account_id)
        return
    print('账号 %s（%s）' % (asset.account_id, account.account_type))
    print('  总资产 %.2f   可用 %.2f   持仓市值 %.2f   冻结 %.2f'
          % (asset.total_asset, asset.cash, asset.market_value, asset.frozen_cash))

    positions = trader.query_stock_positions(account)
    print('持仓 %d 只' % len(positions))
    for pos in positions:
        print('  %-11s %-8s 持有%6d 可用%6d 成本%8.3f 现价%8.3f 市值%12.2f 浮盈%10.2f'
              % (pos.stock_code, pos.instrument_name[:4], pos.volume, pos.can_use_volume,
                 pos.open_price, pos.last_price, pos.market_value, pos.float_profit))

    orders = trader.query_stock_orders(account)
    print('委托 %d 笔（其中可撤 %d 笔）'
          % (len(orders), len([o for o in orders if o.cancelable])))
    for order in orders:
        print('  %s %-11s %s %6d 股 @%8.3f  已成%6d  %s  委托号%s'
              % (_fmt_time(order.order_time), order.stock_code, _side(order.order_type),
                 order.order_volume, order.price, order.traded_volume,
                 order.status_msg, order.order_id))

    trades = trader.query_stock_trades(account)
    print('成交 %d 笔' % len(trades))
    for trade in trades:
        print('  %s %-11s %s %6d 股 @%8.3f  金额%12.2f'
              % (_fmt_time(trade.traded_time), trade.stock_code, _side(trade.order_type),
                 trade.traded_volume, trade.traded_price, trade.traded_amount))


class Watcher(XtQuantTraderCallback):
    """盯盘回调。

    注意这些是轮询出来的，最多比 QMT 晚一秒，见 xttrader.py 的"已知差异"。
    回调跑在桥的接收线程上，别在这儿做耗时的事。
    """

    def on_stock_order(self, order):
        print('[委托] %s %-11s %s %d 股 @%.3f  已成%d  %s'
              % (_fmt_time(order.order_time), order.stock_code, _side(order.order_type),
                 order.order_volume, order.price, order.traded_volume, order.status_msg))

    def on_stock_trade(self, trade):
        print('[成交] %s %-11s %s %d 股 @%.3f  金额%.2f'
              % (_fmt_time(trade.traded_time), trade.stock_code, _side(trade.order_type),
                 trade.traded_volume, trade.traded_price, trade.traded_amount))

    def on_stock_asset(self, asset):
        print('[资金] 总资产%.2f 可用%.2f 市值%.2f'
              % (asset.total_asset, asset.cash, asset.market_value))

    def on_stock_position(self, position):
        print('[持仓] %-11s 持有%d 可用%d 市值%.2f'
              % (position.stock_code, position.volume, position.can_use_volume,
                 position.market_value))

    def on_disconnected(self):
        print('[断开] 跟桥的连接断了 —— QMT 里那个模型还在运行吗？')


def main():
    parser = argparse.ArgumentParser(description='查看 QMT 账号的交易数据')
    parser.add_argument('-a', '--account',
                        help='资金账号，不传就先问桥要策略绑定的那个，'
                             '拿不到再退到 .env / QMT_ACCOUNT_ID 环境变量')
    parser.add_argument('-t', '--account-type', default=DEFAULT_ACCOUNT_TYPE,
                        help='账号类型：STOCK（默认）/ CREDIT / FUTURE')
    parser.add_argument('--watch', action='store_true',
                        help='不退出，盯着委托/成交/资金/持仓的变化')
    args = parser.parse_args()

    trader = XtQuantTrader('', 0)
    trader.start()
    if trader.connect() != 0:
        # 桥分得清「没人监听」和「僵尸端口」，原样打出来，别自己编一句含糊的
        print(trader.last_error or
              '连不上 QMT 桥。确认大QMT已启动，且「模型交易」里的「桥接服务」正在运行。')
        return 1

    if args.account:
        account = StockAccount(args.account, args.account_type)
    else:
        # 优先级：-a 显式指定 > 桥问到的策略绑定账号 > QMT_ACCOUNT_ID 环境变量兜底。
        # 这个版本的 ContextInfo 拿不到 accountid，第二步总是 None，直接落到环境变量；
        # 别的 QMT 版本如果有 accountid，会在这一步就拿到，不用碰环境变量。
        account = trader.get_default_account()
        if account is not None:
            print('用策略绑定的账号：%s（%s）' % (account.account_id, account.account_type))
        elif DEFAULT_ACCOUNT:
            account = StockAccount(DEFAULT_ACCOUNT, DEFAULT_ACCOUNT_TYPE)
            print('桥拿不到策略绑定的账号，用 .env / 环境变量里的 QMT_ACCOUNT_ID：%s（%s）'
                  % (account.account_id, account.account_type))
        else:
            print('没有可用账号：桥拿不到策略绑定的账号，也没传 -a。'
                  '复制 .env.example 为 .env 填上账号，或者传 -a。')
            return 1

    print_snapshot(trader, account)

    if args.watch:
        print()
        print('盯着变化，Ctrl+C 退出……')
        trader.register_callback(Watcher())
        trader.subscribe(account)
        try:
            trader.run_forever()
        except KeyboardInterrupt:
            print('\n再见')
        finally:
            trader.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
