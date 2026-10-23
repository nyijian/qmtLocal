# coding:utf-8
"""持仓做 T 的实盘版：规则沿用 t0_sim.py，信号到了**真的下委托**（加 --live 才下，默认只演示）。

    .venv313\\Scripts\\python.exe tools\\t0_live.py                    # 演示：盯实时行情，打印会下什么单，不下单
    .venv313\\Scripts\\python.exe tools\\t0_live.py --live             # 实盘：真下单（桥接服务那边还要 ALLOW_ORDER=True）
    .venv313\\Scripts\\python.exe tools\\t0_live.py --replay 20261019  # 用当天逐笔回放，假成交，验证流程
    .venv313\\Scripts\\python.exe tools\\t0_live.py --status           # 看今天每只已下几笔、还剩几笔

要停：在共享数据目录 sim_t0/live/ 下建一个名为 STOP 的空文件，就不再开新的一轮；手上没平的那轮照常平，已挂的单照常撤/等。

硬限制（写死在下面 LIMITS / MAX_ORDERS / MAX_DAILY_LOSS，命令行改不了，要改就改代码）：
  * 只做 LIMITS 里的代码，单笔不超过对应股数，100 股整数倍。
  * 开新一轮：每只每天最多 MAX_ORDERS 笔委托，买卖方向不限；EXTRA_ROUNDS 里的代码当天已实现净利过了门槛再开一轮
    （600366 最多 3 轮：净赚 > 0 开第 2 轮，> 100 元开第 3 轮）。计数在**发单之前**写进状态文件，
    程序重启、崩溃都不会重置；没成交被撤掉的也算一笔。只有桥明确回「ALLOW_ORDER 是 False」
    （单根本没发出去）才不算。
  * 平仓不受上面的额度限制（2026-10-23 改）：平仓单部分成交、没成交被撤、废单，剩下的接着按止损 / 止盈条件补单，
    每轮最多 CLOSE_TRIES 笔平仓单；用完还没平完就停掉这只、报警，剩下的留给用户。
  * 止损只看开仓价、现价、带宽，不依赖 VWAP —— 成交额是坏的、是 0、重启后还没有正常 tick，都照常判断止损；
    回均价止盈要 VWAP，坏 tick 时用上一笔正常的（存在状态文件里，重启不丢）。
  * 同一只同一时间只挂一笔，没走完（成交/撤单）不发下一笔。委托状态查不清（找不到、查询一直报错）就停掉这只，不重发。
  * 当天已实现净亏超过 MAX_DAILY_LOSS 就不再开新的一轮（手上那轮照常平）。这只挡开仓，不是当天亏损上限的保证。
  * 卖出不超过可用持仓，买入不超过可用资金；限价 = 对手价（卖用买一、买用卖一），
    开仓偏离最新价超过 1% 不发、平仓放宽到 3%；对手价为 0（封板）不发，平仓碰上会报警。
  * 行情时间落后本机超过 STALE_MAX 秒、或者比上一笔还旧，不用；14:56:30 按本机时间截止。
  * 同一个记账目录同时只能跑一个进程（live.lock 记着机器名和进程号）；另一台机器的锁靠 OneDrive 同步，有几分钟延迟，
    只当兜底 —— 实盘只在一台机器上跑。
  * orders.csv / rounds.csv 写失败（比如被 Excel 打开锁住）只记日志，不影响交易；状态文件先存、报表后写。
  * 收盘**不强制还原**（2026-10-19 用户定的）：一轮只开了一半，剩下那半留给用户自己处理。

策略（参数读 sim_t0/params.json，跟模拟盘同一份）：
  * 只做正 T（ALLOW_BUY_FIRST = False）：价格 ≥ VWAP × (1 + 带宽) 先卖；回到 VWAP 买回止盈，
    比卖出价再涨 stop_k × 带宽 买回止损。
  * 开一轮要剩 2 笔额度。entry_start ~ entry_end 才开新的，14:56:30 后不再发任何单。

记账在共享数据目录 sim_t0/live/（演示记到 sim_t0/live_dry/，回放记到 sim_t0/live_replay/，互不干扰）：
  state_<日期>.json  当天每只的委托计数、未平的一轮、挂着的单 —— 重启靠它接上
  orders.csv         每笔委托一行（发单价、成交量、成交均价、状态；2026-10-21 起加发单那一刻的
                     最新价/买一/卖一/挂单量、相对盘口中间价的滑点、成交用时，旧表头会自动升级）
  rounds.csv         每平一次一行（毛利、费用、净利）
  log_<日期>.txt     流水

只在 .venv313 跑（用了 3.7+ 写法）。
"""
import argparse
import datetime as dt
import json
import os
import socket
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import t0_sim  # noqa: E402  共用参数、费用、CSV、时间格式
from t0_sim import append_csv, fee, hms, load_params  # noqa: E402

# ---------------------------------------------------------------- 硬限制

LIMITS = {'600366.SH': 800, '605208.SH': 600}   # 代码 -> 单笔最多股数（2026-10-19 用户定）
MAX_ORDERS = 2                  # 每只每天最多几笔委托（买卖不限）
# 赚了钱再开几轮：代码 -> 每多一轮要求的当天已实现净利门槛（元），一轮 2 笔。2026-10-22 用户定最多 3 轮：
# 第 2 轮要净赚 > 0；第 3 轮要净赚 > 100（≈ 800 股 × 10.3 × 带宽 0.97% + 费用 14，按止损价成交时一轮的亏损）。
# 只是大致让第 3 轮亏了当天不怎么转负，**不是保证**：止损成交有滑点、跳空时会亏得更多（10-23 605208 一轮 -128）。
EXTRA_ROUNDS = {'600366.SH': [0.0, 100.0]}
MAX_DAILY_LOSS = 300.0          # 当天已实现净亏到这个数就不再开新的一轮
ALLOW_BUY_FIRST = False         # 反 T 先关：模拟盘 5 次全止损
LAST_ORDER_TIME = '14:56:30'    # 这之后不再发任何单（14:57 进收盘集合竞价）
ORDER_WAIT = 15.0               # 挂单多少秒没成交完就撤
RESOLVE_WAIT = 10.0             # 发单后多少秒还在委托列表里找不到 -> 状态不明，停掉这只
PRICE_DEV_MAX = 0.01            # 开仓：限价偏离最新价超过这么多不发
CLOSE_DEV_MAX = 0.03            # 平仓放宽：急涨急跌时卖一买一离最新价远，止损恰恰最要发出去
VWAP_DEV_MAX = 0.10             # VWAP 偏离现价超过这么多 = 坏 tick（成交额坏了，价格和盘口是对的）
CLOSE_TRIES = 3                 # 每轮最多几笔平仓单（部分成交 / 没成交 / 废单后补单），不占开仓额度
LOST_WAIT = 30.0                # 知道委托号却查不到、或查询一直报错，持续这么久 -> 停掉这只
STALE_MAX = 30.0                # 行情时间落后本机超过这么多秒就不用（本机时钟要准）

FINAL = {54, 53, 56, 57}        # 已撤、部撤、已成、废单（xtconstant.ORDER_*）
STOCK_BUY, STOCK_SELL, FIX_PRICE = 23, 24, 11

ORDER_COLS = ['日期', '模式', '代码', '名称', '用途', '方向', '委托量', '委托价', '发单时间',
              '委托号', '状态', '成交量', '成交均价', '备注',
              # 2026-10-21 加：发单那一刻的盘口和滑点，用来算真实成交成本（旧记录这几列是空的）
              '行情时间', '最新价', '买一', '卖一', '买一量', '卖一量', '中间价',
              '滑点_元每股', '滑点%', '相对最新价_元每股', '成交用时秒']
ROUND_COLS = ['日期', '模式', '代码', '名称', '方向', '数量', '开仓时间', '开仓价', '平仓时间', '平仓价',
              '原因', '毛利', '费用', '净利', '带宽', '参数版本']


def slippage_cols(rec):
    """发单时的盘口 + 成交结果 -> 滑点列。滑点以盘口中间价为基准，正数 = 吃亏（卖低于中间价、买高于中间价）。
    旧状态文件里的单没有 mkt，这几列留空。"""
    m = rec.get('mkt') or {}
    out = {'行情时间': hms(m['ts']) if m.get('ts') else '', '最新价': m.get('last', ''), '买一': m.get('bid1', ''),
           '卖一': m.get('ask1', ''), '买一量': m.get('bid_vol1', ''), '卖一量': m.get('ask_vol1', ''),
           '中间价': '', '滑点_元每股': '', '滑点%': '', '相对最新价_元每股': '',
           '成交用时秒': rec.get('done_secs') if rec.get('done_secs') is not None else ''}
    b, a, last = m.get('bid1') or 0, m.get('ask1') or 0, m.get('last') or 0
    if b > 0 and a > 0:
        mid = (b + a) / 2
        out['中间价'] = round(mid, 4)
        if rec.get('filled'):
            sign = 1 if rec['side'] == 'buy' else -1
            slip = sign * (rec['avg'] - mid)
            out['滑点_元每股'] = round(slip, 4)
            out['滑点%'] = round(slip / mid * 100, 4)
            if last > 0:
                out['相对最新价_元每股'] = round(sign * (rec['avg'] - last), 4)
    return out


def upgrade_header(path, cols):
    """orders.csv 是旧表头（少了后加的列）就整份重写成新表头，旧行新列留空。"""
    import csv
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.reader(f))
    if not rows or rows[0] == cols:
        return
    old = rows[0]
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows[1:]:
            d = dict(zip(old, r))
            w.writerow([d.get(c, '') for c in cols])
    os.replace(tmp, path)


# ---------------------------------------------------------------- 下单通道

class AllowOrderOff(Exception):
    """桥明确说 ALLOW_ORDER 是 False：单没发出去。"""


class RealBroker(object):
    """走桥真下单。所有查询失败都往上抛，由调用方决定停不停。"""

    def __init__(self):
        from qmt_bridge.xttrader import XtQuantTrader
        from qmt_bridge.xttype import StockAccount
        acct = os.environ.get('QMT_ACCOUNT_ID', '')
        if not acct:
            sys.exit('.env 里没有 QMT_ACCOUNT_ID')
        self.trader = XtQuantTrader('', 0)
        self.trader.order_resolve_timeout = 0          # 不在下单里干等，靠后面轮询认单
        self.trader.start()
        self.trader.connect()
        self.acc = StockAccount(acct, os.environ.get('QMT_ACCOUNT_TYPE', 'STOCK'))

    def can_sell(self, code):
        pos = self.trader.query_stock_position(self.acc, code)
        return pos.can_use_volume if pos else 0

    def cash(self):
        a = self.trader.query_stock_asset(self.acc)
        return a.cash if a else 0.0

    def order_ids(self, code):
        return {o.order_sysid for o in self.trader.query_stock_orders(self.acc) if o.stock_code == code}

    def submit(self, code, side, qty, price, marker):
        try:
            self.trader.order_stock(self.acc, code, STOCK_SELL if side == 'sell' else STOCK_BUY, qty,
                                    FIX_PRICE, price, 't0_live', marker)
        except Exception as e:
            if 'ALLOW_ORDER' in str(e):
                raise AllowOrderOff(str(e))
            raise

    def find(self, code, side, qty, marker, before, price=None):
        """认单：先按备注；认不到再找发单后新冒出来的、代码方向数量**委托价**都对得上、策略名不是别人的那笔，
        而且只能有一笔。对不上就返回 None，由调用方超时停掉这只、报警 —— 宁可停，不认领别人的委托
        （认错了会按别人的单记账，甚至把它撤掉）。"""
        want = STOCK_SELL if side == 'sell' else STOCK_BUY
        cands = []
        for o in self.trader.query_stock_orders(self.acc):
            if o.order_remark == marker:
                return o
            if (o.stock_code == code and o.order_sysid and o.order_sysid not in before
                    and o.order_type == want and o.order_volume == qty
                    and (price is None or abs(o.price - price) < 0.005)
                    and getattr(o, 'strategy_name', '') in ('', 't0_live')):
                cands.append(o)
        return cands[0] if len(cands) == 1 else None

    def get(self, sysid):
        for o in self.trader.query_stock_orders(self.acc):
            if o.order_sysid == sysid:
                return o
        return None

    def cancel(self, sysid):
        return self.trader.cancel_order_stock_sysid(self.acc, sysid)


class _PaperOrder(object):
    def __init__(self, sysid, qty, price):
        self.order_sysid, self.order_status = sysid, 56
        self.traded_volume, self.traded_price, self.order_volume = qty, price, qty


class PaperBroker(object):
    """演示/回放用：限价立刻全成。持仓、资金从账户读一次（只读），读不到就当够用。"""

    def __init__(self, positions=None, cash=None):
        self.pos = dict(positions or {})
        self._cash = cash
        self.orders, self.seq = {}, 0

    def can_sell(self, code):
        return self.pos.get(code, 10 ** 9)

    def cash(self):
        return 1e12 if self._cash is None else self._cash

    def order_ids(self, code):
        return set(self.orders)

    def submit(self, code, side, qty, price, marker):
        self.seq += 1
        self.orders[marker] = _PaperOrder('P%d' % self.seq, qty, price)
        if code in self.pos:
            self.pos[code] += -qty if side == 'sell' else 0     # 当天买的 T+1 才能卖，可用不加
        if self._cash is not None:
            self._cash += qty * price if side == 'sell' else -qty * price

    def find(self, code, side, qty, marker, before, price=None):
        return self.orders.get(marker)

    def get(self, sysid):
        for o in self.orders.values():
            if o.order_sysid == sysid:
                return o
        return None

    def cancel(self, sysid):
        return 0


# ---------------------------------------------------------------- 单只股票

def pid_alive(pid):
    """进程还在不在。Windows 上不能用 os.kill(pid, 0) —— 那会真把进程杀掉。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == 'nt':
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259            # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def quota(code, n_orders, realized):
    """当天额度：MAX_ORDERS，这只当天已实现净利超过 EXTRA_ROUNDS 里的门槛就逐个加一轮（2 笔）。
    加的那一轮一旦开了就不收回，免得平到一半转亏、平仓单反而发不出去。"""
    q = MAX_ORDERS
    for need in EXTRA_ROUNDS.get(code, []):
        if not (realized > need or n_orders > q):
            break
        q += 2
    return q


class LiveT(object):

    def __init__(self, eng, code, name, cap, band):
        self.eng, self.code, self.name, self.cap, self.band = eng, code, name, cap, band
        self.orders = []          # 当天发过的每一笔（含失败的），len 就是用掉的额度
        self.leg = None           # {'side','qty','px','vwap','ts'} 开了没平完的一轮
        self.pending = None       # 挂着的单
        self.realized = 0.0
        self.banned = set()
        self.halted = ''          # 非空 = 这只停了，原因
        self.last = None
        self.book = {}            # 最近一笔通过检查的行情盘口，发单时抄进委托记录
        self.bad_ticks = 0
        self.last_vwap = None     # 最近一笔正常 tick 的 VWAP，坏 tick 时拿来做止盈判断（存状态文件，重启不丢）
        self.warned = {}          # 告警 -> 上次打的时刻，同一种告警一分钟打一次

    # -- 状态存取 --
    STATE_KEYS = ('orders', 'leg', 'pending', 'realized', 'halted', 'last_vwap')

    def dump(self):
        return {k: getattr(self, k) for k in self.STATE_KEYS} | {'banned': sorted(self.banned)}

    def load(self, d):
        for k in self.STATE_KEYS:
            if k in d:
                setattr(self, k, d[k])
        self.banned = set(d.get('banned', []))

    def warn(self, key, msg):
        if time.time() - self.warned.get(key, 0) >= 60:
            self.warned[key] = time.time()
            self.eng.log(msg)

    @property
    def quota(self):
        return quota(self.code, len(self.orders), self.realized)

    @property
    def left(self):
        return self.quota - len(self.orders)

    # -- 行情驱动 --
    def on_tick(self, ts, price, bid1, ask1, v, fresh=True):
        """v = 这笔的 VWAP；fresh=False 表示这笔成交额是坏的 / 是 0，v 是沿用的上一笔正常 VWAP（可能是 None）。
        止损不看 VWAP，总是判断；止盈要 v；开新一轮要 fresh 且 v 有值。"""
        self.last = (ts, price, bid1, ask1)
        if self.halted or self.pending or price <= 0:
            return
        t, p, b = hms(ts), self.eng.p, self.band
        if t >= LAST_ORDER_TIME:
            return
        if self.leg:
            leg = self.leg
            if leg['side'] == 'sell_first':
                if price >= leg['px'] * (1 + p['stop_k'] * b):
                    self._send(ts, 'close', 'buy', leg['qty'], ask1, price, v, '止损')
                elif v is not None and price <= v * (1 + p['exit_frac'] * b):
                    self._send(ts, 'close', 'buy', leg['qty'], ask1, price, v, '回均价止盈')
            else:
                if price <= leg['px'] * (1 - p['stop_k'] * b):
                    self._send(ts, 'close', 'sell', leg['qty'], bid1, price, v, '止损')
                elif v is not None and price >= v * (1 - p['exit_frac'] * b):
                    self._send(ts, 'close', 'sell', leg['qty'], bid1, price, v, '回均价止盈')
            return
        if not fresh or v is None:
            return
        if t < p['entry_start'] or t > p['entry_end'] or self.left < 2 or self.eng.no_new_rounds():
            return
        if price >= v * (1 + b) and 'sell_first' not in self.banned:
            self._send(ts, 'open', 'sell', self.cap, bid1, price, v, '正T 先卖')
        elif ALLOW_BUY_FIRST and price <= v * (1 - b) and 'buy_first' not in self.banned:
            self._send(ts, 'open', 'buy', self.cap, ask1, price, v, '反T 先买')

    def _send(self, ts, purpose, side, qty, px, last, v, why):
        eng = self.eng
        if purpose == 'open':
            if eng.stop_file():                       # STOP 只挡开新的一轮，平仓照做，免得仓位卡在半路
                return
            if self.left < 2:                         # 开仓额度；平仓不受它限制
                return
        elif self.leg.get('closes', 0) >= CLOSE_TRIES:
            self.halt('这一轮已经发了 %d 笔平仓单还剩 %d 股没平（%s），不再补，请手动处理' % (
                CLOSE_TRIES, self.leg['qty'], '先卖待买回' if self.leg['side'] == 'sell_first' else '先买待卖出'))
            return
        if px <= 0:                                   # 封板，对手价没有
            if purpose == 'close':
                self.warn('limit', '%s %s 想%s（%s）但对手价为 0，可能封板，挂不出去；剩 %d 股' % (
                    hms(ts), self.code, '买回' if side == 'buy' else '卖出', why, self.leg['qty']))
            return
        dev_max = PRICE_DEV_MAX if purpose == 'open' else CLOSE_DEV_MAX
        if abs(px / last - 1) > dev_max:
            self.warn('dev_' + purpose, '%s %s 对手价 %.2f 偏离最新价 %.2f 超过 %.0f%%，不发（%s）' % (
                hms(ts), self.code, px, last, dev_max * 100, why))
            return
        px = round(px, 2)
        try:
            if side == 'sell':
                qty = min(qty, self.cap, eng.broker.can_sell(self.code))
            else:
                qty = min(qty, self.cap, int(eng.broker.cash() / (px * 1.002)))
        except Exception as e:
            eng.log('%s %s 查持仓/资金失败，这次不发：%s' % (hms(ts), self.code, e))
            return
        qty = qty // 100 * 100
        if qty <= 0:
            eng.log('%s %s 想%s但可用不足 100 股/资金不够，不发' % (hms(ts), self.code, '卖' if side == 'sell' else '买'))
            if purpose == 'open':
                self.banned.add('sell_first' if side == 'sell' else 'buy_first')
            return
        marker = 'T0L%s%s%d' % (self.code[:6], hms(ts).replace(':', ''), len(self.orders) + 1)
        try:
            before = eng.broker.order_ids(self.code)
        except Exception as e:
            eng.log('%s %s 查委托失败，这次不发：%s' % (hms(ts), self.code, e))
            return
        rec = {'purpose': purpose, 'side': side, 'qty': qty, 'price': px, 'ts': ts, 'marker': marker,
               'why': why, 'vwap': v, 'sysid': '', 'status': 'sending', 'filled': 0, 'avg': 0.0,
               'mkt': dict(self.book), 'sent_wall': time.time(), 'done_secs': None}
        # 先记账再发：发到一半崩了，重启也不会多发
        self.orders.append(rec)
        if purpose == 'close':
            self.leg['closes'] = self.leg.get('closes', 0) + 1
        self.pending = {'idx': len(self.orders) - 1, 'before': sorted(before), 'sent_at': time.time(),
                        'cancel_at': None, 'lost_at': None}
        eng.save()
        eng.log('%s %s %s %s %s %d 股 @ %.2f（%s，VWAP %s，偏离 %s）第 %d/%d 笔%s｜盘口 最新 %s 买一 %s×%s 卖一 %s×%s' % (
            hms(ts), '【实盘】' if eng.mode == 'live' else '【演示】', self.code, self.name,
            '卖出' if side == 'sell' else '买入', qty, px, why,
            '%.3f' % v if v else '无', '%+.2f%%' % ((last / v - 1) * 100) if v else '-',
            len(self.orders), self.quota,
            '（平仓第 %d/%d 笔）' % (self.leg['closes'], CLOSE_TRIES) if purpose == 'close' else '',
            self.book.get('last'), self.book.get('bid1'), self.book.get('bid_vol1'),
            self.book.get('ask1'), self.book.get('ask_vol1')))
        try:
            eng.broker.submit(self.code, side, qty, px, marker)
        except Exception as e:
            if isinstance(e, AllowOrderOff) or 'ALLOW_ORDER' in str(e):
                self.orders.pop()                     # 单没出门，额度退回
                if purpose == 'close':
                    self.leg['closes'] -= 1
                self.pending = None
                eng.halt_all('桥接服务 ALLOW_ORDER 是 False，下单被挡：%s' % e)
                return
            rec['status'] = 'error'
            self.pending = None
            self.halt('发单出错，不知道单有没有出去，这只停掉，请到 QMT 委托里核对：%s' % e)
            return
        rec['status'] = 'sent'
        eng.save()
        self.poll()

    # -- 跟单 --
    def poll(self):
        if not self.pending:
            return
        eng, pend = self.eng, self.pending
        rec = self.orders[pend['idx']]
        now = time.time()
        o, err = None, None
        try:
            if not rec['sysid']:
                o = eng.broker.find(self.code, rec['side'], rec['qty'], rec['marker'], set(pend['before']),
                                    rec['price'])
                if o is not None:
                    rec['sysid'] = o.order_sysid
                    eng.save()
            else:
                o = eng.broker.get(rec['sysid'])
        except Exception as e:
            err = e
        if o is None:
            # 认不到 / 查不到 / 查询报错：都计时，到点就停掉这只。挂着不管的话 on_tick 一直跳过这只，止损也不判断。
            if err is not None:
                self.warn('query', '%s 查委托失败（下轮再查）：%s' % (self.code, err))
            if not rec['sysid']:
                if now - pend['sent_at'] > RESOLVE_WAIT:
                    self.pending = None
                    rec['status'] = 'unknown'
                    self.halt('发单 %.0f 秒还没在委托列表里认到（备注、或代码方向数量价格都对得上的那笔），这只停掉，'
                              '请到 QMT 委托里核对' % RESOLVE_WAIT)
                return
            if pend.get('lost_at') is None:
                pend['lost_at'] = now
            elif now - pend['lost_at'] > LOST_WAIT:
                self.pending = None
                rec['status'] = 'lost'
                self.halt('委托 %s 连续 %.0f 秒查不到 / 查询报错，这只停掉（止损也停了），请到 QMT 委托里核对、'
                          '必要时手动撤单或平仓' % (rec['sysid'], LOST_WAIT))
            return
        pend['lost_at'] = None
        rec['filled'], rec['avg'] = o.traded_volume, round(o.traded_price, 4)
        if o.order_status in FINAL:
            rec['status'] = {54: '已撤', 53: '部撤', 56: '已成', 57: '废单'}[o.order_status]
            if rec.get('sent_wall'):                  # 发单时刻存在状态文件里，重启接上的单也从真正发单算起
                rec['done_secs'] = round(time.time() - rec['sent_wall'], 1)
            self.pending = None
            self._on_done(rec)
            eng.save()
            return
        # 还挂着：到点就撤，撤了接着等终态
        if pend['cancel_at'] is None and time.time() - pend['sent_at'] > ORDER_WAIT:
            pend['cancel_at'] = time.time()
            try:
                r = eng.broker.cancel(rec['sysid'])
            except Exception as e:                    # 撤单报错也按「撤了等终态」走，RESOLVE_WAIT 后还没终态就停
                eng.log('%s 撤单报错：%s' % (self.code, e))
                r = -1
            eng.log('%s %s 挂了 %.0f 秒没成交完（已成 %d/%d），撤单%s' % (
                dt.datetime.now().strftime('%H:%M:%S'), self.code, ORDER_WAIT, rec['filled'], rec['qty'],
                '' if r == 0 else '失败'))
            eng.save()
        elif pend['cancel_at'] is not None and time.time() - pend['cancel_at'] > RESOLVE_WAIT:
            self.pending = None
            rec['status'] = 'cancel_unknown'
            self.halt('撤单 %.0f 秒还没回终态，这只停掉，请到 QMT 委托里核对' % RESOLVE_WAIT)

    def _on_done(self, rec):
        """顺序：先改仓位 / 盈亏、存状态文件，再写 CSV 报表。报表写不进去（Excel 占着）只记日志，不影响交易。"""
        eng, q, px = self.eng, rec['filled'], rec['avg']
        eng.log('%s %s %s %s 结果：%s，成交 %d/%d 股%s' % (
            dt.datetime.now().strftime('%H:%M:%S'), '【实盘】' if eng.mode == 'live' else '【演示】',
            self.code, self.name, rec['status'], q, rec['qty'], ' 均价 %.3f' % px if q else ''))
        rnd = None
        if q > 0 and rec['purpose'] == 'open':
            self.leg = {'side': 'sell_first' if rec['side'] == 'sell' else 'buy_first', 'qty': q, 'px': px,
                        'vwap': rec['vwap'], 'ts': rec['ts'], 'closes': 0}
        elif q > 0:
            leg = self.leg
            sell_px, buy_px = (leg['px'], px) if leg['side'] == 'sell_first' else (px, leg['px'])
            gross = (sell_px - buy_px) * q
            cost = fee(eng.p, 'sell', sell_px * q) + fee(eng.p, 'buy', buy_px * q)
            net = gross - cost
            self.realized += net
            if rec['why'] == '止损':
                self.banned.add(leg['side'])
            rnd = (dict(leg), q, gross, cost, net)
            leg['qty'] -= q
            if leg['qty'] <= 0:
                self.leg = None
        eng.save()
        eng.report(eng.order_row, self, rec)
        if rnd:
            leg0, q, gross, cost, net = rnd
            eng.report(eng.round_row, self, leg0, rec, q, gross, cost, net)
            eng.log('    平 %d 股：毛利 %+.0f 费用 %.0f 净 %+.0f（这只当天累计 %+.0f）%s' % (
                q, gross, cost, net, self.realized,
                '' if self.leg is None else '，还剩 %d 股没平，条件到了接着补单（平仓已发 %d/%d 笔）' % (
                    self.leg['qty'], self.leg.get('closes', 0), CLOSE_TRIES)))
        elif rec['purpose'] == 'close' and self.leg:
            eng.log('    平仓单没成交，还剩 %d 股，条件到了接着补单（平仓已发 %d/%d 笔）' % (
                self.leg['qty'], self.leg.get('closes', 0), CLOSE_TRIES))

    def halt(self, why):
        self.halted = why
        self.eng.log('!!! %s %s 停止：%s' % (self.code, self.name, why))
        self.eng.save()


# ---------------------------------------------------------------- 引擎

class Engine(object):

    def __init__(self, mode, day, broker):
        from qmt_bridge import xtdata
        self.xt, self.mode, self.day, self.broker = xtdata, mode, day, broker
        self.p = load_params()
        sub = {'live': 'live', 'dry': 'live_dry', 'replay': 'live_replay'}[mode]
        self.dir = os.path.join(t0_sim.SIM_DIR, sub)
        os.makedirs(self.dir, exist_ok=True)
        self.state_path = os.path.join(self.dir, 'state_%s.json' % day)
        self.logf = open(os.path.join(self.dir, 'log_%s.txt' % day), 'a', encoding='utf-8-sig')
        self.halted = ''
        self.lock_path = None
        self.force_lock = False
        bands = self._bands(list(LIMITS), day)
        self.stocks = {}
        saved = {}
        if mode != 'replay' and os.path.exists(self.state_path):
            with open(self.state_path, encoding='utf-8') as f:
                saved = json.load(f)
            self.halted = saved.get('_halted', '')
        for code, cap in LIMITS.items():
            name = (xtdata.get_instrument_detail(code) or {}).get('InstrumentName', '')
            st = LiveT(self, code, name, cap, bands.get(code, self.p['band_min']))
            if code in saved:
                st.load(saved[code])
                if st.pending:                       # 重启：挂着的单重新从「刚发出」开始计时
                    st.pending['sent_at'] = time.time()
                    st.pending['cancel_at'] = None
            self.stocks[code] = st
            self.log('%s %s 单笔 %d 股 带宽 %.2f%% 今天已用 %d/%d 笔%s%s' % (
                code, name, cap, st.band * 100, len(st.orders), st.quota,
                '，未平 %s %d 股 @ %.3f' % (st.leg['side'], st.leg['qty'], st.leg['px']) if st.leg else '',
                '，已停：' + st.halted if st.halted else ''))

    # -- 公共 --
    def log(self, msg):
        print(msg, flush=True)
        self.logf.write(msg + '\n')
        self.logf.flush()

    def save(self):
        if self.mode == 'replay':
            return
        d = {c: st.dump() for c, st in self.stocks.items()}
        d['_halted'] = self.halted
        tmp = self.state_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)

    def stop_file(self):
        if os.path.exists(os.path.join(self.dir, 'STOP')):
            if not self.halted:
                self.halt_all('发现 STOP 文件')
            return True
        return bool(self.halted)

    def no_new_rounds(self):
        loss = sum(st.realized for st in self.stocks.values())
        return loss <= -MAX_DAILY_LOSS

    def halt_all(self, why):
        if not self.halted:
            self.halted = why
            self.log('!!! 全部停止新单：%s' % why)
            self.save()

    def report(self, fn, *args):
        """写 CSV 报表。失败（Excel 打开着锁住文件、OneDrive 同步占用、磁盘问题）只记日志，交易照常；
        状态文件在这之前已经存好，报表缺的那行可以从日志和状态文件补。"""
        try:
            fn(*args)
        except Exception as e:
            self.log('!!! 写报表失败（%s，文件是不是被 Excel 打开了？），交易不受影响，这行先记在这里：%s' % (
                e, json.dumps(args[-1] if fn == self.order_row else args[2:], ensure_ascii=False, default=str)[:600]))

    # -- 同一个记账目录只能跑一个 --
    def acquire_lock(self):
        path = os.path.join(self.dir, 'live.lock')
        me = {'host': socket.gethostname(), 'pid': os.getpid(), 'day': self.day,
              'start': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)     # 原子创建，两个同时起也只有一个成功
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(me, f, ensure_ascii=False)
                self.lock_path = path
                return True
            except FileExistsError:
                try:
                    with open(path, encoding='utf-8') as f:
                        old = json.load(f)
                except Exception:
                    old = {}
                stale = old.get('day') != self.day or (old.get('host') == me['host'] and not pid_alive(old.get('pid')))
                if not (stale or self.force_lock):
                    self.log('!!! 已经有 t0_live 在用 %s：机器 %s、进程 %s、%s 启动。不能同时跑两个（会重复下单）。'
                             '确定那个已经不在了（比如另一台机器异常关机），加 --force 接管' % (
                                 self.dir, old.get('host'), old.get('pid'), old.get('start')))
                    return False
                self.log('旧的运行锁已失效（%s），接管' % ('--force' if not stale else old or '内容读不出'))
                try:
                    os.remove(path)
                except OSError:
                    pass
        return False

    def release_lock(self):
        if self.lock_path:
            try:
                os.remove(self.lock_path)
            except OSError:
                pass
            self.lock_path = None

    def fresh_enough(self, code, ts, now=None):
        """行情过期或倒序就不用：QMT 行情冻住时 get_full_tick 还会一直给最后那笔，拿它下单价格就是旧的。"""
        st = self.stocks[code]
        if st.last and ts < st.last[0]:
            return False
        try:
            age = ((now or dt.datetime.now()) - dt.datetime.strptime(ts[:14], '%Y%m%d%H%M%S')).total_seconds()
        except ValueError:
            return False
        if age > STALE_MAX:
            st.warn('stale', '%s 行情 %.0f 秒没更新（最后一笔 %s），先不用%s' % (
                code, age, hms(ts), '；有未平仓位，止损也在等新行情' if st.leg else ''))
            return False
        return True

    def order_row(self, st, rec):
        path = os.path.join(self.dir, 'orders.csv')
        upgrade_header(path, ORDER_COLS)
        row = {'日期': self.day, '模式': self.mode, '代码': st.code, '名称': st.name,
               '用途': '开' if rec['purpose'] == 'open' else '平', '方向': '卖' if rec['side'] == 'sell' else '买',
               '委托量': rec['qty'], '委托价': rec['price'], '发单时间': hms(rec['ts']), '委托号': rec['sysid'],
               '状态': rec['status'], '成交量': rec['filled'], '成交均价': rec['avg'], '备注': rec['why']}
        row.update(slippage_cols(rec))
        append_csv(path, ORDER_COLS, [row])

    def round_row(self, st, leg, rec, q, gross, cost, net):
        append_csv(os.path.join(self.dir, 'rounds.csv'), ROUND_COLS, [{
            '日期': self.day, '模式': self.mode, '代码': st.code, '名称': st.name,
            '方向': '正T' if leg['side'] == 'sell_first' else '反T', '数量': q,
            '开仓时间': hms(leg['ts']), '开仓价': leg['px'], '平仓时间': hms(rec['ts']), '平仓价': rec['avg'],
            '原因': rec['why'], '毛利': round(gross, 2), '费用': round(cost, 2), '净利': round(net, 2),
            '带宽': round(st.band, 4), '参数版本': self.p['version']}])

    def _bands(self, codes, day):
        data = self.xt.get_market_data_ex(['high', 'low', 'close'], codes, period='1d',
                                          end_time=day, count=25, dividend_type='front')
        out = {}
        for c in codes:
            df = data.get(c)
            if df is None or len(df) < 5:
                continue
            df = df[[str(i)[:8] < day for i in df.index]].tail(21)
            amp = ((df['high'] - df['low']) / df['close'].shift(1)).dropna().tail(20).mean()
            out[c] = min(max(amp * self.p['band_k'], self.p['band_min']), self.p['band_max'])
        return out

    @staticmethod
    def _vwap(amount, volume, price):
        if volume <= 0 or amount <= 0:
            return None
        unit = 100 if amount / volume / price > 50 else 1
        return amount / (volume * unit)

    def feed(self, code, ts, price, bid1, ask1, amount, volume, bid_vol1=None, ask_vol1=None):
        st = self.stocks.get(code)
        if st is None:
            return
        st.poll()
        if price <= 0:
            return
        v = self._vwap(amount, volume, price)
        if v is None or abs(v / price - 1) > VWAP_DEV_MAX:
            # 成交额坏了（2026-10-19 13:58 一笔放大 100 倍，误触发止盈；10-23 13:17~14:09 get_full_tick 约两成
            # 快照这样坏），或者成交额 / 成交量是 0。价格和盘口是对的：有未平仓位就照常判断平仓 ——
            # 止损不用 VWAP，止盈用上一笔正常的 VWAP（没有就先不判断止盈）；没仓位就丢掉，不拿它开新一轮。
            if v is not None:
                st.bad_ticks += 1
                if st.bad_ticks in (1, 20, 200) or st.bad_ticks % 1000 == 0:
                    self.log('%s %s 丢掉坏 tick（VWAP %.3f vs 现价 %.3f，第 %d 次%s）' % (
                        hms(ts), code, v, price, st.bad_ticks,
                        ('；有未平仓位，止损照常判断，止盈按上一笔 VWAP %.3f' % st.last_vwap if st.last_vwap
                         else '；有未平仓位，止损照常判断，还没有正常 VWAP，止盈先不判断') if st.leg else ''))
            if not st.leg:
                return
            st.book = {'ts': ts, 'last': price, 'bid1': bid1, 'ask1': ask1, 'bid_vol1': bid_vol1, 'ask_vol1': ask_vol1}
            st.on_tick(ts, price, bid1, ask1, st.last_vwap, fresh=False)
            return
        st.last_vwap = v
        st.book = {'ts': ts, 'last': price, 'bid1': bid1, 'ask1': ask1, 'bid_vol1': bid_vol1, 'ask_vol1': ask_vol1}
        st.on_tick(ts, price, bid1, ask1, v)

    # -- 实时 --
    def run_live(self, interval, until):
        codes = list(self.stocks)
        if not self.acquire_lock():
            self.logf.close()
            return
        self.log('== %s %s 开始（每 %.0f 秒），%s 收工；停：在 %s 建 STOP 文件 ==' % (
            self.day, '实盘' if self.mode == 'live' else '演示（不下单）', interval, until, self.dir))
        errs, last_save = 0, time.time()
        try:
            while True:
                now = dt.datetime.now().strftime('%H:%M:%S')
                if now >= until:
                    break
                try:
                    self._live_step(now, codes)
                    errs = 0
                except Exception:
                    # 循环里任何意外都不能让程序退出：退出了手上那轮就没人止损。记下来，3 秒后接着跑。
                    errs += 1
                    if errs <= 3 or errs % 100 == 0:
                        self.log('!!! %s 循环出错（连续第 %d 次），接着跑：\n%s' % (now, errs, traceback.format_exc()))
                    time.sleep(3)
                    continue
                if time.time() - last_save >= 60:             # 顺带把 last_vwap 存下来，重启后止盈有得用
                    try:
                        self.save()
                    except Exception as e:
                        self.log('存状态文件失败：%s' % e)
                    last_save = time.time()
                time.sleep(interval if '09:29:50' <= now <= '11:30:05' or '13:00:03' <= now else 5)
        except KeyboardInterrupt:
            self.log('手动中断')
        self.finish()

    def _live_step(self, now, codes):
        for st in self.stocks.values():
            st.poll()
        if '11:30:05' < now < '13:00:03' or now < '09:29:50':
            return
        if now >= LAST_ORDER_TIME:                    # 按本机时间截止：只跟挂着的单，不再喂行情、不再发新单
            return
        try:
            ticks = self.xt.get_client().call('get_full_tick', timeout=t0_sim.CALL_TIMEOUT, code_list=codes)
            self.fails = 0
        except Exception as e:
            self.fails = getattr(self, 'fails', 0) + 1
            self.log('%s 取行情失败（连续第 %d 次）：%s' % (now, self.fails, e))
            return
        for c, tk in ticks.items():
            if not tk or c not in self.stocks:
                continue
            ts = str(tk.get('timetag', '')).replace(' ', '').replace(':', '')
            if not ts.startswith(self.day) or not self.fresh_enough(c, ts):
                continue
            self.feed(c, ts, tk.get('lastPrice', 0), (tk.get('bidPrice') or [0])[0],
                      (tk.get('askPrice') or [0])[0], tk.get('amount', 0), tk.get('volume', 0),
                      (tk.get('bidVol') or [None])[0], (tk.get('askVol') or [None])[0])

    # -- 回放 --
    def run_replay(self, until):
        codes = list(self.stocks)
        self.log('== %s 回放（假成交）到 %s ==' % (self.day, until))
        data = self.xt.get_market_data_ex([], codes, period='tick', start_time=self.day,
                                          end_time=self.day + '235959')
        events = []
        for c in codes:
            df = data.get(c)
            if df is None or not len(df):
                self.log('%s 没有当天逐笔' % c)
                continue
            for ts, r in df.iterrows():
                ts = str(ts).split('.')[0]
                if hms(ts) > until:
                    break
                bp, ap = r.get('bidPrice'), r.get('askPrice')
                bv, av = r.get('bidVol'), r.get('askVol')
                events.append((ts, c, r['lastPrice'], bp[0] if isinstance(bp, list) and bp else 0,
                               ap[0] if isinstance(ap, list) and ap else 0, r['amount'], r['volume'],
                               bv[0] if isinstance(bv, list) and bv else None,
                               av[0] if isinstance(av, list) and av else None))
        events.sort()
        for ev in events:
            self.feed(ev[1], ev[0], *ev[2:])
        self.finish()

    def finish(self):
        self.log('== %s 收工 ==' % self.day)
        tot = 0.0
        for c, st in self.stocks.items():
            tot += st.realized
            self.log('%s %s 用了 %d/%d 笔，已实现净 %+.0f%s%s' % (
                c, st.name, len(st.orders), st.quota, st.realized,
                '，**未平 %s %d 股 @ %.3f，留给你处理**' % (
                    '先卖待买回' if st.leg['side'] == 'sell_first' else '先买待卖出',
                    st.leg['qty'], st.leg['px']) if st.leg else '',
                '，挂单未了结（委托 %s），请到 QMT 核对' % st.orders[st.pending['idx']]['sysid']
                if st.pending else ''))
        self.log('合计已实现净 %+.0f 元' % tot)
        self.save()
        self.release_lock()
        self.logf.close()


def status():
    day = dt.date.today().strftime('%Y%m%d')
    for sub in ('live', 'live_dry'):
        path = os.path.join(t0_sim.SIM_DIR, sub, 'state_%s.json' % day)
        if not os.path.exists(path):
            print('%s：今天还没有记录' % sub)
            continue
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        print('== %s %s%s ==' % (sub, day, '（全部停：%s）' % d['_halted'] if d.get('_halted') else ''))
        for c, s in d.items():
            if c.startswith('_'):
                continue
            print('%s 已用 %d/%d 笔 已实现 %+.0f%s%s' % (
                c, len(s['orders']), quota(c, len(s['orders']), s['realized']), s['realized'],
                '，未平 %d 股' % s['leg']['qty'] if s['leg'] else '', '，停：' + s['halted'] if s['halted'] else ''))
            for o in s['orders']:
                print('   %s %s %s %d @ %.2f -> %s 成交 %d 均价 %.3f 委托号 %s' % (
                    hms(o['ts']), o['why'], o['side'], o['qty'], o['price'], o['status'], o['filled'],
                    o['avg'], o['sysid']))


def main():
    ap = argparse.ArgumentParser(description='持仓做 T 实盘（默认演示不下单，--live 才真下）')
    ap.add_argument('--live', action='store_true', help='真下单（桥接服务那边也要 ALLOW_ORDER=True）')
    ap.add_argument('--replay', metavar='YYYYMMDD', help='用这天逐笔回放，假成交')
    ap.add_argument('--until', help='几点收工，默认 14:57:00（回放默认 15:00:00）')
    ap.add_argument('--interval', type=float, default=3.0, help='轮询间隔秒，默认 3')
    ap.add_argument('--status', action='store_true', help='看今天的委托计数和状态')
    ap.add_argument('--force', action='store_true', help='无视运行锁（确定另一个 t0_live 已经不在了才用）')
    a = ap.parse_args()
    if a.status:
        status()
        return
    from qmt_bridge import xtdata
    xtdata.enable_hello = False
    xtdata.connect()
    if a.replay:
        Engine('replay', a.replay, PaperBroker()).run_replay(a.until or '15:00:00')
        return
    day = dt.date.today().strftime('%Y%m%d')
    if a.live:
        broker = RealBroker()
        print('实盘模式：账号 %s，只做 %s，每只每天最多 %d 笔%s' % (
            broker.acc.account_id, ', '.join('%s≤%d股' % kv for kv in LIMITS.items()), MAX_ORDERS,
            ''.join('，%s 净赚超过 %s 元依次再开一轮' % (c, ' / '.join('%g' % x for x in v))
                    for c, v in EXTRA_ROUNDS.items())))
        eng = Engine('live', day, broker)
        eng.force_lock = a.force
        eng.run_live(a.interval, a.until or '14:57:00')
    else:
        try:                                         # 演示用真实可用持仓/资金（只读），读不到就当够用
            rb = RealBroker()
            pos = {c: rb.can_sell(c) for c in LIMITS}
            cash = rb.cash()
        except Exception as e:
            print('读账户失败，演示按持仓资金充足算：%s' % e)
            pos, cash = None, None
        eng = Engine('dry', day, PaperBroker(pos, cash))
        eng.force_lock = a.force
        eng.run_live(a.interval, a.until or '14:57:00')


if __name__ == '__main__':
    main()
