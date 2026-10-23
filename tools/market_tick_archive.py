# coding:utf-8
"""全市场 tick 存档：订阅 QMT 全推（subscribe_whole_quote），把全部 A 股（沪深 + 京市）的 3 秒快照 + 五档存下来。

    .venv313\\Scripts\\python.exe tools\\market_tick_archive.py                 # 盘中挂着，15:02 收工、合并、质检、搬到 OneDrive
    .venv313\\Scripts\\python.exe tools\\market_tick_archive.py --test 30       # 压力测试：只录 30 分钟，结果留在本地不搬
    .venv313\\Scripts\\python.exe tools\\market_tick_archive.py --status        # 看已经存了哪些天

只在 .venv313 跑（用了 3.7+ 写法和 pyarrow）。只读行情，不碰账户、不下单。

跟 tools/tick_archive.py 分工（2026-10-23 定）：
  * tick_archive.py：指定的几十只，逐只整天重拉，完整性有保证 —— 实盘复盘、精确回测用。
  * 本脚本：全市场，QMT 推什么存什么，断线 / 桥卡住的那段永久缺 —— 大样本研究用，用之前看质检。

存法：
  * 白天写项目目录下 tick_tmp/<日期>/batch_<HHMMSS>.parquet（每分钟一批），不进 OneDrive，免得同步正在写的文件。
  * 收工后按半小时合并成 part_<HHMM>.parquet（按 代码、时间 排序、去重），连同 quality.json、run.log
    搬到共享数据目录 ticks_all/<日期>/，ticks_all/index.csv 记每天一行。--keep 保留本地临时文件。
  * 列：code, stime（YYYYMMDDHHMMSS）, lastPrice, open, high, low, lastClose, amount, volume（手，当天累计）,
    pvolume, transactionNum, stockStatus, ask1..5, askVol1..5, bid1..5, bidVol1..5。
  * 两台机器共用 ticks_all/<日期>/.lock，另一台跳过（--force 强制）。

质检（quality.json，收工时自动做）：
  * 跟 tick_archive.py 同一天存的那几只逐只比：09:30~14:57 的不同时间戳条数（全推 / 逐只拉）、最后累计成交量是否一致。
  * 每分钟收到多少行、多少只，0 行的分钟记成缺口；断线重连、主动让路的时段也记成缺口。
  * 桥服务端断开时会在 QMT 日志写「丢弃 N 条」（队列满了丢最老的），那是服务端的丢包，这里看不到，要去 QMT 日志看。

让路：另开一条连接每 30 秒 ping 桥，连续 2 次超过 --ping-max 秒（默认 2）就退订 2 分钟再重订，
  给 tools/t0_live.py 这些实盘程序让路；让路的时段记成缺口。
"""
import argparse
import datetime as dt
import json
import os
import shutil
import socket
import sys
import threading
import time

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qmt_bridge.dotenv_lite import load_dotenv  # noqa: E402

load_dotenv()
DATA_DIR = os.environ.get('QMTLOCAL_DATA_DIR') or os.path.join(ROOT, 'real_data')
OUT_DIR = os.path.join(DATA_DIR, 'ticks_all')
INDEX_FILE = os.path.join(OUT_DIR, 'index.csv')
TMP_DIR = os.path.join(ROOT, 'tick_tmp')
CURATED_DIR = os.path.join(DATA_DIR, 'ticks')          # tick_archive.py 的存档，质检对照用

SECTORS = ['沪深A股', '京市A股']
A_PREFIX = {'SH': ('60', '68'), 'SZ': ('00', '30'), 'BJ': ('4', '8', '92')}
SCALARS = ['lastPrice', 'open', 'high', 'low', 'lastClose', 'amount', 'volume', 'pvolume',
           'transactionNum', 'stockStatus']
BOOK = [('askPrice', 'ask'), ('askVol', 'askVol'), ('bidPrice', 'bid'), ('bidVol', 'bidVol')]
COLS = ['code', 'stime'] + SCALARS + ['%s%d' % (n, i) for _, n in BOOK for i in range(1, 6)]
START, PAUSE = '09:14:00', 120                          # 几点开始订阅；让路退订多少秒


def is_a_share(code):
    inst, _, mkt = code.partition('.')
    return len(inst) == 6 and inst.isdigit() and inst.startswith(A_PREFIX.get(mkt, ()))


def now_hms():
    return dt.datetime.now().strftime('%H:%M:%S')


class Tee(object):
    def __init__(self, path):
        self.f = open(path, 'a', encoding='utf-8')
        self.out = sys.stdout

    def write(self, s):
        self.f.write(s)
        self.f.flush()
        try:
            self.out.write(s)
        except Exception:
            pass

    def flush(self):
        self.f.flush()
        try:
            self.out.flush()
        except Exception:
            pass


def flat(code, t):
    """全推回调里的一条快照 -> 一行。"""
    row = [code, str(t.get('stime', '')).split('.')[0]] + [t.get(k) for k in SCALARS]
    for src, _ in BOOK:
        v = t.get(src) or []
        row += [v[i] if i < len(v) else None for i in range(5)]
    return row


# ---------------------------------------------------------------- 录制

class Recorder(object):

    def __init__(self, day, day_dir, ping_max):
        self.day, self.dir, self.ping_max = day, day_dir, ping_max
        self.lock = threading.Lock()
        self.buf = []
        self.codes = set()
        self.seq = None
        self.xt = None
        self.gaps = []                    # [开始, 结束, 原因]
        self.minutes = {}                 # HH:MM -> [行数, 只数]
        self.min_codes = set()
        self.cur_min = None
        self.rows_total = 0
        self.batches = 0
        self.pings = []                   # (时刻, 秒；超时记 None)
        self.paused_until = 0.0
        self.pause_from = None
        self.pinger = None                # 专门 ping 的常驻连接（每次新开会让 QMT 日志刷满「接入 / 断开」）

    # -- 回调：只往缓冲里塞，别的都不做（跑在桥的接收线程上）
    def on_data(self, datas):
        rows = [flat(c, t) for c, t in datas.items() if c in self.codes and t]
        if not rows:
            return
        m = now_hms()[:5]
        with self.lock:
            self.buf.extend(rows)
            if m != self.cur_min:
                if self.cur_min:
                    self.minutes[self.cur_min][1] = len(self.min_codes)
                self.cur_min, self.min_codes = m, set()
                self.minutes.setdefault(m, [0, 0])
            self.minutes[m][0] += len(rows)
            self.min_codes.update(r[0] for r in rows)

    def connect(self):
        from qmt_bridge import xtdata
        xtdata.enable_hello = False
        for i in range(3):
            try:
                xtdata.connect()
                if i:
                    print('第 %d 次重试连上了' % i, flush=True)
                self.xt = xtdata
                return True
            except Exception as e:
                print('连桥失败（%s），10 秒后重试' % str(e).split('\n')[0], flush=True)
                time.sleep(10)
        return False

    def load_codes(self):
        codes = []
        for s in SECTORS:
            try:
                got = [c for c in (self.xt.get_stock_list_in_sector(s) or []) if is_a_share(c)]
            except Exception as e:
                got = []
                print('取板块「%s」失败：%s' % (s, e), flush=True)
            print('板块「%s」%d 只' % (s, len(got)), flush=True)
            codes += got
        self.codes = set(codes)
        return len(self.codes)

    def subscribe(self):
        """先按代码列表订；QMT 不认就按市场订，回调里按代码过滤。"""
        for arg in (sorted(self.codes), ['SH', 'SZ', 'BJ'], ['SH', 'SZ']):
            try:
                self.seq = self.xt.subscribe_whole_quote(arg, callback=self.on_data)
                print('%s 订阅全推（%s），订阅号 %s' % (
                    now_hms(), '%d 个代码' % len(arg) if len(arg) > 3 else '市场 ' + ','.join(arg), self.seq), flush=True)
                return True
            except Exception as e:
                print('订阅全推失败（%s）：%s' % ('代码列表' if len(arg) > 3 else ','.join(arg), str(e)[:100]), flush=True)
        return False

    def unsubscribe(self):
        if self.seq is None:
            return
        try:
            self.xt.unsubscribe_quote(self.seq)
        except Exception:
            pass
        self.seq = None

    def alive(self):
        b = getattr(self.xt, '_bridge', None)
        return b is not None and b.alive

    # -- 写盘：每分钟一批
    def flush(self):
        with self.lock:
            rows, self.buf = self.buf, []
        if not rows:
            return 0
        df = pd.DataFrame(rows, columns=COLS)
        self.batches += 1
        df.to_parquet(os.path.join(self.dir, 'batch_%s_%05d.parquet' % (
            dt.datetime.now().strftime('%H%M%S'), self.batches)), index=False, compression='zstd')
        self.rows_total += len(df)
        return len(df)

    # -- 另开一条连接 ping，量桥的响应（跟全推的连接分开，不被推送数据排队拖慢）
    def ping(self):
        from qmt_bridge.xtdata import _Bridge, DEFAULT_HOST, DEFAULT_PORT
        sec = None
        try:
            if self.pinger is None or not self.pinger.alive:
                self.pinger = _Bridge(DEFAULT_HOST, DEFAULT_PORT)
                self.pinger.connect()
            t0 = time.time()
            self.pinger.call('ping', timeout=self.ping_max * 3)
            sec = time.time() - t0
        except Exception:
            pass
        self.pings.append((now_hms(), sec))
        return sec

    def run(self, until):
        last_flush = last_ping = time.time()
        slow = 0
        down_since = None
        while now_hms() < until:
            time.sleep(1)
            t = time.time()
            # 断线：记缺口、重连、重订
            if self.seq is not None and not self.alive():
                down_since = down_since or now_hms()
                print('%s 桥断了，重连' % now_hms(), flush=True)
                self.seq = None
                if self.connect() and self.subscribe():
                    self.gaps.append([down_since, now_hms(), '断线重连'])
                    down_since = None
                continue
            # 让路结束：重订
            if self.seq is None and self.paused_until and t >= self.paused_until:
                if self.subscribe():
                    self.gaps.append([self.pause_from, now_hms(), '桥慢，让路'])
                    self.paused_until = 0.0
            if t - last_flush >= 60:
                n = self.flush()
                last_flush = t
                m = (dt.datetime.now() - dt.timedelta(minutes=1)).strftime('%H:%M')
                got = self.minutes.get(m, [0, 0])
                print('%s 写入 %d 行；%s 这一分钟 %d 行 %d 只；累计 %d 行' % (
                    now_hms(), n, m, got[0], got[1] or len(self.min_codes), self.rows_total), flush=True)
            if t - last_ping >= 30:
                last_ping = t
                sec = self.ping()
                if sec is None or sec > self.ping_max:
                    slow += 1
                    print('%s ping %s' % (now_hms(), '超时' if sec is None else '%.2f 秒' % sec), flush=True)
                else:
                    slow = 0
                if slow >= 2 and self.seq is not None:
                    print('%s 桥连续 %d 次慢，退订 %d 秒给实盘让路' % (now_hms(), slow, PAUSE), flush=True)
                    self.unsubscribe()
                    self.pause_from = now_hms()
                    self.paused_until = t + PAUSE
                    slow = 0
        self.unsubscribe()
        self.flush()
        if self.pinger is not None:
            self.pinger.close()
        if self.cur_min:
            self.minutes[self.cur_min][1] = len(self.min_codes)


# ---------------------------------------------------------------- 收工：合并、质检、搬家

def merge(day_dir):
    """batch_*.parquet 按半小时合并成 part_<HHMM>.parquet。"""
    batches = sorted(f for f in os.listdir(day_dir) if f.startswith('batch_'))
    if not batches:
        return []
    df = pd.concat([pd.read_parquet(os.path.join(day_dir, f)) for f in batches], ignore_index=True)
    df = df.drop_duplicates(['code', 'stime'], keep='last')
    hm = df['stime'].str[8:12]
    df['part'] = hm.str[:2] + hm.str[2:4].astype(int).floordiv(30).mul(30).astype(str).str.zfill(2)
    parts = []
    for p, g in df.groupby('part'):
        g = g.drop(columns='part').sort_values(['code', 'stime'])
        name = 'part_%s.parquet' % p
        g.to_parquet(os.path.join(day_dir, name), index=False, compression='zstd')
        parts.append((name, len(g), g['code'].nunique()))
    for f in batches:
        os.remove(os.path.join(day_dir, f))
    return parts


def quality(day, day_dir, rec):
    q = {'日期': day, '代码数_订阅': len(rec.codes), '行数': 0, '只数': 0, '对照': [], '缺口': rec.gaps,
         '空分钟': [], 'ping': {}}
    parts = sorted(f for f in os.listdir(day_dir) if f.startswith('part_'))
    if not parts:
        return q
    df = pd.concat([pd.read_parquet(os.path.join(day_dir, f)) for f in parts], ignore_index=True)
    q['行数'], q['只数'] = len(df), int(df['code'].nunique())
    # 录制时段内（交易时间）一行都没收到的分钟；中途开始 / 压力测试只看录了的那段
    have = set(df['stime'].str[8:12])
    trade = [m for m in pd.date_range(day + ' 09:30', day + ' 14:56', freq='1min').strftime('%H%M')
             if not ('1130' <= m < '1300')]
    inside = [m for m in trade if m in have]
    if inside:
        q['空分钟'] = [m for m in trade if inside[0] <= m <= inside[-1] and m not in have]
    # 跟 tick_archive 逐只比
    cdir = os.path.join(CURATED_DIR, day)
    if os.path.isdir(cdir):
        for f in sorted(os.listdir(cdir)):
            if not f.endswith('.csv.gz'):
                continue
            code = f[:-7]
            if code not in rec.codes:
                continue
            c = pd.read_csv(os.path.join(cdir, f), dtype={'stime': str}, usecols=['stime', 'volume'])
            c['stime'] = c['stime'].str.split('.').str[0]
            mine = df[df['code'] == code]
            # 只比两边都录到的那段（压力测试只录半小时；tick_archive 白天每 15 分钟才存一次）
            lo = max(mine['stime'].min() if len(mine) else '9', c['stime'].min(), day + '093000')
            hi = min(mine['stime'].max() if len(mine) else '0', c['stime'].max(), day + '145700')
            ca = c[(c['stime'] >= lo) & (c['stime'] <= hi)]
            mb = mine[(mine['stime'] >= lo) & (mine['stime'] <= hi)]
            a, b = ca['stime'].nunique(), mb['stime'].nunique()
            same = sorted(set(ca['stime']) & set(mb['stime']))
            vol_ok = None
            if same:                                       # 同一个时间戳上累计量是不是一样
                t = same[-1]
                vol_ok = float(ca.loc[ca['stime'] == t, 'volume'].iloc[-1]) == \
                    float(mb.loc[mb['stime'] == t, 'volume'].iloc[-1])
            q['对照'].append({'代码': code, '时段': '%s~%s' % (lo[8:], hi[8:]), '逐只条数': int(a),
                            '全推条数': int(b), '覆盖%': round(100.0 * b / a, 1) if a else None,
                            '全推多出': int(len(set(mb['stime']) - set(ca['stime']))),
                            '同时刻累计量一致': vol_ok})
    s = [p for _, p in rec.pings if p is not None]
    q['ping'] = {'次数': len(rec.pings), '超时': sum(1 for _, p in rec.pings if p is None),
                 '中位秒': round(sorted(s)[len(s) // 2], 3) if s else None, '最大秒': round(max(s), 3) if s else None}
    q['每分钟'] = {m: v for m, v in sorted(rec.minutes.items())}
    return q


def print_quality(q):
    print('质检：%d 行，%d 只（订阅 %d 只）' % (q['行数'], q['只数'], q['代码数_订阅']))
    for r in q['对照']:
        print('  %s %s 逐只 %d 条 / 全推 %d 条，覆盖 %s%%，全推多出 %d 条，同时刻累计量%s' % (
            r['代码'], r['时段'], r['逐只条数'], r['全推条数'], r['覆盖%'], r['全推多出'],
            {True: '一致', False: '不一致', None: '无共同时刻'}[r['同时刻累计量一致']]))
    print('  缺口：%s' % (q['缺口'] or '无'))
    print('  交易时段空分钟：%d 个%s' % (len(q['空分钟']), ' ' + ','.join(q['空分钟'][:20]) if q['空分钟'] else ''))
    print('  ping：%s' % q['ping'])


def finish(day, day_dir, rec, keep, move):
    print('%s 收工，合并 ……' % now_hms(), flush=True)
    parts = merge(day_dir)
    for name, n, k in parts:
        print('  %s %d 行 %d 只' % (name, n, k))
    q = quality(day, day_dir, rec)
    with open(os.path.join(day_dir, 'quality.json'), 'w', encoding='utf-8') as f:
        json.dump(q, f, ensure_ascii=False, indent=1)
    print_quality(q)
    mb = sum(os.path.getsize(os.path.join(day_dir, f)) for f in os.listdir(day_dir)) / 1e6
    print('  本地 %.0f MB：%s' % (mb, day_dir))
    if not move:
        return
    dst = os.path.join(OUT_DIR, day)
    os.makedirs(dst, exist_ok=True)
    for f in os.listdir(day_dir):
        if f.startswith('part_') or f in ('quality.json',):
            (shutil.copy2 if keep else shutil.move)(os.path.join(day_dir, f), os.path.join(dst, f))
    row = {'日期': day, '行数': q['行数'], '只数': q['只数'], '文件数': len(parts), 'MB': round(mb),
           '缺口段数': len(q['缺口']), '空分钟': len(q['空分钟']),
           '对照最低覆盖%': min([r['覆盖%'] for r in q['对照'] if r['覆盖%'] is not None], default=None),
           '机器': socket.gethostname()}
    old = pd.read_csv(INDEX_FILE, dtype=str, encoding='utf-8-sig') if os.path.exists(INDEX_FILE) else None
    new = pd.DataFrame([row]).astype(str)
    if old is not None:
        new = pd.concat([old[old['日期'] != day], new])
    new.sort_values('日期').to_csv(INDEX_FILE, index=False, encoding='utf-8-sig')
    print('  已搬到 %s' % dst)


# ---------------------------------------------------------------- 入口

def lock_ok(day, force):
    path = os.path.join(OUT_DIR, day, '.lock')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    me = socket.gethostname()
    if os.path.exists(path) and not force:
        with open(path, encoding='utf-8') as f:
            owner = f.read().strip()
        if owner and owner != me:
            print('%s 已经由 %s 在录（%s），这台跳过；确定要录加 --force' % (day, owner, path))
            return False
    with open(path, 'w', encoding='utf-8') as f:
        f.write(me)
    return True


def status():
    if not os.path.exists(INDEX_FILE):
        print('还没存过')
        return
    print(pd.read_csv(INDEX_FILE, dtype=str, encoding='utf-8-sig').to_string(index=False))


def main():
    ap = argparse.ArgumentParser(description='全市场 tick 存档（订阅全推）')
    ap.add_argument('--until', default='15:02:00', help='几点收工，默认 15:02:00')
    ap.add_argument('--test', type=int, metavar='分钟', help='压力测试：现在开始只录这么多分钟，结果留在本地不搬')
    ap.add_argument('--ping-max', type=float, default=2.0, help='ping 超过几秒算桥慢，默认 2')
    ap.add_argument('--keep', action='store_true', help='搬到 OneDrive 后本地也留一份')
    ap.add_argument('--force', action='store_true', help='无视另一台机器的锁')
    ap.add_argument('--status', action='store_true')
    a = ap.parse_args()
    if a.status:
        status()
        return

    today = dt.date.today()
    day = today.strftime('%Y%m%d')
    if today.weekday() >= 5:
        print('周末，不录')
        return
    if not a.test and not lock_ok(day, a.force):
        return
    day_dir = os.path.join(TMP_DIR, day + ('_test' if a.test else ''))
    os.makedirs(day_dir, exist_ok=True)
    sys.stdout = Tee(os.path.join(day_dir, 'run.log'))
    until = ((dt.datetime.now() + dt.timedelta(minutes=a.test)).strftime('%H:%M:%S') if a.test else a.until)
    print('== %s 全市场 tick 存档%s，%s 收工，临时目录 %s ==' % (
        day, '（压力测试 %d 分钟）' % a.test if a.test else '', until, day_dir), flush=True)

    rec = Recorder(day, day_dir, a.ping_max)
    if not rec.connect():
        sys.exit('桥连不上：确认大QMT「模型交易」里桥接服务在运行')
    if not rec.load_codes():
        sys.exit('一只 A 股代码都没取到')
    while not a.test and now_hms() < START:
        time.sleep(20)
    if not rec.subscribe():
        sys.exit('订阅全推失败')
    try:
        rec.run(until)
    except KeyboardInterrupt:
        print('手动中断', flush=True)
        rec.unsubscribe()
        rec.flush()
    finish(day, day_dir, rec, keep=a.keep, move=not a.test)


if __name__ == '__main__':
    main()
