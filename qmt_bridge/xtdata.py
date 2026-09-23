# coding:utf-8
"""xtdata 的替身：签名照抄官方 xtquant.xtdata，底层走本机到大QMT的桥。

国金把 miniQMT 停了之后，官方 `xtquant.xtdata` 连得上 58600 但每个取数接口都返回
ErrorID 200005「未找到订阅数据」。行情只在大QMT策略运行时里拿得到，所以数据从
`qmt_client_scripts/桥接服务.py` 转出来，这里把它重新包成 xtdata 的样子。

用法就是换一行 import：

    from qmt_bridge import xtdata          # 走桥
    # from xtquant import xtdata           # 官方（等 miniQMT 恢复了换回来）

    def on_data(datas):
        for code, ticks in datas.items():
            for t in ticks:
                print(code, t['time'], t['lastPrice'])

    xtdata.subscribe_quote('000001.SZ', period='tick', callback=on_data)
    xtdata.run()

刻意保持一致的地方：函数名、位置参数顺序、返回值形状（subscribe_quote 推
`{code: [tick, ...]}`，subscribe_whole_quote 推 `{code: tick}`），以及
`get_market_data_ex` 返回 `{code: DataFrame}`。

已知差异：
* 没有 download_history_data —— 大QMT 的补数据是客户端自己管的，桥不转。
* `start_time`/`end_time`/`count` 在 subscribe_quote 里被忽略（QMT 的
  ContextInfo.subscribe_quote 不接这几个参数），只订阅增量推送，不回补历史。
  要历史请单独调 get_market_data_ex。
* 回调在桥的接收线程上执行，跟官方一样：**别在回调里做耗时的事**。
* **QMT 侧策略的周期必须是「分笔线」。** 桥靠后台线程干活，而 QMT 的内嵌 Python
  似乎只在它自己进入 Python 执行时才调度这些线程。日线周期下 handlebar 几乎不触发，
  线程就饿死 —— 表现是连得上、请求大面积超时（实测 90 秒 90 个 ping 只回 20 个）。
"""

import json
import socket
import threading
import time

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 58611
DEFAULT_TIMEOUT = 30.0
VERIFY_TIMEOUT = 5.0         # 连上之后确认对面真的在服务，等这么久

enable_hello = True          # 与官方同名，置 False 可关掉连接提示


class BridgeError(Exception):
    pass


# ---------------------------------------------------------------- 连接

class _Bridge(object):

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = None
        self.alive = False
        self.lock = threading.Lock()
        self.next_id = 1
        self.next_seq = 1
        self.pending = {}            # 请求号 -> [Event, 结果, 错误]
        self.cb_errors = 0           # 回调报错次数，用于日志限流
        self.callbacks = {}          # 订阅号 -> 回调

    def new_seq(self):
        """订阅号由客户端分配，好在发请求之前就把回调挂上，避免首包被丢。"""
        with self.lock:
            seq = self.next_seq
            self.next_seq += 1
        return seq

    def connect(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect((self.host, self.port))
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = sock
        self.alive = True
        threading.Thread(target=self._read_loop, name='xtbridge-read', daemon=True).start()

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except Exception:
            pass
        # 叫醒所有还在等的调用，别让它们卡到超时
        for box in list(self.pending.values()):
            box[2] = BridgeError('与桥的连接已断开')
            box[0].set()

    def _read_loop(self):
        buf = b''
        try:
            while self.alive:
                chunk = self.sock.recv(262144)
                if not chunk:
                    break
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    line = line.strip()
                    if line:
                        self._handle(line)
        except Exception:
            pass
        finally:
            self.close()

    def _handle(self, line):
        try:
            msg = json.loads(line.decode('utf-8'))
        except Exception:
            return

        seq = msg.get('push')
        if seq is not None:
            cb = self.callbacks.get(seq)
            if cb:
                try:
                    cb(_from_wire(msg.get('data')))
                except Exception:
                    self._log_callback_error()
            return

        box = self.pending.pop(msg.get('id'), None)
        if box is None:
            return
        if msg.get('ok'):
            box[1] = _from_wire(msg.get('ret'))
        else:
            box[2] = BridgeError(msg.get('err', '未知错误'))
        box[0].set()

    def _log_callback_error(self):
        """回调自己抛异常时打日志，但要限流。

        tick 速率下一个坏回调能刷出每秒上千条 traceback，print 本身
        就会变成瓶颈还占着 GIL。所以前几次打全栈，之后只按间隔报累计数。
        """
        import traceback
        self.cb_errors += 1
        n = self.cb_errors
        if n <= 3:
            traceback.print_exc()
            if n == 3:
                print('[qmt_bridge] 回调连续报错，后续只汇总不再打完整堆栈')
        elif n % 1000 == 0:
            last = traceback.format_exc().strip().splitlines()[-1]
            print('[qmt_bridge] 回调已累计报错 %d 次，最近一次：%s' % (n, last))

    def call(self, func, timeout=DEFAULT_TIMEOUT, **kwargs):
        if not self.alive:
            raise BridgeError('与桥的连接已断开')
        with self.lock:
            rid = self.next_id
            self.next_id += 1
            payload = json.dumps({'id': rid, 'func': func, 'kwargs': kwargs},
                                 ensure_ascii=False).encode('utf-8') + b'\n'
            box = [threading.Event(), None, None]
            self.pending[rid] = box
            try:
                self.sock.sendall(payload)
            except Exception as e:
                self.pending.pop(rid, None)
                raise BridgeError('发送失败：%s' % e)

        if not box[0].wait(timeout):
            self.pending.pop(rid, None)
            raise BridgeError('调用 %s 超时（%.1fs）' % (func, timeout))
        if box[2] is not None:
            raise box[2]
        return box[1]


_bridge = None


def _from_wire(obj):
    """还原服务端打包的 DataFrame，其余结构原样递归。"""
    if isinstance(obj, dict):
        if obj.get('__df__'):
            import pandas as pd
            return pd.DataFrame(obj['data'], index=obj['index'], columns=obj['columns'])
        return dict((k, _from_wire(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return [_from_wire(v) for v in obj]
    return obj


def _client():
    global _bridge
    if _bridge is None or not _bridge.alive:
        connect()
    return _bridge


# ---------------------------------------------------------------- 公开 API

def connect(ip='', port=None, remember_if_success=True, verify=True):
    """与官方同名。ip/port 留空就连本机默认端口。

    `verify` 是桥自己加的：连上之后先 ping 一次，确认对面真的在服务。
    """
    global _bridge
    if _bridge is not None and _bridge.alive:
        return _bridge

    host = ip or DEFAULT_HOST
    prt = port or DEFAULT_PORT
    bridge = _Bridge(host, prt)
    try:
        bridge.connect()
    except Exception as e:
        raise BridgeError(
            '连不上 QMT 桥 %s:%d（%s）。\n'
            '请确认大QMT已启动，且「模型交易」里的「桥接服务」策略状态是「运行中」。' % (host, prt, e)) from None

    # 端口在、却没人应答 —— 僵尸监听。QMT 停止策略时会把 Python 线程拆掉，
    # 但操作系统层面的监听 fd 留在 QMT 进程里，于是 TCP 握手成功、没人 accept。
    # 不在这儿挡一下，后面每个请求都要干等到超时，报出来的是「调用 xxx 超时」，
    # 看不出真正的原因。
    if verify:
        try:
            bridge.call('ping', timeout=VERIFY_TIMEOUT)
        except Exception:
            bridge.close()
            raise BridgeError(
                'QMT 桥 %s:%d 的端口在，但不回应请求。两种可能：\n'
                '  1) 策略被停了，监听 fd 还留在 QMT 进程里（僵尸端口）——\n'
                '     回「模型交易」把「桥接服务」那一行重新启动一次；\n'
                '  2) 策略周期是「日线」这种低频的，QMT 很少进 Python，\n'
                '     桥的后台线程抢不到时间片（实测 90 秒只应答 20 次）——\n'
                '     把策略周期改成「分笔线」。' % (host, prt)) from None

    _bridge = bridge
    if enable_hello:
        print('***** 已连接 QMT 桥 %s:%d  %s *****'
              % (host, prt, time.strftime('%Y-%m-%d %H:%M:%S')))
        print('设置 qmt_bridge.xtdata.enable_hello = False 可关闭此提示')
    return bridge


def reconnect(ip='', port=None, remember_if_success=True):
    disconnect()
    return connect(ip, port, remember_if_success)


def disconnect():
    global _bridge
    if _bridge is not None:
        _bridge.close()
        _bridge = None


def get_client():
    return _client()


def subscribe_quote(stock_code, period='1d', start_time='', end_time='',
                    count=0, callback=None):
    """订阅单个标的。period='tick' 即逐笔推送。

    回调收到 {stock_code: [tick, tick, ...]}，与官方一致。
    start_time/end_time/count 收下但不生效，见模块开头的“已知差异”。
    """
    return subscribe_quote2(stock_code, period, start_time, end_time, count, None, callback)


def subscribe_quote2(stock_code, period='1d', start_time='', end_time='',
                     count=0, dividend_type=None, callback=None):
    bridge = _client()
    seq = bridge.new_seq()
    if callback:
        bridge.callbacks[seq] = callback
    try:
        return bridge.call('subscribe_quote', seq=seq,
                           stock_code=stock_code,
                           period=period,
                           dividend_type=dividend_type or 'follow')
    except Exception:
        bridge.callbacks.pop(seq, None)
        raise


def subscribe_whole_quote(code_list, callback=None):
    """全推。回调收到 {code: tick}（单条快照，不是列表），与官方一致。"""
    bridge = _client()
    seq = bridge.new_seq()
    if callback:
        bridge.callbacks[seq] = callback
    try:
        return bridge.call('subscribe_whole_quote', seq=seq, code_list=code_list)
    except Exception:
        bridge.callbacks.pop(seq, None)
        raise


def unsubscribe_quote(seq):
    bridge = _client()
    bridge.callbacks.pop(seq, None)
    return bridge.call('unsubscribe_quote', seq=seq)


def get_full_tick(code_list):
    return _client().call('get_full_tick', code_list=code_list)


def get_market_data_ex(field_list=[], stock_list=[], period='1d',
                       start_time='', end_time='', count=-1,
                       dividend_type='none', fill_data=True):
    """返回 {code: DataFrame}，与官方一致。"""
    return _client().call('get_market_data_ex', timeout=120.0,
                          field_list=field_list, stock_list=stock_list,
                          period=period, start_time=start_time, end_time=end_time,
                          count=count, dividend_type=dividend_type,
                          fill_data=fill_data, subscribe=True)


def get_instrument_detail(stock_code):
    return _client().call('get_instrument_detail', stock_code=stock_code)


def get_stock_list_in_sector(sector_name):
    return _client().call('get_stock_list_in_sector', sector_name=sector_name)


def get_trading_dates(stock_code, start_date='', end_date='', count=-1, period='1d'):
    return _client().call('get_trading_dates', stock_code=stock_code,
                          start_date=start_date, end_date=end_date,
                          count=count, period=period)


def run():
    """阻塞当前线程接收推送；桥断开时抛异常。与官方同名同语义。"""
    bridge = _client()
    while True:
        time.sleep(1)
        if not bridge.alive:
            raise BridgeError('与 QMT 桥的连接断开')
