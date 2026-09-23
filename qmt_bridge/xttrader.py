# coding:utf-8
"""xttrader 的替身：签名照抄官方 xtquant.xttrader，底层走本机到大QMT的桥。

国金把 miniQMT 停了之后，`xtquant.xttrader.XtQuantTrader` 连不上交易服务。
交易数据跟行情一样，只在大QMT策略运行时里拿得到（`get_trade_detail_data` /
`passorder` / `cancel`），所以从 `qmt_client_scripts/桥接服务.py` 转出来，
这里把它重新包成 xttrader 的样子。

用法就是换一行 import：

    from qmt_bridge.xttrader import XtQuantTrader, XtQuantTraderCallback
    from qmt_bridge.xttype import StockAccount
    from qmt_bridge import xtconstant
    # from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
    # from xtquant.xttype import StockAccount
    # from xtquant import xtconstant

    trader = XtQuantTrader('', 0)
    trader.start()
    trader.connect()

    acc = StockAccount('55004374')
    print(trader.query_stock_asset(acc).cash)
    for p in trader.query_stock_positions(acc):
        print(p.stock_code, p.volume, p.can_use_volume)

要委托/成交的实时回调，再加：

    class Cb(XtQuantTraderCallback):
        def on_stock_order(self, o):  print('委托', o.stock_code, o.order_status)
        def on_stock_trade(self, t):  print('成交', t.stock_code, t.traded_volume)

    trader.register_callback(Cb())
    trader.subscribe(acc)
    trader.run_forever()

字段名对齐官方（`asset.cash`、`position.can_use_volume`、`order.order_status`、
`trade.traded_volume` …）。QMT 原始的 `m_xxx` 字段一个不落地挂在每个对象的
`.raw` 上 —— 各版本字段名不尽相同，对不上的时候先打 `.raw` 看看实际给了什么。

已知差异（都写在这儿，不藏着）
------------------------------
* **交易数据没有推送接口，是轮询出来的。** 桥每秒拉一次（有委托/成交回调时
  会立刻拉），所以 `on_stock_order` / `on_stock_trade` 比官方晚，最多一秒。
* **`order_stock` 返回的委托号要"认"。** `passorder` 本身不返回委托号，
  所以下单时带一个唯一备注，再回查委托列表按备注把交易所委托号认回来
  （默认最多等 `order_resolve_timeout` 秒）。认不回来就返回本地流水号，
  这个号 `cancel_order_stock` 也认。只有 11 参数版的 passorder 支持备注，
  见桥接服务里的 `PASSORDER_ARGS`。
* **`order_stock_async` 不是真异步**，跟同步走一条路，只是不等委托号。
* **默认下单是被挡住的。** 桥接服务里 `ALLOW_ORDER = False`，查询不受影响，
  下单/撤单会直接报错。确认无误后去那边改成 True 并重新「运行」。
* 没有 `on_account_status` / `on_smt_appointment_async_response` 这类接口。
"""

import threading
import time

from . import xtconstant
from . import xtdata
from .xtdata import BridgeError                    # noqa: F401  （方便上层 except）
from .xttype import StockAccount


# ---------------------------------------------------------------- 字段翻译
#
# QMT 各版本的 m_ 字段名不完全一样，所以每个属性给一串候选名，取第一个有值的。
# 翻译放在本地而不是 QMT 侧：那边改一次就要重新粘贴一次脚本，这边改完存盘就行。

def _pick(raw, names, default=None):
    for name in names:                    # 先找有值的
        v = raw.get(name)
        if v is not None and v != '':
            return v
    for name in names:                    # 都为空，那就认第一个存在的
        if name in raw:
            return raw[name]
    return default


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _code(raw):
    """m_strInstrumentID + m_strExchangeID -> '000001.SZ'。"""
    inst = _pick(raw, ['m_strInstrumentID', 'm_strInstrumentId'], '') or ''
    market = _pick(raw, ['m_strExchangeID', 'm_strExchangeId'], '') or ''
    return '%s.%s' % (inst, market) if inst and market else str(inst or '')


def _epoch(raw, names):
    """QMT 的时间字段形状太多（'09:31:05' / '20260923 09:31:05' / 时间戳），
    统一成官方那样的秒级时间戳；认不出来给 0，不猜。"""
    v = _pick(raw, names, None)
    if v in (None, '', 0):
        return 0
    if isinstance(v, (int, float)) and v >= 1e9:
        return int(v / 1000 if v >= 1e11 else v)
    digits = ''.join(c for c in str(v) if c.isdigit())
    if len(digits) in (5, 6):
        digits = time.strftime('%Y%m%d') + digits.zfill(6)
    elif len(digits) != 14:
        return 0
    try:
        return int(time.mktime(time.strptime(digits, '%Y%m%d%H%M%S')))
    except (ValueError, OverflowError):
        return 0


def _order_type(raw):
    """买/卖。股票里 m_nOffsetFlag 48=买 49=卖，跟 m_nDirection 同一套取值。"""
    for name in ('m_nOffsetFlag', 'm_nDirection'):
        v = raw.get(name)
        if v in (None, ''):
            continue
        v = _i(v, -1)
        if v == xtconstant.OFFSET_FLAG_OPEN:
            return xtconstant.STOCK_BUY
        if v == xtconstant.OFFSET_FLAG_CLOSE:
            return xtconstant.STOCK_SELL
    return 0


def _sysid(raw):
    return str(_pick(raw, ['m_strOrderSysID', 'm_strOrderSysId'], '') or '')


def _remark(raw):
    return str(_pick(raw, ['m_strOrderRemark', 'm_strRemark', 'm_strUserOrderId',
                           'm_strInvestorRemark'], '') or '')


class _Row(object):
    """所有交易对象的共同部分：原始字段留在 .raw 上，repr 只挑几个看。"""

    _SHOW = ()

    def __init__(self, raw):
        self.raw = raw or {}

    def __repr__(self):
        body = ' '.join('%s=%s' % (k, getattr(self, k, None)) for k in self._SHOW)
        return '<%s %s>' % (type(self).__name__, body)


class XtAsset(_Row):
    _SHOW = ('account_id', 'cash', 'market_value', 'total_asset')

    def __init__(self, raw, account_id='', account_type='STOCK'):
        _Row.__init__(self, raw)
        self.account_type = account_type
        self.account_id = str(_pick(raw, ['m_strAccountID'], '') or account_id)
        self.cash = _f(_pick(raw, ['m_dAvailable', 'm_dAvailableCash']))
        self.frozen_cash = _f(_pick(raw, ['m_dFrozenCash', 'm_dFrozenMargin']))
        self.market_value = _f(_pick(raw, ['m_dInstrumentValue', 'm_dStockValue',
                                           'm_dMarketValue']))
        self.total_asset = _f(_pick(raw, ['m_dBalance', 'm_dTotalAsset',
                                          'm_dAssureAsset']))
        # 官方没有，但是大QMT给了，白扔可惜
        self.position_profit = _f(_pick(raw, ['m_dPositionProfit']))


class XtPosition(_Row):
    _SHOW = ('stock_code', 'volume', 'can_use_volume', 'open_price', 'market_value')

    def __init__(self, raw, account_id='', account_type='STOCK'):
        _Row.__init__(self, raw)
        self.account_type = account_type
        self.account_id = str(_pick(raw, ['m_strAccountID'], '') or account_id)
        self.stock_code = _code(raw)
        self.volume = _i(_pick(raw, ['m_nVolume']))
        self.can_use_volume = _i(_pick(raw, ['m_nCanUseVolume']))
        self.frozen_volume = _i(_pick(raw, ['m_nFrozenVolume']))
        self.on_road_volume = _i(_pick(raw, ['m_nOnRoadVolume']))
        self.yesterday_volume = _i(_pick(raw, ['m_nYesterdayVolume']))
        self.open_price = _f(_pick(raw, ['m_dOpenPrice']))
        self.avg_price = _f(_pick(raw, ['m_dOpenPrice', 'm_dSettlementPrice']))
        self.market_value = _f(_pick(raw, ['m_dInstrumentValue', 'm_dMarketValue']))
        self.direction = _i(_pick(raw, ['m_nDirection']))
        # 官方没有的几个，大QMT有就带上
        self.last_price = _f(_pick(raw, ['m_dLastPrice']))
        self.float_profit = _f(_pick(raw, ['m_dFloatProfit', 'm_dPositionProfit']))
        self.instrument_name = str(_pick(raw, ['m_strInstrumentName'], '') or '')


class XtOrder(_Row):
    _SHOW = ('order_id', 'stock_code', 'order_type', 'order_volume',
             'traded_volume', 'price', 'order_status')

    def __init__(self, raw, account_id='', account_type='STOCK'):
        _Row.__init__(self, raw)
        self.account_type = account_type
        self.account_id = str(_pick(raw, ['m_strAccountID'], '') or account_id)
        self.order_sysid = _sysid(raw)
        # 官方的 order_id 是整数。交易所委托号通常是纯数字；不是的话退回本地号。
        self.order_id = (int(self.order_sysid) if self.order_sysid.isdigit()
                         else _i(_pick(raw, ['m_nOrderRef', 'm_nOrderID'])))
        self.stock_code = _code(raw)
        self.order_type = _order_type(raw)
        self.order_volume = _i(_pick(raw, ['m_nVolumeTotalOriginal', 'm_nVolumeTotal']))
        self.traded_volume = _i(_pick(raw, ['m_nVolumeTraded']))
        self.price = _f(_pick(raw, ['m_dLimitPrice', 'm_dPrice']))
        self.price_type = _i(_pick(raw, ['m_nOrderPriceType', 'm_nPriceType']))
        self.order_status = _i(_pick(raw, ['m_nOrderStatus']))
        self.status_msg = str(_pick(raw, ['m_strStatusMsg', 'm_strErrorMsg'], '')
                              or xtconstant.ORDER_STATUS_TEXT.get(self.order_status, ''))
        self.order_time = _epoch(raw, ['m_strInsertTime', 'm_nInsertTime',
                                       'm_strOrderTime'])
        self.strategy_name = str(_pick(raw, ['m_strStrategyName'], '') or '')
        self.order_remark = _remark(raw)
        self.direction = _i(_pick(raw, ['m_nDirection']))
        self.offset_flag = _i(_pick(raw, ['m_nOffsetFlag']))
        amount = _f(_pick(raw, ['m_dTradeAmount']))
        self.traded_price = (amount / self.traded_volume) if self.traded_volume else 0.0

    @property
    def cancelable(self):
        return self.order_status in xtconstant.CANCELABLE_STATUS


class XtTrade(_Row):
    _SHOW = ('stock_code', 'order_type', 'traded_volume', 'traded_price', 'traded_id')

    def __init__(self, raw, account_id='', account_type='STOCK'):
        _Row.__init__(self, raw)
        self.account_type = account_type
        self.account_id = str(_pick(raw, ['m_strAccountID'], '') or account_id)
        self.order_sysid = _sysid(raw)
        self.order_id = (int(self.order_sysid) if self.order_sysid.isdigit()
                         else _i(_pick(raw, ['m_nOrderRef'])))
        self.traded_id = str(_pick(raw, ['m_strTradeID', 'm_strTradeId'], '') or '')
        self.stock_code = _code(raw)
        self.order_type = _order_type(raw)
        self.traded_volume = _i(_pick(raw, ['m_nVolume']))
        self.traded_price = _f(_pick(raw, ['m_dPrice']))
        self.traded_amount = _f(_pick(raw, ['m_dTradeAmount']))
        self.traded_time = _epoch(raw, ['m_strTradeTime', 'm_nTradeTime'])
        self.strategy_name = str(_pick(raw, ['m_strStrategyName'], '') or '')
        self.order_remark = _remark(raw)
        self.direction = _i(_pick(raw, ['m_nDirection']))
        self.offset_flag = _i(_pick(raw, ['m_nOffsetFlag']))
        self.commission = _f(_pick(raw, ['m_dComssion', 'm_dCommission']))


class XtOrderError(object):
    def __init__(self, order_id=0, error_id=-1, error_msg=''):
        self.order_id = order_id
        self.error_id = error_id
        self.error_msg = error_msg

    def __repr__(self):
        return '<XtOrderError order_id=%s msg=%s>' % (self.order_id, self.error_msg)


class XtCancelError(XtOrderError):
    pass


# 'account' 是 get_trade_detail_data 的 datatype，'asset' 是推送里的 kind，同一个东西。
_TYPES = {'order': XtOrder, 'deal': XtTrade, 'position': XtPosition,
          'account': XtAsset, 'asset': XtAsset}


# ---------------------------------------------------------------- 回调基类

class XtQuantTraderCallback(object):
    """跟官方同名同方法。继承它，重写关心的那几个。

    回调跑在桥的接收线程上，**别在里面做耗时的事** —— 卡住它，后面所有推送
    都堵在后面。
    """

    def on_disconnected(self):
        pass

    def on_stock_order(self, order):
        pass

    def on_stock_trade(self, trade):
        pass

    def on_stock_asset(self, asset):
        pass

    def on_stock_position(self, position):
        pass

    def on_order_error(self, order_error):
        pass

    def on_cancel_error(self, cancel_error):
        pass

    def on_order_stock_async_response(self, response):
        pass


# ---------------------------------------------------------------- 主体

class XtQuantTrader(object):

    def __init__(self, path='', session_id=0, callback=None):
        # path / session_id 官方要，这里收下不用 —— 桥不认这两样东西。
        self.path = path
        self.session_id = session_id
        self.callback = callback
        self.last_error = None             # connect() 返回 -1 时的原因，见 connect 的注释
        self.order_resolve_timeout = 3.0   # 下单后回查委托号最多等多久，0 = 不等
        self._subs = {}                    # StockAccount -> 桥的订阅号
        self._lock = threading.Lock()
        self._local_seq = 0
        self._by_remark = {}               # 本地备注 -> 交易所委托号
        self._local_to_sysid = {}          # 本地流水号 -> 交易所委托号

    # -- 连接 -------------------------------------------------------

    def register_callback(self, callback):
        self.callback = callback

    def start(self):
        """官方要求先 start 再 connect。这里没有后台进程要拉起来，留作占位。"""
        return None

    def connect(self, ip='', port=None):
        """连上返回 0，连不上返回 -1（跟官方一样，不抛异常）。

        官方这个签名只有一个返回码，但桥能说清楚到底是没人监听还是僵尸端口，
        丢了可惜 —— 原因留在 `last_error` 上，调用方想打就打。
        """
        self.last_error = None
        try:
            xtdata.connect(ip, port)
            return 0
        except Exception as e:
            self.last_error = e
            return -1

    def stop(self):
        for account in list(self._subs):
            try:
                self.unsubscribe(account)
            except Exception:
                pass
        xtdata.disconnect()

    def sleep(self, seconds):
        time.sleep(seconds)

    def run_forever(self):
        """阻塞着等推送，断了就回调 on_disconnected 然后返回。与官方同义。"""
        bridge = xtdata.get_client()
        while bridge.alive:
            time.sleep(0.5)
        self._fire('on_disconnected')

    # -- 账号 -------------------------------------------------------

    def get_default_account(self):
        """策略在 QMT 界面上绑的那个账号。桥自己的扩展，官方没有。

        拿不到就返回 None —— 有些版本的 ContextInfo 上没有 accountid。
        """
        info = xtdata.get_client().call('get_trade_account')
        if not info or not info.get('account_id'):
            return None
        return StockAccount(info['account_id'], info.get('account_type') or 'STOCK')

    def subscribe(self, account):
        """订阅这个账号的委托/成交/资金/持仓变化。成功返回 0。

        注意是轮询来的，不是推送，最多晚一秒，见模块开头的"已知差异"。
        """
        bridge = xtdata.get_client()
        seq = bridge.new_seq()
        bridge.callbacks[seq] = self._make_dispatcher(account)
        try:
            bridge.call('subscribe_trade', seq=seq,
                        account_id=account.account_id,
                        account_type=account.account_type)
        except Exception:
            bridge.callbacks.pop(seq, None)
            raise
        with self._lock:
            self._subs[account] = seq
        return 0

    def unsubscribe(self, account):
        with self._lock:
            seq = self._subs.pop(account, None)
        if seq is None:
            return 0
        bridge = xtdata.get_client()
        bridge.callbacks.pop(seq, None)
        bridge.call('unsubscribe_trade', seq=seq)
        return 0

    # -- 查询 -------------------------------------------------------

    def _query(self, account, data_type):
        rows = xtdata.get_client().call(
            'get_trade_detail_data',
            account_id=account.account_id,
            account_type=account.account_type,
            data_type=data_type) or []
        cls = _TYPES[data_type]
        out = []
        for raw in rows:
            obj = cls(raw, account.account_id, account.account_type)
            out.append(obj)
        if data_type == 'order':
            self._remember(out)
        return out

    def query_stock_asset(self, account):
        """返回 XtAsset；账号查不到返回 None（与官方一致）。"""
        rows = self._query(account, 'account')
        return rows[0] if rows else None

    def query_stock_positions(self, account):
        # QMT 会把清零的持仓留在列表里，官方不返回这些，跟齐。
        return [p for p in self._query(account, 'position') if p.volume > 0]

    def query_stock_position(self, account, stock_code):
        for pos in self.query_stock_positions(account):
            if pos.stock_code == stock_code:
                return pos
        return None

    def query_stock_orders(self, account, cancelable_only=False):
        orders = self._query(account, 'order')
        if cancelable_only:
            orders = [o for o in orders if o.cancelable]
        return orders

    def query_stock_order(self, account, order_id):
        for order in self.query_stock_orders(account):
            if order.order_id == order_id:
                return order
        return None

    def query_stock_trades(self, account):
        return self._query(account, 'deal')

    # -- 下单 -------------------------------------------------------

    def order_stock(self, account, stock_code, order_type, order_volume,
                    price_type=xtconstant.FIX_PRICE, price=0,
                    strategy_name='', order_remark=''):
        """下单，返回委托号。参数顺序与官方 order_stock 一致。

        `price_type` 原样传给 passorder 的 prType：限价 11、最新价 5 两边一致，
        市价那几档两边编号不同，要用就查大QMT的 prType 表填数字。

        返回的委托号优先是交易所委托号；`order_resolve_timeout` 秒内认不回来
        就返回一个本地流水号，撤单时这个号也认。
        """
        marker = order_remark or self._new_marker()
        resp = xtdata.get_client().call(
            'passorder',
            op_type=order_type,
            order_type=xtconstant.FIX_VOLUME,   # order_stock 一律按股数，跟官方一致
            account_id=account.account_id,
            order_code=stock_code,
            price_type=price_type,
            price=price,
            volume=order_volume,
            strategy_name=strategy_name,
            user_order_id=marker)

        local_id = self._new_local_id()
        with self._lock:
            self._local_to_sysid[local_id] = marker
        if not resp or resp.get('passorder_args') != 11:
            # 老版 passorder 不接 userOrderId，备注没送出去，认不回来
            return local_id
        sysid = self._resolve(account, marker, self.order_resolve_timeout)
        if sysid and str(sysid).isdigit():
            return int(sysid)
        return local_id

    def order_stock_async(self, account, stock_code, order_type, order_volume,
                          price_type=xtconstant.FIX_PRICE, price=0,
                          strategy_name='', order_remark=''):
        """跟同步版走同一条路，只是不回查委托号（所以返回的是本地流水号）。

        官方这个是真异步、结果走 on_order_stock_async_response；这里只是不等。
        """
        saved = self.order_resolve_timeout
        self.order_resolve_timeout = 0
        try:
            order_id = self.order_stock(account, stock_code, order_type, order_volume,
                                        price_type, price, strategy_name, order_remark)
        finally:
            self.order_resolve_timeout = saved
        self._fire('on_order_stock_async_response',
                   XtOrderError(order_id, 0, ''))
        return order_id

    def cancel_order_stock(self, account, order_id):
        """撤单。成功返回 0，失败返回 -1（与官方一致，不抛异常）。

        order_id 可以是交易所委托号，也可以是 order_stock 返回的本地流水号。
        """
        sysid = self._to_sysid(account, order_id)
        if sysid is None:
            self._fire('on_cancel_error',
                       XtCancelError(order_id, -1, '找不到这个委托号对应的交易所委托号'))
            return -1
        return self.cancel_order_stock_sysid(account, sysid)

    def cancel_order_stock_sysid(self, account, sysid):
        try:
            xtdata.get_client().call('cancel_order',
                                     order_sysid=str(sysid),
                                     account_id=account.account_id,
                                     account_type=account.account_type)
            return 0
        except Exception as e:
            self._fire('on_cancel_error', XtCancelError(sysid, -1, str(e)))
            return -1

    def cancel_order_stock_async(self, account, order_id):
        return self.cancel_order_stock(account, order_id)

    def cancel_order_stock_sysid_async(self, account, sysid):
        return self.cancel_order_stock_sysid(account, sysid)

    # -- 内部 -------------------------------------------------------

    def _new_marker(self):
        """下单备注。QMT 那边字段短，别搞太长；够本地唯一就行。"""
        with self._lock:
            self._local_seq += 1
            seq = self._local_seq
        return 'QB%s%03d' % (time.strftime('%H%M%S'), seq % 1000)

    def _new_local_id(self):
        """认不回交易所委托号时用的本地号。刻意做得大，不会跟真委托号撞。"""
        with self._lock:
            self._local_seq += 1
            return 900000000 + self._local_seq

    def _remember(self, orders):
        """每次查委托都顺手记下 备注 -> 交易所委托号，撤单和认号都要用。"""
        with self._lock:
            for order in orders:
                if order.order_remark and order.order_sysid:
                    self._by_remark[order.order_remark] = order.order_sysid

    def _resolve(self, account, marker, timeout):
        """按下单备注把交易所委托号认回来。

        订阅着的话推送会先把它填进来（不花钱）；没订阅就回查委托列表。
        认不回来返回 None —— 委托还没进列表、或者这版 QMT 不回传备注。
        """
        deadline = time.time() + max(0.0, timeout)
        while True:
            with self._lock:
                sysid = self._by_remark.get(marker)
            if sysid:
                return sysid
            if time.time() >= deadline:
                return None
            try:
                self.query_stock_orders(account)
            except Exception:
                return None
            with self._lock:
                sysid = self._by_remark.get(marker)
            if sysid:
                return sysid
            if time.time() >= deadline:
                return None
            time.sleep(0.25)

    def _to_sysid(self, account, order_id):
        with self._lock:
            marker = self._local_to_sysid.get(order_id)
            if marker is not None:
                sysid = self._by_remark.get(marker)
                if sysid:
                    return sysid
        if marker is not None:
            # 下单时没认回来，撤之前再认一次
            return self._resolve(account, marker, 1.0)
        for order in self.query_stock_orders(account):
            if order.order_id == order_id:
                return order.order_sysid or str(order_id)
        return str(order_id) if str(order_id).isdigit() else None

    def _make_dispatcher(self, account):
        def dispatch(msg):
            if not isinstance(msg, dict):
                return
            kind = msg.get('kind')
            rows = msg.get('rows') or []
            if kind == 'order_error':
                for raw in rows:
                    self._fire('on_order_error', XtOrderError(0, -1, str(raw)))
                return
            cls = _TYPES.get(kind)
            if cls is None:
                return
            objs = [cls(raw, account.account_id, account.account_type) for raw in rows]
            if kind == 'order':
                self._remember(objs)
            method = {'order': 'on_stock_order', 'deal': 'on_stock_trade',
                      'asset': 'on_stock_asset', 'position': 'on_stock_position'}[kind]
            for obj in objs:
                self._fire(method, obj)
        return dispatch

    def _fire(self, method, *args):
        cb = self.callback
        if cb is None:
            return
        fn = getattr(cb, method, None)
        if fn is None:
            return
        fn(*args)
