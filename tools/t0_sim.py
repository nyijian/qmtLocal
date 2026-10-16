# coding:utf-8
"""持仓做 T 的模拟盘：盯实时行情，按规则虚拟买卖，记账、复盘。**不下任何真实委托。**

    .venv313\\Scripts\\python.exe tools\\t0_sim.py --holdings 持仓.csv               # 实盘时段盯盘，15:00 自动出日报
    .venv313\\Scripts\\python.exe tools\\t0_sim.py --from-account                    # 底仓直接读账户持仓（只读）
    .venv313\\Scripts\\python.exe tools\\t0_sim.py --holdings 持仓.csv --replay 20261015   # 用当天逐笔回放，验证规则
    .venv313\\Scripts\\python.exe tools\\t0_sim.py --report                          # 只看累计战绩

持仓文件：CSV，两列「代码,股数」，例如 `600366.SH,5000`。代码不带后缀时 6 开头当 SH，其余当 SZ。

规则（参数在共享数据目录 sim_t0/params.json，第一次运行自动生成，复盘后改它）：
  * 底仓 = 持仓文件里的股数，每天开盘重新按它算，不受前一天模拟影响（T+0 做完当天还原）。
  * 每只每次做 T 的数量 = 底仓 × qty_ratio（默认 20%），按 100 股向下取整，不够 100 股不做。
  * 带宽 band = 近 20 日平均振幅 × band_k，夹在 [band_min, band_max]。
  * 价格 ≥ VWAP × (1 + band)：先卖后买（正 T）；价格 ≤ VWAP × (1 - band)：先买后卖（反 T）。
  * 平仓：回到 VWAP ± exit_frac × band 止盈；反向再走 stop_k × band 止损；force_time 强制还原。
  * 每只每天最多 max_rounds 轮；止损过的方向当天不再做。entry_start 前、entry_end 后不开新仓。
  * 成交价按吃对手价算（卖用买一、买用卖一），涨跌停封死（对手价为 0）不成交。
  * 费用：佣金 commission（默认万 5，最低 min_commission 元）、卖出印花税 stamp_tax、过户费 transfer_fee。

记账（共享数据目录 sim_t0/，回放的记到 sim_t0/replay/，不跟实盘模拟混）：
  trades.csv   每一轮一行：开平时间价格、原因、毛利、费用、净利、当时的参数版本
  daily.csv    每天每只一行汇总
  log_<日期>.txt  盘中事件流水

数据：行情走桥（get_full_tick 轮询；回放用 get_market_data_ex 的 tick 周期），要大QMT 开着、
桥接服务在运行。VWAP = 当日累计成交额 / 累计成交量。

只在 .venv313 跑（用了 3.7+ 写法）。
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qmt_bridge.dotenv_lite import load_dotenv  # noqa: E402

load_dotenv()
DATA_DIR = os.environ.get('QMTLOCAL_DATA_DIR') or os.path.join(ROOT, 'real_data')
SIM_DIR = os.path.join(DATA_DIR, 'sim_t0')
CALL_TIMEOUT = 8.0                    # 单次取行情的超时（秒）

DEFAULT_PARAMS = {
    'version': 1,
    'qty_ratio': 0.2,
    'band_k': 0.35,
    'band_min': 0.006,
    'band_max': 0.03,
    'exit_frac': 0.0,           # 0 = 回到 VWAP 就平；0.2 = 还差 0.2 个带宽就平
    'stop_k': 1.0,
    'max_rounds': 2,
    'entry_start': '09:35:00',
    'entry_end': '14:30:00',
    'force_time': '14:50:00',
    'commission': 0.0005,           # 本账户实际佣金万 5（2026-10-15 用户确认）
    'min_commission': 5.0,
    'stamp_tax': 0.0005,
    'transfer_fee': 0.00001,
}

TRADE_COLS = ['日期', '模式', '代码', '名称', '方向', '数量', '开仓时间', '开仓价', '开仓VWAP',
              '平仓时间', '平仓价', '原因', '毛利', '费用', '净利', '带宽', '参数版本']
DAILY_COLS = ['日期', '模式', '代码', '名称', '底仓', '每次数量', '轮数', '盈利轮', '毛利', '费用', '净利',
              '收盘价', '昨收', '底仓当日盈亏', '参数版本']


# ---------------------------------------------------------------- 工具

def full_code(c):
    c = c.strip().upper()
    if '.' in c:
        return c
    return c + ('.SH' if c[0] in '659' else '.SZ')


def load_params():
    os.makedirs(SIM_DIR, exist_ok=True)
    path = os.path.join(SIM_DIR, 'params.json')
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_PARAMS, f, ensure_ascii=False, indent=2)
        return dict(DEFAULT_PARAMS)
    with open(path, encoding='utf-8') as f:
        p = json.load(f)
    return {**DEFAULT_PARAMS, **p}


def load_holdings(path):
    out = {}
    with open(path, encoding='utf-8-sig') as f:
        for row in csv.reader(f):
            if not row or not row[0].strip() or row[0].strip().startswith('#'):
                continue
            try:
                out[full_code(row[0])] = int(float(row[1]))
            except (IndexError, ValueError):
                continue                      # 表头或空行
    return out


def holdings_from_account():
    """只读查询账户持仓，用当天开盘前的股数（volume）当底仓。"""
    from qmt_bridge.xttrader import XtQuantTrader
    from qmt_bridge.xttype import StockAccount
    acct = os.environ.get('QMT_ACCOUNT_ID', '')
    if not acct:
        sys.exit('.env 里没有 QMT_ACCOUNT_ID')
    trader = XtQuantTrader('', 0)
    trader.start()
    trader.connect()
    acc = StockAccount(acct, os.environ.get('QMT_ACCOUNT_TYPE', 'STOCK'))
    return {p.stock_code: int(p.volume) for p in trader.query_stock_positions(acc) if p.volume > 0}


def fee(p, side, amount):
    c = max(amount * p['commission'], p['min_commission'])
    t = amount * p['transfer_fee']
    s = amount * p['stamp_tax'] if side == 'sell' else 0.0
    return c + t + s


def append_csv(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, 'a', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def hms(ts):
    return ts[8:10] + ':' + ts[10:12] + ':' + ts[12:14] if len(ts) >= 14 else ts


# ---------------------------------------------------------------- 单只股票的做 T 状态机

class StockT(object):

    def __init__(self, code, name, base, band, p, log):
        self.code, self.name, self.base, self.band, self.p, self.log = code, name, base, band, p, log
        self.qty = int(base * p['qty_ratio'] // 100 * 100)
        self.leg = None                       # 当前未平的一轮
        self.rounds = []                      # 已平的
        self.banned = set()                   # 止损过的方向
        self.last = None                      # 最后一笔行情，收盘结算用
        self.pre = None                       # 昨收，盘中从行情里记下，收盘结算不用再联网
        self.vol_unit = None                  # 成交量单位：1 = 股，100 = 手

    def vwap(self, amount, volume, price):
        if volume <= 0 or amount <= 0:
            return None
        if self.vol_unit is None:
            self.vol_unit = 100 if amount / volume / price > 50 else 1
        return amount / (volume * self.vol_unit)

    def on_tick(self, ts, price, bid1, ask1, amount, volume, pre=None):
        if pre:
            self.pre = pre
        if price <= 0:
            return
        self.last = (ts, price, bid1, ask1)
        v = self.vwap(amount, volume, price)
        if v is None or self.qty <= 0:
            return
        t = hms(ts)
        p, b = self.p, self.band
        if self.leg is not None:
            self._check_exit(ts, t, price, bid1, ask1, v)
            return
        if t < p['entry_start'] or t > p['entry_end'] or len(self.rounds) >= p['max_rounds']:
            return
        if price >= v * (1 + b) and 'sell_first' not in self.banned and bid1 > 0:
            self._open(ts, 'sell_first', bid1, v)
        elif price <= v * (1 - b) and 'buy_first' not in self.banned and ask1 > 0:
            self._open(ts, 'buy_first', ask1, v)

    def _open(self, ts, side, px, v):
        self.leg = {'side': side, 'px': px, 'ts': ts, 'vwap': v}
        self.log('%s %s %s 开 %s %d 股 @ %.3f（VWAP %.3f，偏离 %+.2f%%，带宽 %.2f%%）' % (
            hms(ts), self.code, self.name, '正T 先卖' if side == 'sell_first' else '反T 先买',
            self.qty, px, v, (px / v - 1) * 100, self.band * 100))

    def _check_exit(self, ts, t, price, bid1, ask1, v):
        p, b, leg = self.p, self.band, self.leg
        reason = None
        if leg['side'] == 'sell_first':           # 等低位买回
            if t >= p['force_time']:
                reason = '尾盘还原'
            elif price <= v * (1 + p['exit_frac'] * b):
                reason = '回均价止盈'
            elif price >= leg['px'] * (1 + p['stop_k'] * b):
                reason = '止损'
            fill = ask1
        else:                                     # 等高位卖出
            if t >= p['force_time']:
                reason = '尾盘还原'
            elif price >= v * (1 - p['exit_frac'] * b):
                reason = '回均价止盈'
            elif price <= leg['px'] * (1 - p['stop_k'] * b):
                reason = '止损'
            fill = bid1
        if reason is None:
            return
        if fill <= 0:                             # 封板吃不到，等下一笔；尾盘还原拿现价兜底
            if reason != '尾盘还原':
                return
            fill = price
        self._close(ts, fill, v, reason)

    def _close(self, ts, px, v, reason):
        leg, q, p = self.leg, self.qty, self.p
        if leg['side'] == 'sell_first':
            sell_px, buy_px = leg['px'], px
        else:
            buy_px, sell_px = leg['px'], px
        gross = (sell_px - buy_px) * q
        cost = fee(p, 'sell', sell_px * q) + fee(p, 'buy', buy_px * q)
        net = gross - cost
        if reason == '止损':
            self.banned.add(leg['side'])
        self.rounds.append({
            '方向': '正T' if leg['side'] == 'sell_first' else '反T', '数量': q,
            '开仓时间': hms(leg['ts']), '开仓价': round(leg['px'], 3), '开仓VWAP': round(leg['vwap'], 3),
            '平仓时间': hms(ts), '平仓价': round(px, 3), '原因': reason,
            '毛利': round(gross, 2), '费用': round(cost, 2), '净利': round(net, 2),
            '带宽': round(self.band, 4), '参数版本': p['version']})
        self.leg = None
        self.log('%s %s %s 平 %s @ %.3f（%s）毛利 %+.0f 费用 %.0f 净 %+.0f' % (
            hms(ts), self.code, self.name, '买回' if leg['side'] == 'sell_first' else '卖出',
            px, reason, gross, cost, net))

    def settle(self):
        """收盘或中途退出：没平的按最后一笔对手价还原。"""
        if self.leg is not None and self.last is not None:
            ts, price, bid1, ask1 = self.last
            px = (ask1 if self.leg['side'] == 'sell_first' else bid1) or price
            self._close(ts, px, self.leg['vwap'], '收盘还原')


# ---------------------------------------------------------------- 运行

class Session(object):

    def __init__(self, holdings, params, day, mode):
        from qmt_bridge import xtdata
        self.xt = xtdata
        self.p, self.day, self.mode = params, day, mode
        self.dir = SIM_DIR if mode == 'live' else os.path.join(SIM_DIR, 'replay')
        os.makedirs(self.dir, exist_ok=True)
        self.logf = open(os.path.join(self.dir, 'log_%s.txt' % day), 'a', encoding='utf-8-sig')
        self.stocks = {}
        self.fails = 0
        bands = self._bands(list(holdings), day)
        for code, base in holdings.items():
            det = xtdata.get_instrument_detail(code) or {}
            name = det.get('InstrumentName', '')
            st = StockT(code, name, base, bands.get(code, params['band_min']), params, self.log)
            self.stocks[code] = st
            self.log('%s %s 底仓 %d 每次 %d 股 带宽 %.2f%%%s' % (
                code, name, base, st.qty, st.band * 100, '' if st.qty else '（不足 100 股，不做）'))

    def log(self, msg):
        print(msg)
        self.logf.write(msg + '\n')
        self.logf.flush()

    def _bands(self, codes, day):
        """近 20 个交易日（不含当天）平均振幅 × band_k。"""
        p, out = self.p, {}
        data = self.xt.get_market_data_ex(['high', 'low', 'close'], codes, period='1d',
                                          end_time=day, count=25, dividend_type='front')
        for c in codes:
            df = data.get(c)
            if df is None or len(df) < 5:
                continue
            df = df[[str(i)[:8] < day for i in df.index]].tail(21)
            pre = df['close'].shift(1)
            amp = ((df['high'] - df['low']) / pre).dropna().tail(20).mean()
            out[c] = min(max(amp * p['band_k'], p['band_min']), p['band_max'])
        return out

    # -- 实时 --
    def run_live(self, interval, until):
        codes = list(self.stocks)
        self.log('== %s 开始盯盘（每 %.0f 秒），%s 收工 ==' % (self.day, interval, until))
        try:
            while True:
                now = dt.datetime.now().strftime('%H:%M:%S')
                if now >= until:
                    break
                # 午休、开盘前不取：这些时段 QMT 没有行情，老版本的桥还会被饿死，请求只会超时
                if '11:30:05' < now < '13:00:03' or now < '09:29:50':
                    time.sleep(5)
                    continue
                try:
                    # 超时设短：卡住时宁可 3 秒后重来，也别一次瞎 30 秒
                    ticks = self.xt.get_client().call('get_full_tick', timeout=CALL_TIMEOUT,
                                                      code_list=codes)
                    self.fails = 0
                except Exception as e:
                    self.fails += 1
                    self.log('%s 取行情失败（连续第 %d 次）：%s' % (now, self.fails, e))
                    time.sleep(3)
                    continue
                for c, tk in ticks.items():
                    st = self.stocks.get(c)
                    if not st or not tk:
                        continue
                    ts = str(tk.get('timetag', '')).replace(' ', '').replace(':', '')
                    if not ts.startswith(self.day):
                        continue
                    st.on_tick(ts, tk.get('lastPrice', 0), (tk.get('bidPrice') or [0])[0],
                               (tk.get('askPrice') or [0])[0], tk.get('amount', 0), tk.get('volume', 0),
                               tk.get('lastClose'))
                time.sleep(interval)
        except KeyboardInterrupt:
            self.log('手动中断')
        self.finish()

    # -- 回放 --
    def run_replay(self, until):
        codes = list(self.stocks)
        self.log('== %s 回放 %d 只，回放到 %s ==' % (self.day, len(codes), until))
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
                events.append((ts, c, r['lastPrice'], bp[0] if isinstance(bp, list) and bp else 0,
                               ap[0] if isinstance(ap, list) and ap else 0, r['amount'], r['volume'],
                               r.get('lastClose')))
        events.sort()
        for ts, c, *rest in events:
            self.stocks[c].on_tick(ts, *rest)
        self.finish()

    # -- 结算 --
    def finish(self):
        trades, daily = [], []
        for c, st in self.stocks.items():
            st.settle()
            for r in st.rounds:
                trades.append({'日期': self.day, '模式': self.mode, '代码': c, '名称': st.name, **r})
            close = st.last[1] if st.last else None
            pre = st.pre
            if pre is None:
                try:
                    pre = (self.xt.get_instrument_detail(c) or {}).get('PreClose')
                except Exception:
                    pre = None
            net = sum(r['净利'] for r in st.rounds)
            daily.append({'日期': self.day, '模式': self.mode, '代码': c, '名称': st.name, '底仓': st.base,
                          '每次数量': st.qty, '轮数': len(st.rounds),
                          '盈利轮': sum(1 for r in st.rounds if r['净利'] > 0),
                          '毛利': round(sum(r['毛利'] for r in st.rounds), 2),
                          '费用': round(sum(r['费用'] for r in st.rounds), 2), '净利': round(net, 2),
                          '收盘价': close, '昨收': pre,
                          '底仓当日盈亏': round((close - pre) * st.base, 2) if close and pre else None,
                          '参数版本': self.p['version']})
        append_csv(os.path.join(self.dir, 'trades.csv'), TRADE_COLS, trades)
        append_csv(os.path.join(self.dir, 'daily.csv'), DAILY_COLS, daily)
        self.log('== %s 日报 ==' % self.day)
        tot = 0.0
        for d in daily:
            tot += d['净利']
            self.log('%s %s 轮数 %d 盈利 %d 毛利 %+.0f 费用 %.0f 净 %+.0f｜底仓当日 %s' % (
                d['代码'], d['名称'], d['轮数'], d['盈利轮'], d['毛利'], d['费用'], d['净利'],
                '%+.0f' % d['底仓当日盈亏'] if d['底仓当日盈亏'] is not None else '-'))
        self.log('做 T 合计净利 %+.0f 元（这部分是相对「不动」的超额）' % tot)
        self.logf.close()


def report(mode):
    import pandas as pd
    d = SIM_DIR if mode == 'live' else os.path.join(SIM_DIR, 'replay')
    tp = os.path.join(d, 'trades.csv')
    if not os.path.exists(tp):
        print('还没有记录：%s' % tp)
        return
    t = pd.read_csv(tp, encoding='utf-8-sig', dtype={'代码': str})
    print('== 累计（%s）%d 天 %d 轮 ==' % (mode, t['日期'].nunique(), len(t)))
    print('净利 %+.0f，毛利 %+.0f，费用 %.0f，胜率 %.0f%%' % (
        t['净利'].sum(), t['毛利'].sum(), t['费用'].sum(), (t['净利'] > 0).mean() * 100))
    for key in ['日期', '代码', '方向', '原因', '参数版本']:
        g = t.groupby(key)['净利'].agg(['count', 'sum', lambda s: (s > 0).mean() * 100])
        g.columns = ['轮数', '净利', '胜率%']
        print('-- 按%s' % key)
        print(g.round(1).to_string())


def main():
    ap = argparse.ArgumentParser(description='持仓做 T 模拟盘（不下真实委托）')
    ap.add_argument('--holdings', help='持仓 CSV：代码,股数')
    ap.add_argument('--from-account', action='store_true', help='底仓从账户持仓读（只读）')
    ap.add_argument('--replay', metavar='YYYYMMDD', help='用这天的逐笔回放，不盯实时')
    ap.add_argument('--until', help='实时：几点收工（默认 14:57:00，收盘集合竞价不连续成交，14:50 已强制还原）；回放：回放到几点（默认 15:00:00）')
    ap.add_argument('--interval', type=float, default=3.0, help='实时轮询间隔秒，默认 3')
    ap.add_argument('--report', action='store_true', help='只打印累计战绩')
    ap.add_argument('--replay-report', action='store_true', help='只打印回放的累计战绩')
    a = ap.parse_args()

    if a.report or a.replay_report:
        report('replay' if a.replay_report else 'live')
        return
    if a.from_account:
        holdings = holdings_from_account()
    elif a.holdings:
        holdings = load_holdings(a.holdings)
    else:
        ap.error('要么 --holdings 文件，要么 --from-account')
    if not holdings:
        sys.exit('底仓是空的')

    from qmt_bridge import xtdata
    xtdata.enable_hello = False
    xtdata.connect()
    params = load_params()
    if a.replay:
        Session(holdings, params, a.replay, 'replay').run_replay(a.until or '15:00:00')
    else:
        day = dt.date.today().strftime('%Y%m%d')
        Session(holdings, params, day, 'live').run_live(a.interval, a.until or '14:57:00')


if __name__ == '__main__':
    main()
