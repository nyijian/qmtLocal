# coding:utf-8
"""桥接层自测：不依赖 QMT，用假 ContextInfo 把协议端到端跑一遍。

    .venv\\Scripts\\python.exe -m qmt_bridge.桥接自测

改动桥两侧任何一边之后都跑一次。真连 QMT 之前，这个能把协议、序列化、
订阅号路由、字段翻译、断线清理这些问题先挡掉。

交易那几段用的是假账号假成交，**不会碰任何真实账号**。
"""
import importlib.util
import os
import sys
import threading
import time

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

spec = importlib.util.spec_from_file_location(
    'bridge_server', os.path.join(ROOT, 'qmt_client_scripts', '桥接服务.py'))
bridge_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge_server)


class FakeContext(object):
    def __init__(self):
        self.next_sub = 100
        self.stop = threading.Event()
        self.unsubbed = []
        self.accountid = '55004374'
        self.accounttype = 'STOCK'

    def subscribe_quote(self, code, period, dividend_type, result_type, callback):
        self.next_sub += 1
        sub = self.next_sub

        def pump():
            n = 0
            while not self.stop.is_set() and n < 3:
                # QMT 给的形状：{字段: [值, ...]}，一次可能带多条
                callback({code: {'time': [1789959780000 + n, 1789959781000 + n],
                                 'lastPrice': [11.39 + n, 11.40 + n],
                                 'volume': [100 + n, 200 + n]}})
                n += 1
                time.sleep(0.05)
        threading.Thread(target=pump, daemon=True).start()
        return sub

    def subscribe_whole_quote(self, code_list, callback):
        self.next_sub += 1
        sub = self.next_sub

        def pump():
            n = 0
            while not self.stop.is_set() and n < 3:
                callback({'000001.SZ': {'time': 1789959780000 + n, 'lastPrice': 11.39,
                                        'askPrice': [11.4, 11.41], 'bidVol': [2429, 7127]}})
                n += 1
                time.sleep(0.05)
        threading.Thread(target=pump, daemon=True).start()
        return sub

    def unsubscribe_quote(self, sub):
        self.unsubbed.append(sub)
        return True

    def get_full_tick(self, code_list):
        return dict((c, {'time': 1789959780000, 'lastPrice': 11.39,
                         'askPrice': [11.4], 'bidPrice': [11.39]}) for c in code_list)

    def get_market_data_ex(self, fields, stocks, period, start, end, count,
                           dividend_type, fill_data, subscribe):
        return dict((s, pd.DataFrame({'close': [10.1, 10.2, 10.3]},
                                     index=['20260917', '20260918', '20260919']))
                    for s in stocks)

    def get_instrument_detail(self, code):
        return {'InstrumentID': code.split('.')[0], 'InstrumentName': '测试'}


# ---------------------------------------------------------------- 假交易接口
#
# 每种数据一个类：`_dump_obj` 的字段名缓存是按类型存的，共用一个类会让
# 第一次 dir() 的结果套到后面所有对象上 —— 真 QMT 里每种 datatype 就是一个
# 固定的 C++ 类型，这里也照着来，顺便把那个缓存一起测了。

class FakeAccountRow(object):
    def __init__(self, cash, market_value):
        self.m_strAccountID = '55004374'
        self.m_dAvailable = cash
        self.m_dBalance = cash + market_value
        self.m_dInstrumentValue = market_value
        self.m_dFrozenCash = 0.0
        self.m_dPositionProfit = 1234.5

    def some_method(self):            # 可调用的成员不该被 dump 进去
        return 'nope'


class FakePositionRow(object):
    def __init__(self, inst, market, volume, can_use):
        self.m_strInstrumentID = inst
        self.m_strExchangeID = market
        self.m_strInstrumentName = '平安银行'
        self.m_nVolume = volume
        self.m_nCanUseVolume = can_use
        self.m_nFrozenVolume = 0
        self.m_nYesterdayVolume = can_use
        self.m_nOnRoadVolume = 0
        self.m_dOpenPrice = 11.0
        self.m_dInstrumentValue = volume * 11.39
        self.m_dLastPrice = 11.39
        self.m_dFloatProfit = 390.0
        self.m_nDirection = 48


class FakeOrderRow(object):
    def __init__(self, sysid, inst, market, volume, price, remark, status=50, traded=0):
        self.m_strAccountID = '55004374'
        self.m_strOrderSysID = sysid
        self.m_strInstrumentID = inst
        self.m_strExchangeID = market
        self.m_nOffsetFlag = 48
        self.m_nDirection = 48
        self.m_dLimitPrice = price
        self.m_nOrderPriceType = 11
        self.m_nVolumeTotalOriginal = volume
        self.m_nVolumeTraded = traded
        self.m_nOrderStatus = status
        self.m_strInsertTime = '09:31:05'
        self.m_dTradeAmount = traded * price
        self.m_strOrderRemark = remark
        self.m_strStrategyName = 'selftest'


class FakeDealRow(object):
    def __init__(self, trade_id, sysid, inst, market, volume, price):
        self.m_strAccountID = '55004374'
        self.m_strTradeID = trade_id
        self.m_strOrderSysID = sysid
        self.m_strInstrumentID = inst
        self.m_strExchangeID = market
        self.m_nOffsetFlag = 48
        self.m_nDirection = 48
        self.m_nVolume = volume
        self.m_dPrice = price
        self.m_dTradeAmount = volume * price
        self.m_strTradeTime = '09:31:06'
        self.m_dComssion = 5.0
        self.m_strOrderRemark = ''


class FakeBroker(object):
    """一个够用的假柜台：认账号、记委托和成交、能撤单。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.cash = 500000.0
        self.positions = [FakePositionRow('000001', 'SZ', 1000, 1000)]
        self.orders = []
        self.deals = []
        self.next_sysid = 30001
        self.passorder_calls = []
        self.cancel_calls = []

    def query(self, accountid, accounttype, datatype, strategyname=''):
        assert accounttype == 'stock', accounttype       # 大写名字应该被翻译过
        with self.lock:
            if datatype == 'account':
                return [FakeAccountRow(self.cash, 11390.0)]
            if datatype == 'position':
                return list(self.positions)
            if datatype == 'order':
                return list(self.orders)
            if datatype == 'deal':
                return list(self.deals)
        return []

    def passorder(self, op_type, order_type, accountid, order_code, pr_type,
                  price, volume, strategy_name, quick_trade, user_order_id, C):
        with self.lock:
            self.passorder_calls.append(
                (op_type, order_type, accountid, order_code, pr_type, price,
                 volume, strategy_name, quick_trade, user_order_id))
            sysid = str(self.next_sysid)
            self.next_sysid += 1
            inst, market = order_code.split('.')
            self.orders.append(FakeOrderRow(sysid, inst, market, volume, price,
                                            user_order_id))
        return 0

    def cancel(self, order_sysid, accountid, accounttype, C):
        with self.lock:
            self.cancel_calls.append((order_sysid, accountid, accounttype))
            for order in self.orders:
                if order.m_strOrderSysID == order_sysid:
                    order.m_nOrderStatus = 54          # 已撤
        return 0

    def fill(self, sysid, volume, price):
        """模拟成交回报。"""
        with self.lock:
            for order in self.orders:
                if order.m_strOrderSysID == sysid:
                    order.m_nVolumeTraded = volume
                    order.m_nOrderStatus = 56          # 已成
                    order.m_dTradeAmount = volume * price
                    inst, market = order.m_strInstrumentID, order.m_strExchangeID
                    break
            else:
                return
            self.deals.append(FakeDealRow('T%s' % sysid, sysid, inst, market,
                                          volume, price))


broker = FakeBroker()
bridge_server.get_trade_detail_data = broker.query
bridge_server.passorder = broker.passorder
bridge_server.cancel = broker.cancel
bridge_server.TRADE_POLL_INTERVAL = 0.15          # 自测里别等满一秒


fake = FakeContext()
PORT = 58698                       # 跟默认端口错开，免得撞上真在跑的桥
server = bridge_server.BridgeServer(fake, '127.0.0.1', PORT)
server.start()
time.sleep(0.3)

from qmt_bridge import xtconstant, xtdata
from qmt_bridge.xttrader import XtQuantTrader, XtQuantTraderCallback
from qmt_bridge.xttype import StockAccount

failures = []


def check(name, cond, detail=''):
    print(('  OK   ' if cond else '  FAIL ') + name + ('' if cond else '  <- ' + str(detail)))
    if not cond:
        failures.append(name)


def until(pred, timeout=3.0):
    """等一个条件成立，最多等 timeout 秒。轮询出来的东西不能靠固定 sleep。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


xtdata.enable_hello = False
xtdata.connect('127.0.0.1', PORT)
print('1) 请求/响应')
check('ping', xtdata.get_client().call('ping') == 'pong')
tick = xtdata.get_full_tick(['000001.SZ', '600000.SH'])
check('get_full_tick 两个标的', set(tick) == {'000001.SZ', '600000.SH'}, tick)
check('get_full_tick 字段完整', tick['000001.SZ']['lastPrice'] == 11.39, tick)

df = xtdata.get_market_data_ex(['close'], ['000001.SZ'], period='1d', count=3)
got = df['000001.SZ']
check('get_market_data_ex 还原成 DataFrame', isinstance(got, pd.DataFrame), type(got))
check('DataFrame 形状', got.shape == (3, 1), got.shape)
check('DataFrame 索引', list(got.index) == ['20260917', '20260918', '20260919'], list(got.index))
check('DataFrame 取值', float(got['close'].iloc[-1]) == 10.3, got)

detail = xtdata.get_instrument_detail('000001.SZ')
check('get_instrument_detail 中文不乱码', detail['InstrumentName'] == '测试', detail)

print('2) 逐笔推送（subscribe_quote）')
ticks = []
seq1 = xtdata.subscribe_quote('000001.SZ', period='tick', callback=lambda d: ticks.append(d))
time.sleep(0.5)
check('拿到推送', len(ticks) >= 3, len(ticks))
if ticks:
    d = ticks[0]
    check('形状是 {code: [tick,...]}', isinstance(d, dict) and isinstance(d.get('000001.SZ'), list), d)
    rows = d['000001.SZ']
    check('转置成了两条 tick', len(rows) == 2, rows)
    check('每条 tick 是扁平 dict', rows[0]['lastPrice'] == 11.39 and rows[1]['lastPrice'] == 11.40, rows)

print('3) 全推（subscribe_whole_quote）')
whole = []
seq2 = xtdata.subscribe_whole_quote(['SH', 'SZ'], callback=lambda d: whole.append(d))
time.sleep(0.5)
check('拿到全推', len(whole) >= 3, len(whole))
if whole:
    w = whole[0]['000001.SZ']
    check('形状是 {code: tick}', isinstance(w, dict) and 'lastPrice' in w, w)
    check('五档数组保真', w['askPrice'] == [11.4, 11.41] and w['bidVol'] == [2429, 7127], w)

print('4) 退订与清理')
xtdata.unsubscribe_quote(seq1)
xtdata.unsubscribe_quote(seq2)
time.sleep(0.2)
check('两个订阅都传到了 ContextInfo', len(fake.unsubbed) == 2, fake.unsubbed)

print('5) 错误传播''（下面这段 traceback 是故意触发的，测的就是服务端要报错）')
try:
    xtdata.get_client().call('不存在的函数')
    check('未知函数应报错', False)
except Exception as e:
    check('未知函数报错且带原因', '未知的函数名' in str(e), e)

# ---------------------------------------------------------------- 交易
print('6) 交易查询与字段翻译')
trader = XtQuantTrader('', 0)
trader.start()
check('connect 返回 0', trader.connect() == 0)

acc = StockAccount('55004374')
default_acc = trader.get_default_account()
check('拿得到策略绑定的账号',
      default_acc is not None and default_acc.account_id == '55004374', default_acc)

asset = trader.query_stock_asset(acc)
check('资金：cash', asset is not None and asset.cash == 500000.0, asset)
check('资金：total_asset = 可用 + 市值', asset.total_asset == 511390.0, asset)
check('资金：market_value', asset.market_value == 11390.0, asset)
check('资金：原始字段留在 .raw 上', asset.raw.get('m_dPositionProfit') == 1234.5, asset.raw)
check('资金：方法不该被 dump 进来', 'some_method' not in asset.raw, sorted(asset.raw))

positions = trader.query_stock_positions(acc)
check('持仓一条', len(positions) == 1, positions)
pos = positions[0]
check('持仓：代码拼成 000001.SZ', pos.stock_code == '000001.SZ', pos.stock_code)
check('持仓：volume / can_use_volume', (pos.volume, pos.can_use_volume) == (1000, 1000), pos)
check('持仓：中文名不乱码', pos.instrument_name == '平安银行', pos.instrument_name)

print('7) 下单开关挡住了没有')
check('ALLOW_ORDER 默认是 False', bridge_server.ALLOW_ORDER is False)
try:
    trader.order_stock(acc, '000001.SZ', xtconstant.STOCK_BUY, 100,
                       xtconstant.FIX_PRICE, 11.39)
    check('ALLOW_ORDER=False 时下单应被挡下', False)
except Exception as e:
    check('ALLOW_ORDER=False 时下单被挡下且说明原因', 'ALLOW_ORDER' in str(e), e)
check('确实没发出去', len(broker.passorder_calls) == 0, broker.passorder_calls)

print('8) 下单、认委托号、撤单')
bridge_server.ALLOW_ORDER = True
order_id = trader.order_stock(acc, '000001.SZ', xtconstant.STOCK_BUY, 100,
                              xtconstant.FIX_PRICE, 11.39, strategy_name='selftest')
check('passorder 被调用一次', len(broker.passorder_calls) == 1, broker.passorder_calls)
call = broker.passorder_calls[0]
check('opType 是买入 23', call[0] == xtconstant.STOCK_BUY, call)
check('orderType 是按股数 1101', call[1] == xtconstant.FIX_VOLUME, call)
check('prType 原样透传 11', call[4] == xtconstant.FIX_PRICE, call)
check('价格和数量对', (call[5], call[6]) == (11.39, 100), call)
check('volume 是整数不是浮点', isinstance(call[6], int), type(call[6]))
check('strategyName 传过去了', call[7] == 'selftest', call)
check('带了唯一的 userOrderId', bool(call[9]), call)
check('按备注把交易所委托号认回来了', order_id == 30001, order_id)

orders = trader.query_stock_orders(acc)
check('委托一条', len(orders) == 1, orders)
order = orders[0]
check('委托：order_type 从 offsetFlag 译成买入', order.order_type == xtconstant.STOCK_BUY, order)
check('委托：order_volume', order.order_volume == 100, order)
check('委托：状态文案', order.status_msg == '已报', order.status_msg)
check('委托：order_time 译成了时间戳', order.order_time > 0, order.order_time)
check('委托：还能撤', order.cancelable is True, order.order_status)

check('撤单返回 0', trader.cancel_order_stock(acc, order_id) == 0)
check('撤单打到了交易所委托号', broker.cancel_calls == [('30001', '55004374', 'stock')],
      broker.cancel_calls)
check('撤完就不可撤了',
      trader.query_stock_orders(acc, cancelable_only=True) == [],
      trader.query_stock_orders(acc))

print('9) 交易推送（轮询转推送）')
events = {'order': [], 'deal': [], 'asset': [], 'position': []}


class Cb(XtQuantTraderCallback):
    def on_stock_order(self, order):
        events['order'].append(order)

    def on_stock_trade(self, trade):
        events['deal'].append(trade)

    def on_stock_asset(self, asset):
        events['asset'].append(asset)

    def on_stock_position(self, position):
        events['position'].append(position)


trader.register_callback(Cb())
check('subscribe 返回 0', trader.subscribe(acc) == 0)
check('首轮推来全量快照',
      until(lambda: events['asset'] and events['position'] and events['order']),
      dict((k, len(v)) for k, v in events.items()))
if events['asset']:
    check('推来的资金也翻译好了', events['asset'][0].cash == 500000.0, events['asset'][0])
if events['position']:
    check('推来的持仓也翻译好了', events['position'][0].stock_code == '000001.SZ',
          events['position'][0])

before_deals = len(events['deal'])
broker.fill('30001', 100, 11.39)
check('成交推过来了', until(lambda: len(events['deal']) > before_deals),
      len(events['deal']))
if len(events['deal']) > before_deals:
    trade = events['deal'][-1]
    check('成交：代码', trade.stock_code == '000001.SZ', trade)
    check('成交：量价', (trade.traded_volume, trade.traded_price) == (100, 11.39), trade)
    check('成交：金额', trade.traded_amount == 1139.0, trade)
    check('成交：traded_id', trade.traded_id == 'T30001', trade)

check('同一笔成交不会重复推',
      not until(lambda: len(events['deal']) > before_deals + 1, timeout=0.6),
      len(events['deal']))

print('10) 退订交易')
check('unsubscribe 返回 0', trader.unsubscribe(acc) == 0)
time.sleep(0.05)
check('服务端订阅表清空了',
      all(not c.trade_subs for c in server.conns), [c.trade_subs for c in server.conns])
count_after_unsub = len(events['deal'])
broker.fill('30001', 100, 11.39)
check('退订之后不再推',
      not until(lambda: len(events['deal']) > count_after_unsub, timeout=0.6),
      len(events['deal']))

print('11) 断线时连接被回收')
conns_before = len(server.conns)
xtdata.disconnect()
time.sleep(0.3)
check('服务端连接已回收', len(server.conns) == conns_before - 1,
      '%d -> %d' % (conns_before, len(server.conns)))

fake.stop.set()
server.stop()
print()
print('FAILURES: %d' % len(failures))
sys.exit(1 if failures else 0)
