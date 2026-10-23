# coding:utf-8
"""t0_live.py 自测：假行情 + 假下单通道（PaperBroker），在临时目录里跑，不连桥、不碰真账户、不写共享数据目录。

    .venv313\\Scripts\\python.exe tests\\t0_live自测.py

只在 .venv313 跑（t0_live 用了 3.9+ 写法）。改了 t0_live.py 就跑一遍，要看到 FAILURES: 0。

覆盖：
  * 2026-10-21 新加的盘口/滑点记录：发单时的最新价、买一卖一、挂单量写进 orders.csv，滑点按盘口中间价算、正数 = 吃亏；
    旧表头的 orders.csv 自动升级、旧行保留；旧状态文件里没有盘口的单不报错；坏 tick 不覆盖盘口。
  * 2026-10-22：600366 最多 3 轮，净赚 > 0 开第 2 轮、> 100 元开第 3 轮，亏了不再开，加的额度用上了不收回。
  * 原有的安全限制：每只每天最多 2 笔、STOP 文件挡开仓、对手价偏离最新价 >1% 不发、ALLOW_ORDER 关着时额度退回并全部停、
    重启后委托计数不丢、坏 tick（成交额放大 100 倍）不触发开仓；有未平仓位时坏 tick 照常止损 / 回均价止盈。
  * 2026-10-23 加固（ScriptBroker 按脚本给部分成交 / 废单 / 一直挂着，还能让查询返回空或报错）：
    平仓部分成交、废单后补单，不占开仓额度，每轮最多 3 笔，用完停掉这只；成交额为 0 照常止损；重启后 last_vwap 不丢；
    知道委托号却查不到 / 查询报错超过 LOST_WAIT 停掉这只；CSV 写失败不影响交易；运行锁；认单兜底核对价格和策略名；
    过期 / 倒序行情不用；平仓放宽价格偏离到 3%；封板挂不出去只告警不停。
"""
import csv
import datetime as dt
import json
import os
import shutil
import socket
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import t0_sim  # noqa: E402
import t0_live  # noqa: E402
from qmt_bridge import xtdata  # noqa: E402

FAILS = []
CODE = '600366.SH'
DAY = '20261021'


def check(name, cond, detail=''):
    print('%s %s%s' % ('ok  ' if cond else 'FAIL', name, '' if cond else '  <- %s' % detail))
    if not cond:
        FAILS.append(name)


def setup():
    tmp = tempfile.mkdtemp(prefix='t0live_test_')
    t0_sim.SIM_DIR = tmp                                    # 参数、状态、委托记录全落到临时目录
    xtdata.get_market_data_ex = lambda *a, **k: {}          # 带宽取不到 -> 用 band_min
    xtdata.get_instrument_detail = lambda code: {'InstrumentName': '测试'}
    return tmp


def tick(eng, hhmmss, price, vwap, bid1, ask1, bv=500, av=300, bad=False):
    vol = 100000
    amount = vwap * vol * 100 * (100 if bad else 1)        # volume 是手；bad = 成交额放大 100 倍的坏 tick
    eng.feed(CODE, DAY + hhmmss, price, bid1, ask1, amount, vol, bv, av)


def read_orders(eng):
    path = os.path.join(eng.dir, 'orders.csv')
    with open(path, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def new_engine(broker=None):
    return t0_live.Engine('dry', DAY, broker or t0_live.PaperBroker())


class ScriptBroker(t0_live.PaperBroker):
    """按脚本决定每笔委托的结果：script 里依次是 (状态, 成交量)，用完了就全成。
    get_mode = 'none' 让查委托返回空，'raise' 让查委托报错。"""

    def __init__(self, script=()):
        t0_live.PaperBroker.__init__(self)
        self.script = list(script)
        self.get_mode = None

    def submit(self, code, side, qty, price, marker):
        t0_live.PaperBroker.submit(self, code, side, qty, price, marker)
        if self.script:
            o = self.orders[marker]
            o.order_status, o.traded_volume = self.script.pop(0)

    def get(self, sysid):
        if self.get_mode == 'none':
            return None
        if self.get_mode == 'raise':
            raise RuntimeError('查询超时')
        return t0_live.PaperBroker.get(self, sysid)


class FakeOrder(object):
    def __init__(self, sysid, code, otype, vol, price, remark='', strategy=''):
        self.order_sysid, self.stock_code, self.order_type = sysid, code, otype
        self.order_volume, self.price, self.order_remark, self.strategy_name = vol, price, remark, strategy


class FakeTrader(object):
    def __init__(self, orders):
        self.orders = orders

    def query_stock_orders(self, acc):
        return self.orders


def real_broker_with(orders):
    b = t0_live.RealBroker.__new__(t0_live.RealBroker)        # 不走 __init__，不连桥
    b.trader, b.acc = FakeTrader(orders), None
    return b


def raw_tick(eng, hhmmss, price, bid1, ask1, amount, vol=100000):
    eng.feed(CODE, DAY + hhmmss, price, bid1, ask1, amount, vol, 500, 300)


def main():
    tmp = setup()
    try:
        # ---- 旧表头的 orders.csv（10-19 那份的格式）
        live_dir = os.path.join(tmp, 'live_dry')
        os.makedirs(live_dir)
        old_cols = t0_live.ORDER_COLS[:14]
        with open(os.path.join(live_dir, 'orders.csv'), 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(old_cols)
            w.writerow(['20261019', 'live', CODE, '宁波韵升', '开', '卖', '800', '9.86', '13:52:30', '635099982',
                        '已成', '800', '9.86', '正T 先卖'])

        eng = new_engine()
        st = eng.stocks[CODE]
        band = st.band

        # ---- 坏 tick 不触发、不覆盖盘口
        tick(eng, '100000', 10.00, 9.90, 9.99, 10.00, bad=True)
        check('坏 tick 不开仓', len(st.orders) == 0, st.orders)
        check('坏 tick 不写盘口', st.book == {}, st.book)

        # ---- 对手价偏离最新价 >1% 不发
        tick(eng, '100003', 10.00, 9.90, 9.85, 10.00)
        check('对手价偏离 >1% 不发', len(st.orders) == 0, st.orders)

        # ---- 正常开仓：先卖在买一
        tick(eng, '100006', 10.00, 9.90, 9.99, 10.00, bv=420, av=180)
        check('偏离超过带宽 %.2f%% 开仓' % (band * 100), len(st.orders) == 1 and st.leg is not None, st.orders)
        rows = read_orders(eng)
        check('旧表头自动升级', list(rows[0].keys()) == t0_live.ORDER_COLS, list(rows[0].keys()))
        check('旧行保留', rows[0]['委托号'] == '635099982' and rows[0]['滑点_元每股'] == '', rows[0])
        r = rows[-1]
        check('开仓行记了盘口', r['最新价'] == '10.0' and r['买一'] == '9.99' and r['卖一'] == '10.0'
              and r['买一量'] == '420' and r['卖一量'] == '180' and r['行情时间'] == '10:00:06', r)
        check('卖出滑点 = 中间价 - 成交价 = 0.005', r['中间价'] == '9.995' and abs(float(r['滑点_元每股']) - 0.005) < 1e-9, r)
        check('卖出相对最新价 = 0.01', abs(float(r['相对最新价_元每股']) - 0.01) < 1e-9, r)
        check('成交用时有值', r['成交用时秒'] != '', r)

        # ---- 回到 VWAP 买回：买在卖一
        tick(eng, '100100', 9.90, 9.90, 9.90, 9.91, bv=None, av=None)
        rows = read_orders(eng)
        r = rows[-1]
        check('平仓', st.leg is None and len(st.orders) == 2, (st.leg, st.orders))
        check('买入滑点 = 成交价 - 中间价 = 0.005', abs(float(r['滑点_元每股']) - 0.005) < 1e-9, r)
        check('挂单量缺失写空不报错', r['买一量'] == '' and r['卖一量'] == '', r)
        with open(os.path.join(eng.dir, 'rounds.csv'), encoding='utf-8-sig') as f:
            rd = list(csv.DictReader(f))[-1]
        check('一轮毛利 = (9.99-9.91)×800 = 64', abs(float(rd['毛利']) - 64) < 1e-6, rd)

        # ---- 额度：赚了钱的 600366 再开一轮；第二轮止损、净利转负后不再开
        check('第一轮净赚', st.realized > 0, st.realized)
        tick(eng, '100200', 10.00, 9.90, 9.99, 10.00)
        check('赚了再开第二轮（第 3 笔）', len(st.orders) == 3 and st.leg is not None, st.orders)
        tick(eng, '100300', 10.06, 9.90, 10.05, 10.06)     # 比卖出价 9.99 涨过带宽 -> 止损
        check('第二轮止损平掉（第 4 笔）', len(st.orders) == 4 and st.leg is None, (st.leg, st.orders))
        check('第二轮亏完当天转负', st.realized < 0, st.realized)
        tick(eng, '100400', 10.20, 10.10, 10.19, 10.20)
        check('额度用完不再发', len(st.orders) == 4, st.orders)
        check('额度：亏了不加', t0_live.quota(CODE, 2, -1.0) == 2)
        check('额度：赚了加第 2 轮', t0_live.quota(CODE, 2, 50.0) == 4)
        check('额度：赚不到 100 不开第 3 轮', t0_live.quota(CODE, 4, 50.0) == 4)
        check('额度：赚过 100 开第 3 轮', t0_live.quota(CODE, 4, 117.0) == 6)
        check('额度：最多 3 轮', t0_live.quota(CODE, 6, 500.0) == 6)
        check('额度：第 2 轮开了就不收回', t0_live.quota(CODE, 3, -20.0) == 4)
        check('额度：第 3 轮开了就不收回', t0_live.quota(CODE, 5, -20.0) == 6)
        check('额度：不在名单的不加', t0_live.quota('605208.SH', 2, 50.0) == 2)

        # ---- 状态文件带盘口、能重启接上
        eng.save()
        with open(eng.state_path, encoding='utf-8') as f:
            saved = json.load(f)
        check('状态文件存了盘口', saved[CODE]['orders'][0]['mkt'].get('bid1') == 9.99, saved[CODE]['orders'][0])
        eng.logf.close()
        eng2 = new_engine()
        check('重启后计数不丢', len(eng2.stocks[CODE].orders) == 4, eng2.stocks[CODE].orders)
        eng2.logf.close()

        # ---- 旧状态里没有盘口的单
        old_rec = {'side': 'buy', 'filled': 800, 'avg': 9.88, 'ts': '20261019135833'}
        cols = t0_live.slippage_cols(old_rec)
        check('旧单没有盘口不报错、滑点留空', cols['滑点_元每股'] == '' and cols['中间价'] == '', cols)
        check('没成交不算滑点', t0_live.slippage_cols({'side': 'sell', 'filled': 0, 'avg': 0,
                                                     'mkt': {'bid1': 9.9, 'ask1': 9.91, 'last': 9.9}})['滑点_元每股'] == '')

        # ---- STOP 文件挡开仓（换一天，额度是新的）
        global DAY
        DAY = '20261022'
        open(os.path.join(live_dir, 'STOP'), 'w').close()
        eng3 = new_engine()
        tick(eng3, '100006', 10.00, 9.90, 9.99, 10.00)
        check('STOP 挡开仓', len(eng3.stocks[CODE].orders) == 0, eng3.stocks[CODE].orders)
        eng3.logf.close()
        os.remove(os.path.join(live_dir, 'STOP'))

        # ---- ALLOW_ORDER 关着：额度退回、全部停
        DAY = '20261023'

        class OffBroker(t0_live.PaperBroker):
            def submit(self, code, side, qty, price, marker):
                raise t0_live.AllowOrderOff('ALLOW_ORDER is False')
        eng4 = new_engine(OffBroker())
        tick(eng4, '100006', 10.00, 9.90, 9.99, 10.00)
        s4 = eng4.stocks[CODE]
        check('ALLOW_ORDER 关：额度退回', len(s4.orders) == 0 and s4.pending is None, s4.orders)
        check('ALLOW_ORDER 关：全部停', bool(eng4.halted), eng4.halted)
        eng4.logf.close()

        # ---- 三轮都赚：第 1 轮赚够 100 后第 2、3 轮依次开，第 4 轮不开
        DAY = '20261024'
        eng5 = new_engine()
        s5 = eng5.stocks[CODE]
        for i, (t_open, t_close) in enumerate([('100006', '100100'), ('100200', '100300'), ('100400', '100500')]):
            tick(eng5, t_open, 10.00, 9.80, 9.99, 10.00)       # 偏离 +2% 开
            tick(eng5, t_close, 9.80, 9.80, 9.79, 9.80)        # 回均价，每轮毛利 (9.99-9.80)×800 = 152
            check('第 %d 轮做完' % (i + 1), len(s5.orders) == 2 * (i + 1) and s5.leg is None, s5.orders)
        check('三轮累计净赚', s5.realized > 300, s5.realized)
        tick(eng5, '100600', 10.00, 9.80, 9.99, 10.00)
        check('最多 3 轮', len(s5.orders) == 6, s5.orders)
        eng5.logf.close()

        # ---- 有未平仓位时坏 tick 照常止损（10-23 实测坏 tick 断断续续持续近一小时）
        DAY = '20261027'
        eng6 = new_engine()
        s6 = eng6.stocks[CODE]
        tick(eng6, '100006', 10.00, 9.90, 9.99, 10.00)
        check('坏 tick 止损：先开仓', s6.leg is not None and len(s6.orders) == 1, s6.orders)
        tick(eng6, '100100', 10.10, 9.90, 10.09, 10.10, bad=True)     # 涨过卖出价 + 带宽，只有坏 tick
        check('坏 tick 也照常止损', s6.leg is None and len(s6.orders) == 2 and s6.orders[-1]['why'] == '止损',
              (s6.leg, s6.orders))
        check('坏 tick 止损时盘口是这一笔的', s6.orders[-1]['mkt'].get('ask1') == 10.10, s6.orders[-1])
        eng6.logf.close()

        # ---- 有未平仓位时坏 tick 按上一笔 VWAP 回均价止盈；平了之后坏 tick 不开新一轮
        DAY = '20261028'
        eng7 = new_engine()
        s7 = eng7.stocks[CODE]
        tick(eng7, '100006', 10.00, 9.80, 9.99, 10.00)
        tick(eng7, '100100', 9.80, 9.80, 9.79, 9.80, bad=True)
        check('坏 tick 按上一笔 VWAP 止盈', s7.leg is None and len(s7.orders) == 2
              and s7.orders[-1]['why'] == '回均价止盈', (s7.leg, s7.orders))
        check('止盈后还有额度', s7.left >= 2, s7.left)
        tick(eng7, '100200', 10.00, 9.80, 9.99, 10.00, bad=True)
        check('没仓位时坏 tick 不开新一轮', len(s7.orders) == 2, s7.orders)
        tick(eng7, '100300', 10.00, 9.80, 9.99, 10.00)
        check('正常 tick 照常开第二轮', len(s7.orders) == 3, s7.orders)
        eng7.logf.close()

        # ==== 2026-10-23 加固 ====
        # ---- 平仓部分成交：剩下的接着补单，不占开仓额度
        DAY = '20261029'
        b = ScriptBroker([(56, 800), (53, 200)])           # 开仓全成；第一笔平仓只成 200 就撤了
        e = new_engine(b)
        s = e.stocks[CODE]
        tick(e, '100006', 10.00, 9.90, 9.99, 10.00)
        tick(e, '100100', 10.10, 9.90, 10.09, 10.10)         # 止损，只成 200
        check('平仓部分成交：还剩 600', s.leg is not None and s.leg['qty'] == 600 and s.leg['closes'] == 1, s.leg)
        check('部分成交那 200 股记了盈亏', s.realized < 0, s.realized)
        tick(e, '100103', 10.10, 9.90, 10.09, 10.10)         # 还在止损线上 -> 补单（额度已经 2/2 了）
        check('额度用完也能补平仓单', s.leg is None and len(s.orders) == 3, (s.leg, s.orders))
        check('补的那笔是剩下的 600', s.orders[-1]['qty'] == 600, s.orders[-1])
        tick(e, '100200', 10.00, 9.80, 9.99, 10.00)
        check('补单不会多出开仓额度', len(s.orders) == 3, s.orders)
        e.logf.close()

        # ---- 平仓连续废单：补到 3 笔还没平就停掉这只
        DAY = '20261030'
        b = ScriptBroker([(56, 800), (57, 0), (57, 0), (57, 0)])
        e = new_engine(b)
        s = e.stocks[CODE]
        tick(e, '100006', 10.00, 9.90, 9.99, 10.00)
        for i, t in enumerate(['100100', '100103', '100106']):
            tick(e, t, 10.10, 9.90, 10.09, 10.10)
        check('废单 3 笔都补发了', len(s.orders) == 4 and s.leg['closes'] == 3 and not s.halted, (s.orders, s.halted))
        tick(e, '100109', 10.10, 9.90, 10.09, 10.10)
        check('平仓 3 笔用完停掉这只、留着仓位', bool(s.halted) and '手动处理' in s.halted and s.leg['qty'] == 800,
              (s.halted, s.leg))
        check('停了不再发', len(s.orders) == 4, s.orders)
        e.logf.close()

        # ---- 成交额为 0 的 tick：有仓位照常止损
        DAY = '20261031'
        e = new_engine()
        s = e.stocks[CODE]
        tick(e, '100006', 10.00, 9.90, 9.99, 10.00)
        raw_tick(e, '100100', 10.10, 10.09, 10.10, 0, 0)
        check('成交额 0 也照常止损', s.leg is None and s.orders[-1]['why'] == '止损', (s.leg, s.orders))
        e.logf.close()

        # ---- 重启后 last_vwap 不丢：坏 tick 照常按它止盈；没有 VWAP 时止损照样判断
        DAY = '20261101'
        e = new_engine()
        tick(e, '100006', 10.00, 9.90, 9.99, 10.00)
        e.save()
        e.logf.close()
        e2 = new_engine()
        s2 = e2.stocks[CODE]
        check('重启后接上仓位和 last_vwap', s2.leg is not None and abs((s2.last_vwap or 0) - 9.90) < 1e-6,
              (s2.leg, s2.last_vwap))
        tick(e2, '100100', 9.90, 9.90, 9.89, 9.90, bad=True)
        check('重启后坏 tick 按存下的 VWAP 止盈', s2.leg is None and s2.orders[-1]['why'] == '回均价止盈', s2.orders)
        e2.logf.close()
        DAY = '20261102'
        e = new_engine()
        tick(e, '100006', 10.00, 9.90, 9.99, 10.00)
        s = e.stocks[CODE]
        s.last_vwap = None                                  # 模拟旧状态文件里没有 last_vwap
        tick(e, '100100', 10.10, 9.90, 10.09, 10.10, bad=True)
        check('没有 VWAP 时坏 tick 也照常止损', s.leg is None and s.orders[-1]['why'] == '止损', s.orders)
        e.logf.close()

        # ---- 知道委托号却查不到 / 查询报错：超过 LOST_WAIT 停掉这只
        for mode in ('none', 'raise'):
            DAY = '2026110' + ('3' if mode == 'none' else '4')
            b = ScriptBroker([(50, 0)])                    # 开仓单一直挂着（已报）
            e = new_engine(b)
            s = e.stocks[CODE]
            tick(e, '100006', 10.00, 9.90, 9.99, 10.00)
            check('[%s] 开仓单挂着' % mode, s.pending is not None and s.orders[0]['sysid'], s.pending)
            b.get_mode = mode
            s.poll()
            check('[%s] 查不到先开始计时' % mode, s.pending and s.pending['lost_at'] is not None and not s.halted,
                  (s.pending, s.halted))
            s.pending['lost_at'] = time.time() - t0_live.LOST_WAIT - 1
            s.poll()
            check('[%s] 超过 LOST_WAIT 停掉这只' % mode, s.pending is None and bool(s.halted)
                  and s.orders[0]['status'] == 'lost', (s.halted, s.orders[0]['status']))
            e.logf.close()

        # ---- CSV 写失败（Excel 锁着）：交易照常，状态先存好
        DAY = '20261105'
        real_append = t0_live.append_csv

        def locked(*a, **k):
            raise PermissionError(13, 'Permission denied: orders.csv')
        t0_live.append_csv = locked
        try:
            e = new_engine()
            s = e.stocks[CODE]
            tick(e, '100006', 10.00, 9.90, 9.99, 10.00)
            tick(e, '100100', 9.90, 9.90, 9.89, 9.90)
            check('CSV 写不进去照常开平', s.leg is None and len(s.orders) == 2 and s.realized > 0, (s.leg, s.orders))
            with open(e.state_path, encoding='utf-8') as f:
                st_saved = json.load(f)[CODE]
            check('CSV 失败时状态文件已经存好', st_saved['leg'] is None and len(st_saved['orders']) == 2, st_saved)
            e.logf.close()
        finally:
            t0_live.append_csv = real_append

        # ---- 运行锁
        DAY = '20261106'
        e = new_engine()
        check('第一个拿到锁', e.acquire_lock())
        e_b = new_engine()
        check('同一台机器、进程还在：第二个拿不到', not e_b.acquire_lock())
        e.release_lock()
        lock = os.path.join(e.dir, 'live.lock')
        with open(lock, 'w', encoding='utf-8') as f:
            json.dump({'host': socket.gethostname(), 'pid': 99999999, 'day': DAY}, f)
        check('进程已经不在的旧锁可以接管', e_b.acquire_lock())
        e_b.release_lock()
        with open(lock, 'w', encoding='utf-8') as f:
            json.dump({'host': 'OTHER-PC', 'pid': 1234, 'day': DAY}, f)
        check('另一台机器今天的锁：拿不到', not e_b.acquire_lock())
        e_b.force_lock = True
        check('另一台机器的锁：--force 接管', e_b.acquire_lock())
        e_b.release_lock()
        with open(lock, 'w', encoding='utf-8') as f:
            json.dump({'host': 'OTHER-PC', 'pid': 1234, 'day': '20200101'}, f)
        e_b.force_lock = False
        check('别的日子的锁算失效', e_b.acquire_lock())
        e_b.release_lock()
        check('pid_alive 认得自己', t0_live.pid_alive(os.getpid()) and not t0_live.pid_alive(99999999))
        e.logf.close()
        e_b.logf.close()

        # ---- 认单兜底：备注对不上时要价格、策略名都对得上，且只有一笔
        SELL = t0_live.STOCK_SELL
        rb = real_broker_with([FakeOrder('1', CODE, SELL, 800, 9.99, remark='MINE')])
        check('认单：备注对上直接认', rb.find(CODE, 'sell', 800, 'MINE', set(), 9.99) is not None)
        rb = real_broker_with([FakeOrder('2', CODE, SELL, 800, 10.20)])
        check('认单：价格不同不认', rb.find(CODE, 'sell', 800, 'MINE', set(), 9.99) is None)
        rb = real_broker_with([FakeOrder('3', CODE, SELL, 800, 9.99)])
        check('认单：代码方向数量价格都对、只有一笔才认', rb.find(CODE, 'sell', 800, 'MINE', set(), 9.99) is not None)
        rb = real_broker_with([FakeOrder('4', CODE, SELL, 800, 9.99, strategy='手动')])
        check('认单：策略名是别人的不认', rb.find(CODE, 'sell', 800, 'MINE', set(), 9.99) is None)
        rb = real_broker_with([FakeOrder('5', CODE, SELL, 800, 9.99), FakeOrder('6', CODE, SELL, 800, 9.99)])
        check('认单：对得上的有两笔不认', rb.find(CODE, 'sell', 800, 'MINE', set(), 9.99) is None)
        rb = real_broker_with([FakeOrder('7', CODE, SELL, 800, 9.99)])
        check('认单：发单前就有的不认', rb.find(CODE, 'sell', 800, 'MINE', {'7'}, 9.99) is None)

        # ---- 过期 / 倒序行情
        DAY = '20261107'
        e = new_engine()
        s = e.stocks[CODE]
        now = dt.datetime.strptime(DAY + '100100', '%Y%m%d%H%M%S')
        check('新行情能用', e.fresh_enough(CODE, DAY + '100055', now))
        check('落后 %.0f 秒以上的不用' % t0_live.STALE_MAX, not e.fresh_enough(CODE, DAY + '100000', now))
        s.last = (DAY + '100058', 10.0, 9.99, 10.0)
        check('比上一笔还旧的不用', not e.fresh_enough(CODE, DAY + '100055', now))
        e.logf.close()

        # ---- 平仓放宽价格偏离到 3%；封板（对手价 0）挂不出去只告警不停
        DAY = '20261108'
        e = new_engine()
        s = e.stocks[CODE]
        tick(e, '100006', 10.00, 9.90, 9.99, 10.00)
        tick(e, '100100', 10.10, 9.90, 10.09, 0)            # 卖一没了（涨停封板）
        check('封板挂不出去：不发、不停、仓位留着', len(s.orders) == 1 and not s.halted and s.leg is not None,
              (s.orders, s.halted))
        tick(e, '100103', 10.10, 9.90, 10.09, 10.30)        # 卖一离最新价 2%：开仓会挡，平仓要发
        check('平仓对手价偏离 2% 照样发', s.leg is None and len(s.orders) == 2, (s.leg, s.orders))
        e.logf.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\nFAILURES: %d' % len(FAILS))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
