# coding:utf-8
"""桥接压测：专挑上了盘才会咬人的失效路径。

    .venv\\Scripts\\python.exe -m qmt_bridge.桥接压测

跟 `桥接自测` 分开放：那个是快速冒烟（几秒），这个跑十几秒，改了桥的线程模型、
队列策略、连接生命周期之后再跑。
"""
import importlib.util
import os
import socket
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

from qmt_bridge import xtdata
xtdata.enable_hello = False

PORT = 58699                       # 跟默认端口错开，免得压测撞上真在跑的桥
failures = []


def check(name, cond, detail=''):
    print(('  OK   ' if cond else '  FAIL ') + name + ('' if cond else '  <- ' + str(detail)))
    if not cond:
        failures.append(name)


class Ctx(object):
    """可编程的假 ContextInfo：想让哪个接口慢、报错、返回坏值都能摆出来。"""

    def __init__(self):
        self.sub_id = 0
        self.unsubbed = []
        self.stop = threading.Event()
        self.sub_returns = None        # 设成 -1 可模拟订阅失败
        self.md_delay = 0.0            # get_market_data_ex 卡多久
        self.md_raise = False
        self.md_rows = 3
        self.pumps = []

    def _make_sub(self, pump):
        if self.sub_returns is not None:
            return self.sub_returns
        self.sub_id += 1
        t = threading.Thread(target=pump, daemon=True)
        t.start()
        self.pumps.append(t)
        return self.sub_id

    def subscribe_quote(self, code, period, dividend_type, result_type, callback):
        stat = {'calls': 0, 'max_block': 0.0}

        def pump():
            while not self.stop.is_set():
                t0 = time.time()
                callback({code: {'time': [int(t0 * 1000)], 'lastPrice': [1.0],
                                 'pad': ['x' * 200]}})
                stat['max_block'] = max(stat['max_block'], time.time() - t0)
                stat['calls'] += 1
        self.last_stat = stat
        return self._make_sub(pump)

    def subscribe_whole_quote(self, code_list, callback):
        def pump():
            while not self.stop.is_set():
                callback({'000001.SZ': {'time': 1, 'lastPrice': 2.0}})
                time.sleep(0.01)
        return self._make_sub(pump)

    def unsubscribe_quote(self, sub):
        self.unsubbed.append(sub)
        return True

    def get_full_tick(self, code_list):
        return dict((c, {'lastPrice': 1.0}) for c in code_list)

    def get_market_data_ex(self, fields, stocks, period, start, end, count,
                           dividend_type, fill_data, subscribe):
        if self.md_raise:
            raise ValueError('假装 ContextInfo 炸了')
        if self.md_delay:
            time.sleep(self.md_delay)
        idx = ['%08d' % i for i in range(self.md_rows)]
        return dict((s, pd.DataFrame({'close': [float(i) for i in range(self.md_rows)]},
                                     index=idx)) for s in stocks)

    def get_instrument_detail(self, code):
        return {'InstrumentID': code}


def new_server(ctx, port=PORT):
    s = bridge_server.BridgeServer(ctx, '127.0.0.1', port)
    s.start()
    time.sleep(0.2)
    return s


# ---------------------------------------------------------------- 1 背压

print('1) 背压：客户端不读时，行情回调线程不能被拖住')
ctx = Ctx()
server = new_server(ctx)
dead = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
dead.connect(('127.0.0.1', PORT))
dead.sendall(b'{"id":1,"func":"subscribe_quote","kwargs":{"seq":1,"stock_code":"000001.SZ","period":"tick"}}\n')
time.sleep(3.0)                     # 只发不收，让内核缓冲和队列都撑满
stat = ctx.last_stat
conn = server.conns[0] if server.conns else None
check('回调仍在持续被调用', stat['calls'] > 1000, stat['calls'])
check('单次回调从未阻塞超过 50ms', stat['max_block'] < 0.05, '%.3fs' % stat['max_block'])
check('服务端确实在丢包而不是堆积', conn is not None and conn.dropped > 0,
      conn.dropped if conn else 'no conn')
check('队列没有超过上限', conn is None or conn.out.qsize() <= bridge_server.MAX_BACKLOG,
      conn.out.qsize() if conn else '-')
ctx.stop.set()
dead.close()
time.sleep(0.3)
server.stop()
time.sleep(0.3)

# ---------------------------------------------------------------- 2 多客户端

print('2) 多客户端：推送不串台，一个断开不影响另一个')
ctx = Ctx()
server = new_server(ctx)
got_a, got_b = [], []
a = xtdata.connect('127.0.0.1', PORT)
xtdata.subscribe_whole_quote(['SH'], callback=lambda d: got_a.append(d))
time.sleep(0.3)
xtdata._bridge = None                       # 再开一条独立连接
b = xtdata.connect('127.0.0.1', PORT)
xtdata.subscribe_whole_quote(['SZ'], callback=lambda d: got_b.append(d))
time.sleep(0.5)
check('两条连接都在收', len(got_a) > 3 and len(got_b) > 3, (len(got_a), len(got_b)))
check('服务端记着两条连接', len(server.conns) == 2, len(server.conns))
a.close()                                    # 掐掉第一条
time.sleep(0.5)
n_b = len(got_b)
time.sleep(0.5)
check('掐掉一条后另一条照常收', len(got_b) > n_b, (n_b, len(got_b)))
check('断开的那条被退订', len(ctx.unsubbed) == 1, ctx.unsubbed)
check('服务端只剩一条连接', len(server.conns) == 1, len(server.conns))
ctx.stop.set()
xtdata.disconnect()
time.sleep(0.3)
server.stop()
time.sleep(0.3)

# ---------------------------------------------------------------- 3 重启

print('3) 重跑脚本：旧实例被关掉，端口能重新绑上')
ctx = Ctx()
bridge_server.BRIDGE_PORT = PORT
bridge_server.init(ctx)
first = bridge_server._registry().get('server')
check('第一次 init 起来了', first is not None and first.running)
xtdata._bridge = None
xtdata.connect('127.0.0.1', PORT)
check('能连上第一个实例', xtdata.get_client().call('ping') == 'pong')
bridge_server.init(ctx)                      # 模拟在编辑器里又点了一次「运行」
second = bridge_server._registry().get('server')
check('第二次 init 也起来了（端口没被占死）', second is not None and second.running)
check('换成了新实例', second is not first)
check('旧实例已停', not first.running)
xtdata._bridge = None
xtdata.connect('127.0.0.1', PORT)
check('能连上新实例', xtdata.get_client().call('ping') == 'pong')
xtdata.disconnect()
ctx.stop.set()
second.stop()
time.sleep(0.3)

# ---------------------------------------------------------------- 4 并发请求

print('4) 并发请求：响应不能串号')
ctx = Ctx()
server = new_server(ctx)
xtdata._bridge = None
xtdata.connect('127.0.0.1', PORT)
results = {}
errors = []


def worker(i):
    try:
        r = xtdata.get_market_data_ex(['close'], ['CODE%02d' % i], count=3)
        results[i] = list(r.keys())
    except Exception as e:
        errors.append((i, e))


threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
for t in threads:
    t.start()
for t in threads:
    t.join(30)
check('20 个并发请求全部返回', len(results) == 20, (len(results), errors[:2]))
check('每个请求拿到的是自己的结果',
      all(results.get(i) == ['CODE%02d' % i] for i in range(20)),
      [(i, results.get(i)) for i in range(20) if results.get(i) != ['CODE%02d' % i]][:3])

# ---------------------------------------------------------------- 5 大 payload

print('5) 大 payload：分片重组')
ctx.md_rows = 20000
big = xtdata.get_market_data_ex(['close'], ['600000.SH'], count=20000)
df = big['600000.SH']
check('两万行完整送达', df.shape == (20000, 1), df.shape)
check('首尾值正确', float(df['close'].iloc[0]) == 0.0 and float(df['close'].iloc[-1]) == 19999.0,
      (df['close'].iloc[0], df['close'].iloc[-1]))
ctx.md_rows = 3

# ---------------------------------------------------------------- 6 异常与超时

print('6) 异常与超时')
ctx.md_raise = True
try:
    xtdata.get_market_data_ex(['close'], ['600000.SH'])
    check('ContextInfo 抛异常要回传', False)
except Exception as e:
    check('ContextInfo 抛异常要回传', 'ValueError' in str(e) and '假装' in str(e), e)
ctx.md_raise = False
check('服务端没被那个异常弄死', xtdata.get_client().call('ping') == 'pong')

ctx.md_delay = 3.0
t0 = time.time()
try:
    xtdata.get_client().call('get_market_data_ex', timeout=1.0,
                             field_list=['close'], stock_list=['600000.SH'])
    check('卡住的请求要超时返回', False)
except Exception as e:
    check('卡住的请求要超时返回', '超时' in str(e), e)
check('超时是按设定值返回的，没一直挂着', time.time() - t0 < 2.0, '%.1fs' % (time.time() - t0))
ctx.md_delay = 0.0
time.sleep(3.2)                      # 等那个慢请求自己走完，免得污染后面
check('超时之后连接还能用', xtdata.get_client().call('ping') == 'pong')

print('7) 回调自己抛异常：线程不能死，日志不能刷屏')
import contextlib
import io as _io

boom = []


def bad_cb(d):
    boom.append(1)
    raise RuntimeError('回调里故意炸一下')


err = _io.StringIO()
xtdata.get_client().cb_errors = 0
with contextlib.redirect_stderr(err):
    xtdata.subscribe_whole_quote(['SH'], callback=bad_cb)
    time.sleep(1.0)
printed = err.getvalue().count('Traceback (most recent call last)')
check('回调被调了很多次（线程没死）', len(boom) > 20, len(boom))
check('错误计数跟上了', xtdata.get_client().cb_errors >= len(boom) - 2,
      (xtdata.get_client().cb_errors, len(boom)))
check('完整堆栈最多只打 3 次', printed <= 3, printed)
check('炸完之后请求照常', xtdata.get_client().call('ping') == 'pong')

print('8) 订阅失败要报错，且不留下野回调')
ctx.sub_returns = -1
n_before = len(xtdata.get_client().callbacks)
try:
    xtdata.subscribe_quote('000001.SZ', period='tick', callback=lambda d: None)
    check('订阅失败要抛异常', False)
except Exception as e:
    check('订阅失败要抛异常', 'subscribe_quote' in str(e), e)
check('失败的订阅没在客户端留回调',
      len(xtdata.get_client().callbacks) == n_before,
      (n_before, len(xtdata.get_client().callbacks)))
ctx.sub_returns = None

ctx.stop.set()
xtdata.disconnect()
time.sleep(0.3)
server.stop()

print()
print('FAILURES: %d' % len(failures))
if failures:
    for f in failures:
        print('  -', f)
sys.exit(1 if failures else 0)
