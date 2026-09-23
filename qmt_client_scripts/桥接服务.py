# coding:gbk
# ==========================================================================
#  PASTE THIS FILE INTO THE QMT STRATEGY EDITOR ------ DO NOT RUN IT LOCALLY.
#  `python this_file.py` fails with a SyntaxError and would do nothing anyway:
#  there is no ContextInfo outside QMT.  (GBK sources with Chinese on 11+ lines
#  cannot be run as top-level scripts on this Python 3.6 build; the same file
#  imports fine.  See the encoding section in qmt_mock/README.md.)
# ==========================================================================
"""QMT 侧桥接服务：把大QMT策略运行时里的行情和交易数据，通过本地 TCP 转发给本机其它进程。

用法
----
1. 在 QMT「策略编辑器」里新建一个模型，把本文件内容整段粘进去；
2. 点「编译」，再点「运行」，保持 QMT 开着不要停止；
3. 「日志输出」里出现 `[bridge] 已监听 127.0.0.1:58611` 即成功；
4. 本地进程连上来取数：
   * 行情 —— `from qmt_bridge import xtdata`，签名对齐官方 xtquant.xtdata；
   * 交易 —— `from qmt_bridge.xttrader import XtQuantTrader`，签名对齐官方
     xtquant.xttrader（资金、持仓、委托、成交、下单、撤单）。

怎么跑（这条是踩出来的，别绕过）
--------------------------------
* **只在「模型交易」里新建策略交易并保持「运行中」。** 桥靠 daemon 线程活着，
  这些线程只在策略真正运行时才存在。
* **「启动本地python」必须取消勾选。** 勾上时 QMT 会去拉一个外部 Python 进程跑策略，
  路径没配就是 `execude cmd:  -u "...py"`（解释器为空）+ `return code:1`，
  Python 一行都不会执行，日志里连 `[bridge]` 都看不到。
* **桥跑着的时候，别在「策略编辑器」里点「运行」，也别反复保存。** 那会起一个短命实例，
  它会按下面的注册表逻辑把正在服务的那个关掉、自己接管端口，然后转眼被 QMT 拆掉线程，
  只剩一个能握手不回话的僵尸监听。真遇上了：回「模型交易」重新启动一次就好。

下单之前必读
------------
* **本文件会真的下单。** `passorder` 打的是这个策略绑定的那个真实账号，
  没有模拟盘开关。先把 `ALLOW_ORDER` 保持为 False 跑通查询链路，
  确认账号、代码、数量都对了再改成 True。
* `passorder` 在不同 QMT 版本上参数个数不一样，见下面的 `PASSORDER_ARGS`。
  **对不上时只会报 TypeError，绝不自动换签名重试** —— 重试有可能把同一笔
  单发两次。报错了照日志提示改常量。

设计要点
--------
* 只监听 127.0.0.1，且服务端只认白名单里的几个函数名，不做任何 eval/getattr 派发，
  所以这个端口不会变成任意代码执行的入口。
* 行情回调里**只做入队**，绝不做网络 IO —— 回调跑在 QMT 自己的线程上，
  在里面阻塞会把整个策略和行情一起拖死。队列满了丢最老的一条。
* 所有 ContextInfo 调用集中在一个专用线程上串行执行，避免多个客户端并发碰同一个
  C++ 上下文对象。
* 服务对象挂在 builtins 上，脚本被重新「运行」时能找到上一轮的实例并干净地关掉，
  不会出现端口被僵尸线程占住的情况。
* 交易数据没有推送接口，只能轮询。轮询也走那一个专用线程，跟行情请求排同一个队，
  所以不会有两个线程同时碰 QMT 的交易接口。客户端订阅了才轮询，没人订阅就歇着。
* 策略被停止时 QMT 会调 `stop(C)`（如果这个版本有的话），在那里把监听关掉，
  免得留下僵尸端口。认不认这个入口各版本不一样，所以只是尽力而为，见 ENABLE_STOP_HOOK。
"""

import builtins
import json
import socket
import threading
import time
import traceback

try:
    import queue
except ImportError:                       # 理论上用不到，py3.6 一定有 queue
    import Queue as queue


BRIDGE_HOST = '127.0.0.1'
BRIDGE_PORT = 58611
MAX_BACKLOG = 20000                       # 每客户端待发队列上限，超了丢最老的
REGISTRY_KEY = '_qmt_bridge_registry'

TRADE_POLL_INTERVAL = 1.0                 # 交易数据轮询间隔（秒），有人订阅才跑

# 每次 handlebar 让出多少时间给桥的后台线程。
#
# 为什么需要：QMT 的内嵌 Python 只在它自己进入 Python 执行时才调度解释器的线程。
# 光靠 daemon 线程，桥会被饿死 —— 实测灌 50 个请求进去，回应每 3 秒才出来一个，
# 节拍死死卡在 QMT 驱动策略的间隔上。而 handlebar 跑的时候 GIL 在我们手里，
# time.sleep 又会释放 GIL，所以在这儿空转一小会儿，就等于把执行权交给那些线程。
#
# 预算按 handlebar 的实际间隔自适应：间隔大就多让点，间隔小就少让点，
# 最多不超过 MAX（别把 QMT 的策略线程占太久）。设成 0 就是关掉这个机制。
HANDLEBAR_YIELD_FRACTION = 0.3            # 让出 handlebar 间隔的这个比例
HANDLEBAR_YIELD_MAX = 0.3                 # 单次最多让出多少秒

# 策略停止时要不要顺手把监听关掉。QMT 停策略时会把 Python 线程拆掉，但操作系统层面的
# 监听 fd 还留着，表现就是「端口连得上、发请求没人回」。有 stop(C) 入口的版本能靠它收干净。
# 万一你这版 QMT 在不该调的时候调 stop（比如每根 bar 都调），把这个改成 False 就退回原状。
ENABLE_STOP_HOOK = True

# 真的允许下单/撤单吗。默认 False —— 查询随便查，下单先挡住。
# 确认账号和参数都对了，再改成 True 重新「运行」一次。
ALLOW_ORDER = False

# 本机 QMT 的 passorder 接几个参数。不同版本不一样，从长到短是：
#   11: (opType, orderType, accountID, orderCode, prType, modelprice, volume,
#        strategyName, quickTrade, userOrderId, ContextInfo)   <- 较新版本
#   10: 同上去掉 userOrderId
#    8: (opType, orderType, accountID, orderCode, prType, modelprice, volume,
#        ContextInfo)                                          <- 老版本
# 下单时报 TypeError 就按日志提示换一个数。**不自动试**：试错有可能重复下单。
# 注意只有 11 参数的版本支持 userOrderId（投资备注），也只有它下完单能靠备注
# 把委托号认回来；10 和 8 参数下 order_stock 返回的是本地流水号。
PASSORDER_ARGS = 11

# get_trade_detail_data 的 accounttype 取值：客户端传 miniQMT 那套大写名字，
# 这里翻成大QMT认的小写名字。认不出来的原样传过去。
_ACCT_TYPE_ALIAS = {
    'STOCK': 'stock',
    'SECURITY': 'stock',
    'CREDIT': 'credit',
    'FUTURE': 'future',
    'OPTION': 'stock_option',
    'STOCK_OPTION': 'stock_option',
    'HUGANGTONG': 'hugangtong',
    'SHENGANGTONG': 'shengangtong',
}


# ---------------------------------------------------------------- 工具函数

def _registry():
    """跨脚本重载存活的全局注册表。"""
    reg = getattr(builtins, REGISTRY_KEY, None)
    if reg is None:
        reg = {}
        setattr(builtins, REGISTRY_KEY, reg)
    return reg


def _json_default(o):
    try:
        import numpy as np
        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:
        pass
    if isinstance(o, bytes):
        return o.decode('utf-8', 'replace')
    return str(o)


def _to_wire(obj):
    """DataFrame 拆成可 JSON 化的三段，其余结构原样递归。"""
    try:
        import pandas as pd
    except ImportError:
        pd = None
    if pd is not None and isinstance(obj, pd.DataFrame):
        return {'__df__': 1,
                'index': list(obj.index),
                'columns': list(obj.columns),
                'data': obj.values.tolist()}
    if isinstance(obj, dict):
        return dict((k, _to_wire(v)) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return [_to_wire(v) for v in obj]
    return obj


def _normalize_ticks(datas):
    """把 subscribe_quote(result_type='list') 的回调数据整成 [tick, tick, ...]。

    QMT 这里给的是 {字段: [值1, 值2, ...]}，转置成一行一个 tick，
    这样形状就跟官方 xtdata 的 {code: [tick, ...]} 对上了。
    """
    if isinstance(datas, list):
        return datas
    if not isinstance(datas, dict):
        return [datas]
    keys = list(datas.keys())
    if not keys:
        return []
    probe = datas[keys[0]]
    if not isinstance(probe, (list, tuple)):
        return [datas]
    out = []
    for i in range(len(probe)):
        row = {}
        for k in keys:
            v = datas[k]
            row[k] = v[i] if isinstance(v, (list, tuple)) and i < len(v) else v
        out.append(row)
    return out


def _log(msg):
    print('[bridge] %s' % msg)


# ---------------------------------------------------------------- 交易工具

_ATTR_CACHE = {}


def _dump_obj(obj):
    """把 QMT 的交易对象摊平成 dict。

    get_trade_detail_data 返回的是 C++ 包出来的对象，没有 __dict__，
    字段全是 m_ 开头的属性。dir() 不便宜，按类型缓存一次字段名，
    之后每个对象只做 getattr —— 持仓几百行、一秒一轮，这个差别看得见。

    字段名不做任何翻译，原样送到客户端去翻。QMT 侧改一次要重新粘贴一次，
    而各版本的字段名又不尽相同，所以把这层易变的东西放在本地更好改。
    """
    if obj is None or isinstance(obj, (bool, int, float, str, bytes)):
        return obj
    if isinstance(obj, dict):
        return dict((k, _dump_obj(v)) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return [_dump_obj(v) for v in obj]

    cls = type(obj)
    names = _ATTR_CACHE.get(cls)
    if names is None:
        names = []
        for n in dir(obj):
            if not n.startswith('m_'):
                continue
            try:
                if callable(getattr(obj, n)):
                    continue
            except Exception:
                continue
            names.append(n)
        _ATTR_CACHE[cls] = names

    if not names:                         # 认不出来的东西，别硬塞，交给 _to_wire
        return _to_wire(obj)
    out = {}
    for n in names:
        try:
            out[n] = _to_wire(getattr(obj, n))
        except Exception:
            pass
    return out


def _qmt_func(name):
    """QMT 引擎把 passorder / get_trade_detail_data / cancel 注入到脚本全局里。

    本地跑自测时它们不在，所以每次现查 —— 顺便让自测能塞假的进来。
    """
    fn = globals().get(name)
    if fn is None:
        raise RuntimeError(
            '当前运行环境里没有 %s。这个接口只有大QMT策略运行时才有，'
            '本文件必须粘到「策略编辑器」里运行。' % name)
    return fn


def _norm_acct_type(t):
    if t is None or t == '':
        return 'stock'
    if isinstance(t, int):
        return t
    s = str(t).strip()
    return _ACCT_TYPE_ALIAS.get(s.upper(), s.lower())


def _order_key(row):
    """委托的身份。交易所委托号最靠谱，没有就退而求其次用本地号加备注。"""
    for k in ('m_strOrderSysID', 'm_strOrderSysId', 'm_nOrderSysID'):
        v = row.get(k)
        if v not in (None, '', 0):
            return 'S%s' % (v,)
    return 'R%s|%s|%s' % (row.get('m_nOrderRef'), row.get('m_strInstrumentID'),
                          row.get('m_strInsertTime'))


def _deal_key(row):
    for k in ('m_strTradeID', 'm_strTradeId', 'm_strDealID'):
        v = row.get(k)
        if v not in (None, '', 0):
            return 'T%s|%s' % (v, row.get('m_strInstrumentID'))
    return 'X%s|%s|%s|%s' % (row.get('m_strInstrumentID'), row.get('m_strTradeTime'),
                             row.get('m_dPrice'), row.get('m_nVolume'))


def _pos_key(row):
    return '%s.%s|%s' % (row.get('m_strInstrumentID'), row.get('m_strExchangeID'),
                         row.get('m_nDirection'))


# ---------------------------------------------------------------- 客户端连接

class _Conn(object):
    """一条客户端连接：一个读线程收请求，一个写线程发响应和推送。"""

    def __init__(self, server, sock, addr):
        self.server = server
        self.sock = sock
        self.addr = addr
        self.out = queue.Queue(MAX_BACKLOG)
        self.alive = True
        self.subs = {}                    # 客户端订阅号 -> QMT 订阅号
        self.trade_subs = {}              # 客户端订阅号 -> 交易轮询状态
        self.dropped = 0

    def start(self):
        threading.Thread(target=self._read_loop, name='bridge-read', daemon=True).start()
        threading.Thread(target=self._write_loop, name='bridge-write', daemon=True).start()

    def send(self, obj):
        """只入队，永不阻塞。队列满了丢最老的一条，保证行情线程不被拖住。"""
        if not self.alive:
            return
        try:
            self.out.put_nowait(obj)
            return
        except queue.Full:
            pass
        try:
            self.out.get_nowait()
            self.dropped += 1
        except queue.Empty:
            pass
        try:
            self.out.put_nowait(obj)
        except queue.Full:
            self.dropped += 1

    def _read_loop(self):
        buf = b''
        try:
            while self.alive:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        req = json.loads(line.decode('utf-8'))
                    except Exception:
                        continue
                    self.server.calls.put((self, req))
        except Exception:
            pass
        finally:
            self.close()

    def _write_loop(self):
        while True:
            obj = self.out.get()
            if obj is None:
                break
            try:
                data = json.dumps(obj, default=_json_default, ensure_ascii=False)
                self.sock.sendall(data.encode('utf-8') + b'\n')
            except Exception:
                break
        self.close()

    def close(self):
        if not self.alive:
            return
        self.alive = False
        self.server.drop_conn(self)
        try:
            self.out.put_nowait(None)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 服务端

class BridgeServer(object):

    def __init__(self, C, host=BRIDGE_HOST, port=BRIDGE_PORT):
        self.C = C
        self.host = host
        self.port = port
        self.running = False
        self.listener = None
        self.calls = queue.Queue()
        self.conns = []
        self.lock = threading.Lock()
        self.poll_event = threading.Event()   # 下单/成交回调拿它把轮询叫醒
        self.poll_inflight = False            # 上一轮还没跑完就不再排队，免得堆积
        self.passorder_args = PASSORDER_ARGS
        self.owner = C                        # 哪个 ContextInfo 起的，stop(C) 靠它认领
        self.ticks = 0                        # handlebar 被调了多少次，用来报频率
        self.last_tick = 0.0
        self.tick_log_at = 0.0

    # -- 生命周期 ---------------------------------------------------

    def start(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind((self.host, self.port))
        self.listener.listen(8)
        self.running = True
        threading.Thread(target=self._accept_loop, name='bridge-accept', daemon=True).start()
        threading.Thread(target=self._call_loop, name='bridge-call', daemon=True).start()
        threading.Thread(target=self._poll_loop, name='bridge-poll', daemon=True).start()
        _log('已监听 %s:%d' % (self.host, self.port))
        _log('下单开关 ALLOW_ORDER=%s，passorder 参数个数 PASSORDER_ARGS=%d'
             % (ALLOW_ORDER, self.passorder_args))

    def stop(self):
        self.running = False
        try:
            self.listener.close()
        except Exception:
            pass
        for conn in list(self.conns):
            conn.close()
        self.calls.put(None)
        self.poll_event.set()
        _log('已停止')

    def rebind(self, C):
        """脚本被重新运行时，把服务挂到新的 ContextInfo 上。"""
        self.C = C
        self.owner = C
        _log('已绑定新的 ContextInfo')

    # -- 连接管理 ---------------------------------------------------

    def _accept_loop(self):
        while self.running:
            try:
                sock, addr = self.listener.accept()
            except Exception:
                break
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn = _Conn(self, sock, addr)
            with self.lock:
                self.conns.append(conn)
            conn.start()
            _log('客户端接入 %s' % (addr,))

    def drop_conn(self, conn):
        with self.lock:
            if conn in self.conns:
                self.conns.remove(conn)
        for seq in list(conn.subs):
            self._unsub(conn, seq)
        conn.trade_subs.clear()
        _log('客户端断开 %s（丢弃 %d 条）' % (conn.addr, conn.dropped))

    # -- 请求派发（全部跑在这一个线程上） ---------------------------

    def _call_loop(self):
        while True:
            item = self.calls.get()
            if item is None:
                break
            if callable(item):            # 交易轮询也排这个队，保证串行
                try:
                    item()
                except Exception:
                    _log('轮询出错：%s' % traceback.format_exc())
                continue
            conn, req = item
            rid = req.get('id')
            try:
                ret = self._dispatch(conn, req)
                conn.send({'id': rid, 'ok': True, 'ret': _to_wire(ret)})
            except Exception as e:
                _log('请求出错 %s: %s' % (req.get('func'), traceback.format_exc()))
                conn.send({'id': rid, 'ok': False,
                           'err': '%s: %s' % (type(e).__name__, e)})

    def _unsub(self, conn, seq):
        subid = conn.subs.pop(seq, None)
        if subid is None:
            return
        try:
            self.C.unsubscribe_quote(subid)
        except Exception:
            pass

    def _dispatch(self, conn, req):
        func = req.get('func')
        kw = req.get('kwargs') or {}
        C = self.C

        if func == 'ping':
            return 'pong'

        # 订阅号由客户端自己分配后传过来。这样它能在发请求之前就把回调挂好，
        # 首包推送早于响应到达也不会丢。
        if func == 'subscribe_quote':
            code = kw['stock_code']
            period = kw.get('period', 'tick')
            dividend_type = kw.get('dividend_type', 'follow')
            seq = kw['seq']

            def on_quote(datas, _seq=seq, _conn=conn, _code=code):
                try:
                    payload = datas.get(_code, datas) if isinstance(datas, dict) else datas
                    _conn.send({'push': _seq, 'data': {_code: _normalize_ticks(payload)}})
                except Exception:
                    pass

            subid = C.subscribe_quote(code, period, dividend_type, 'list', on_quote)
            if subid <= 0:
                raise RuntimeError('subscribe_quote 失败，返回 %s' % subid)
            conn.subs[seq] = subid
            return seq

        if func == 'subscribe_whole_quote':
            code_list = kw['code_list']
            seq = kw['seq']

            def on_whole(datas, _seq=seq, _conn=conn):
                try:
                    _conn.send({'push': _seq, 'data': datas})
                except Exception:
                    pass

            subid = C.subscribe_whole_quote(code_list, on_whole)
            if subid <= 0:
                raise RuntimeError('subscribe_whole_quote 失败，返回 %s' % subid)
            conn.subs[seq] = subid
            return seq

        if func == 'unsubscribe_quote':
            self._unsub(conn, kw['seq'])
            return True

        if func == 'get_full_tick':
            return C.get_full_tick(kw['code_list'])

        if func == 'get_market_data_ex':
            return C.get_market_data_ex(
                kw.get('field_list', []),
                kw.get('stock_list', []),
                kw.get('period', '1d'),
                kw.get('start_time', ''),
                kw.get('end_time', ''),
                kw.get('count', -1),
                kw.get('dividend_type', 'none'),
                kw.get('fill_data', True),
                kw.get('subscribe', True),
            )

        if func == 'get_instrument_detail':
            return C.get_instrument_detail(kw['stock_code'])

        if func == 'get_stock_list_in_sector':
            return C.get_stock_list_in_sector(kw['sector_name'])

        if func == 'get_trading_dates':
            return C.get_trading_dates(kw['stock_code'], kw.get('start_date', ''),
                                       kw.get('end_date', ''), kw.get('count', -1),
                                       kw.get('period', '1d'))

        # ---- 交易 ----------------------------------------------------

        if func == 'get_trade_detail_data':
            return self._trade_rows(kw['account_id'], kw.get('account_type'),
                                    kw['data_type'], kw.get('strategy_name', ''))

        if func == 'get_trade_account':
            # 策略在界面上绑的那个账号，客户端不传账号时拿它当默认值。
            return {'account_id': str(getattr(C, 'accountid', '')
                                      or getattr(C, 'accountID', '') or ''),
                    'account_type': str(getattr(C, 'accounttype', '') or '')}

        if func == 'passorder':
            return self._passorder(kw)

        if func == 'cancel_order':
            if not ALLOW_ORDER:
                raise RuntimeError('撤单被挡下了：桥接服务里 ALLOW_ORDER 是 False')
            fn = _qmt_func('cancel')
            return fn(str(kw['order_sysid']), str(kw['account_id']),
                      _norm_acct_type(kw.get('account_type')), C)

        if func == 'subscribe_trade':
            seq = kw['seq']
            conn.trade_subs[seq] = {
                'account_id': str(kw['account_id']),
                'account_type': _norm_acct_type(kw.get('account_type')),
                'strategy_name': kw.get('strategy_name', '') or '',
                'init': True,             # 首轮推全量快照，之后只推变化
                'orders': {}, 'deals': set(), 'account': None, 'positions': {},
            }
            self.poll_event.set()
            return seq

        if func == 'unsubscribe_trade':
            conn.trade_subs.pop(kw['seq'], None)
            return True

        raise RuntimeError('未知的函数名：%r' % (func,))

    # -- 交易 -------------------------------------------------------

    def _trade_rows(self, account_id, account_type, data_type, strategy_name=''):
        fn = _qmt_func('get_trade_detail_data')
        rows = fn(str(account_id), _norm_acct_type(account_type),
                  str(data_type).lower(), strategy_name or '')
        return [_dump_obj(r) for r in (rows or [])]

    def _passorder(self, kw):
        if not ALLOW_ORDER:
            raise RuntimeError(
                '下单被挡下了：桥接服务里 ALLOW_ORDER 是 False。'
                '确认账号和参数都对了，把它改成 True 再重新「运行」一次。')
        fn = _qmt_func('passorder')

        order_type = int(kw.get('order_type', 1101))
        volume = kw['volume']
        # 1101 是按股数下单，必须是整数；1102（按金额）之类的是浮点。
        volume = int(volume) if order_type == 1101 else float(volume)

        head = (int(kw['op_type']), order_type, str(kw['account_id']),
                str(kw['order_code']), int(kw.get('price_type', 11)),
                float(kw.get('price', 0) or 0), volume)
        sname = str(kw.get('strategy_name', '') or '')
        quick = int(kw.get('quick_trade', 1))
        uid = str(kw.get('user_order_id', '') or '')

        n = int(kw.get('passorder_args') or self.passorder_args)
        if n == 11:
            args = head + (sname, quick, uid, self.C)
        elif n == 10:
            args = head + (sname, quick, self.C)
        elif n == 8:
            args = head + (self.C,)
        else:
            raise RuntimeError('PASSORDER_ARGS 只能是 11 / 10 / 8，现在是 %r' % (n,))

        try:
            ret = fn(*args)
        except TypeError as e:
            # 这里绝不换个签名重试 —— 万一单已经发出去了，重试就是发两笔。
            _log('passorder 报 TypeError：%s' % e)
            _log('多半是参数个数对不上。把本文件开头的 PASSORDER_ARGS 换成 '
                 '11 / 10 / 8 里的另一个，重新「运行」一次。当前是 %d。' % n)
            raise
        _log('已下单 %s 数量=%s 账号=%s 备注=%s' % (head[3], volume, head[2], uid))
        return {'ret': _to_wire(ret), 'user_order_id': uid, 'passorder_args': n}

    # -- 交易轮询（交易数据没有推送接口，只能定期拉） ---------------

    def _has_trade_subs(self):
        with self.lock:
            return any(conn.trade_subs for conn in self.conns)

    def _poll_loop(self):
        while self.running:
            if self.poll_event.wait(TRADE_POLL_INTERVAL):
                self.poll_event.clear()
            if not self.running:
                break
            if self.poll_inflight or not self._has_trade_subs():
                continue
            self.poll_inflight = True
            self.calls.put(self._poll_trade)

    def _poll_trade(self):
        """跑在那一个专用调用线程上，跟客户端请求排同一个队。"""
        try:
            subs = []
            with self.lock:
                for conn in self.conns:
                    for seq, st in list(conn.trade_subs.items()):
                        subs.append((conn, seq, st))
            if not subs:
                return
            if globals().get('get_trade_detail_data') is None:
                return

            cache = {}                    # 多个客户端订阅同一个账号时只查一次

            def rows(acct, atype, dtype, sname):
                key = (acct, atype, dtype, sname)
                if key not in cache:
                    try:
                        cache[key] = self._trade_rows(acct, atype, dtype, sname)
                    except Exception:
                        _log('查 %s 出错：%s' % (dtype, traceback.format_exc()))
                        cache[key] = []
                return cache[key]

            for conn, seq, st in subs:
                if not conn.alive:
                    continue
                try:
                    self._poll_one(conn, seq, st, rows)
                except Exception:
                    _log('交易轮询出错：%s' % traceback.format_exc())
        finally:
            self.poll_inflight = False

    def _poll_one(self, conn, seq, st, rows):
        acct, atype, sname = st['account_id'], st['account_type'], st['strategy_name']
        init = st['init']

        def push(kind, data):
            conn.send({'push': seq, 'data': {'kind': kind, 'rows': data, 'init': init}})

        # 委托：状态或已成数量变了就报一次
        changed = []
        for row in rows(acct, atype, 'order', sname):
            key = _order_key(row)
            sig = (row.get('m_nOrderStatus'), row.get('m_nVolumeTraded'))
            if st['orders'].get(key) != sig:
                st['orders'][key] = sig
                changed.append(row)
        if changed or init:
            push('order', changed)

        # 成交：只报没见过的。每轮把集合重置成当前这批，跨日自然清掉，也不会涨爆
        seen, fresh = set(), []
        for row in rows(acct, atype, 'deal', sname):
            key = _deal_key(row)
            seen.add(key)
            if key not in st['deals']:
                fresh.append(row)
        st['deals'] = seen
        if fresh or init:
            push('deal', fresh)

        # 资金
        acc = rows(acct, atype, 'account', sname)
        acc = acc[0] if acc else None
        if acc is not None and (init or acc != st['account']):
            st['account'] = acc
            push('asset', [acc])

        # 持仓：变了的推变化，清空了的补一条 0 手，否则客户端那边会一直挂着
        cur = dict((_pos_key(p), p) for p in rows(acct, atype, 'position', sname))
        if init or cur != st['positions']:
            moved = [p for k, p in cur.items() if st['positions'].get(k) != p]
            for key, old in st['positions'].items():
                if key not in cur:
                    gone = dict(old)
                    gone['m_nVolume'] = 0
                    gone['m_nCanUseVolume'] = 0
                    moved.append(gone)
            st['positions'] = cur
            if moved or init:
                push('position', moved)

        st['init'] = False


    # -- 让出时间片（handlebar 里调） -------------------------------

    def tick(self):
        """QMT 每次调 handlebar 都走这里，把执行权让给桥的后台线程。

        见 HANDLEBAR_YIELD_FRACTION 上面那段注释。顺带每 10 秒报一次
        handlebar 的真实调用频率 —— 这个数决定桥的响应能快到什么程度。
        """
        now = time.time()
        self.ticks += 1

        if self.tick_log_at == 0.0:
            self.tick_log_at = now
        elif now - self.tick_log_at >= 10.0:
            _log('handlebar 最近 %.1f 秒被调用 %d 次'
                 % (now - self.tick_log_at, self.ticks))
            self.ticks = 0
            self.tick_log_at = now

        gap = (now - self.last_tick) if self.last_tick else HANDLEBAR_YIELD_MAX
        self.last_tick = now
        if HANDLEBAR_YIELD_FRACTION <= 0:
            return
        budget = min(HANDLEBAR_YIELD_MAX, gap * HANDLEBAR_YIELD_FRACTION)
        deadline = now + budget
        while time.time() < deadline:
            time.sleep(0.002)             # sleep 会释放 GIL，别换成空 pass


# ---------------------------------------------------------------- 策略入口

def init(C):
    reg = _registry()
    old = reg.get('server')
    if old is not None:
        try:
            old.stop()
        except Exception:
            pass
        reg.pop('server', None)

    server = BridgeServer(C, BRIDGE_HOST, BRIDGE_PORT)
    try:
        server.start()
    except OSError as e:
        _log('监听 %s:%d 失败：%s' % (BRIDGE_HOST, BRIDGE_PORT, e))
        _log('端口多半被上一轮的残留进程占着，重启 QMT 或换个 BRIDGE_PORT 再试')
        return
    reg['server'] = server


def handlebar(C):
    """桥不依赖 bar 驱动，但**必须**借 handlebar 让出时间片。

    QMT 的内嵌 Python 只在这时候才调度解释器的线程，不在这儿让一让，
    桥的后台线程就被饿死（实测每 3 秒才推进一个请求）。详见 tick()。
    """
    reg = _registry()
    server = reg.get('server')
    if server is None:
        return
    if server.C is not C:
        server.rebind(C)
    server.tick()


def stop(C):
    """策略被停止时 QMT 会调这里（认不认这个入口各版本不一样）。

    只关**自己起的**那个 server：编辑器里跑一下、或者算一次指标，都会走 init 抢走端口
    并把自己登记进注册表，那一轮结束时收自己的摊子是对的；而如果注册表里已经是别人
    （比如「模型交易」那个常驻实例）起的 server，就绝不能碰 —— 否则一次误触发就能
    把正在服务的桥搞死。
    """
    if not ENABLE_STOP_HOOK:
        return
    reg = _registry()
    server = reg.get('server')
    if server is None:
        return
    if server.owner is not C and server.C is not C:
        _log('策略停止，但当前监听不是这一轮起的，不动它')
        return
    try:
        server.stop()
    except Exception:
        pass
    reg.pop('server', None)


# ---------------------------------------------------------------- 引擎回调
#
# QMT 有委托/成交就会调下面这几个。参数个数各版本不一样，所以一律 *args 收着 ——
# 签名对不上引擎会直接报错，比少一次提醒糟糕得多。
#
# 这里不读回调带来的那个对象（形状各版本不同，不敢信），只是把轮询叫醒，
# 让它立刻去查一次权威数据。省下的是那最多一秒的等待。

def _nudge():
    server = _registry().get('server')
    if server is not None:
        try:
            server.poll_event.set()
        except Exception:
            pass


def order_callback(*args):
    _nudge()


def deal_callback(*args):
    _nudge()


def orderError_callback(*args):
    try:
        _log('委托报错：%s' % (args[1:],))
    except Exception:
        pass
    _nudge()
