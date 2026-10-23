# coding:utf-8
"""每天把关注股票的 tick（3 秒快照 + 五档盘口）存下来。QMT 只给「当天」的 tick，昨天的就拿不到了，所以必须每天存。

    .venv313\\Scripts\\python.exe tools\\tick_archive.py                  # 盘中挂着：每 15 分钟存一次，14:55 后每分钟存，15:05 退出
    .venv313\\Scripts\\python.exe tools\\tick_archive.py --once           # 现在存一次就退出
    .venv313\\Scripts\\python.exe tools\\tick_archive.py --codes 600366 600436
    .venv313\\Scripts\\python.exe tools\\tick_archive.py --status         # 看已经存了哪些天

只在 .venv313 跑（用了 3.7+ 写法）。只读行情，不碰账户、不下单。

  * 关注列表：共享数据目录 ticks/codes.txt，一行一个代码（# 开头是注释），第一次跑自动生成。
  * 存法：ticks/<YYYYMMDD>/<代码>.csv.gz，一行一个 tick，五档拆成 bid1..bid5 / bidVol1.. 等列，
    其余字段原样保留（volume / amount / transactionNum 是当天累计值，单笔成交要自己做差分）。
  * 每次都整天重拉、覆盖写；新拉的行数比已存的少就不覆盖（防止桥半途出错把好数据冲掉）。
  * 什么时候拉都给全天（09:15 集合竞价起），所以中途开始、中途断了都不要紧，只要收盘前成功拉一次。
    15:00 后 QMT 不再驱动桥，最后一次在 14:59~15:0x 之间，收盘集合竞价那一笔可能赶不上，下一次也补不回来。
  * 两台机器共用数据目录：ticks/<日期>/.lock 记着哪台机器在存，另一台跳过（--force 强制）。
  * ticks/index.csv 记每天每只的行数、首尾时间，--status 看；盘中模式的输出另记 ticks/<日期>/run.log。
  * 每天自动跑：Windows 计划任务「qmtLocal_tick_archive」，工作日 09:20 启动（要开着电脑和大QMT、桥在运行）。
  * 收工后顺带跑 tools/rule_forward.py（观察中的规则记前向假账）和 tools/order_flow.py（资金足迹攒进累计表），
    失败都不影响存档。
"""
import argparse
import datetime as dt
import os
import socket
import sys
import time

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qmt_bridge.dotenv_lite import load_dotenv  # noqa: E402

load_dotenv()
DATA_DIR = os.environ.get('QMTLOCAL_DATA_DIR') or os.path.join(ROOT, 'real_data')
TICK_DIR = os.path.join(DATA_DIR, 'ticks')
CODES_FILE = os.path.join(TICK_DIR, 'codes.txt')
INDEX_FILE = os.path.join(TICK_DIR, 'index.csv')

DEFAULT_CODES = """# 每天存 tick 的代码，一行一个；# 后面是注释
600366.SH   # 宁波韵升
600436.SH   # 片仔癀
605208.SH
000001.SH   # 上证指数（大盘背景）
"""
BOOK = ['askPrice', 'askVol', 'bidPrice', 'bidVol']


def full_code(c):
    c = c.strip().upper()
    if '.' in c:
        return c
    return c + ('.SH' if c[0] in '659' else '.SZ')


def load_codes():
    os.makedirs(TICK_DIR, exist_ok=True)
    if not os.path.exists(CODES_FILE):
        with open(CODES_FILE, 'w', encoding='utf-8-sig') as f:
            f.write(DEFAULT_CODES)
    out = []
    with open(CODES_FILE, encoding='utf-8-sig') as f:
        for line in f:
            c = line.split('#')[0].strip()
            if c:
                out.append(full_code(c))
    return out


def flatten(df):
    """五档列表拆成 bid1..bid5 这种列；索引（stime）变成 stime 列。"""
    df = df.copy()
    for col in BOOK:
        if col not in df.columns:
            continue
        lv = df[col].apply(lambda v: list(v) if isinstance(v, (list, tuple)) else [])
        n = max(5, lv.map(len).max() or 0)
        name = {'askPrice': 'ask', 'bidPrice': 'bid', 'askVol': 'askVol', 'bidVol': 'bidVol'}[col]
        for i in range(n):
            df['%s%d' % (name, i + 1)] = lv.map(lambda v, i=i: v[i] if i < len(v) else None)
        df = df.drop(columns=[col])
    if 'stime' not in df.columns:
        df['stime'] = [str(i) for i in df.index]
    df['stime'] = df['stime'].astype(str).str.split('.').str[0]
    df = df.reset_index(drop=True)
    first = ['stime', 'lastPrice', 'volume', 'amount', 'transactionNum']
    return df[[c for c in first if c in df.columns] + [c for c in df.columns if c not in first]]


def existing_rows(path):
    if not os.path.exists(path):
        return 0
    try:
        return len(pd.read_csv(path, usecols=['stime']))
    except Exception:
        return 0


def lock_ok(day_dir, force):
    os.makedirs(day_dir, exist_ok=True)
    path = os.path.join(day_dir, '.lock')
    me = socket.gethostname()
    if os.path.exists(path) and not force:
        with open(path, encoding='utf-8') as f:
            owner = f.read().strip()
        if owner and owner != me:
            print('%s 已经由 %s 在存（%s），这台跳过；确定要存加 --force' % (os.path.basename(day_dir), owner, path))
            return False
    with open(path, 'w', encoding='utf-8') as f:
        f.write(me)
    return True


def update_index(rows):
    old = pd.read_csv(INDEX_FILE, dtype=str, encoding='utf-8-sig') if os.path.exists(INDEX_FILE) else None
    new = pd.DataFrame(rows).astype(str)
    if old is not None:
        keys = set(zip(new['日期'], new['代码']))
        old = old[[(d, c) not in keys for d, c in zip(old['日期'], old['代码'])]]
        new = pd.concat([old, new])
    new.sort_values(['日期', '代码']).to_csv(INDEX_FILE, index=False, encoding='utf-8-sig')


def save_once(xtdata, codes, day):
    day_dir = os.path.join(TICK_DIR, day)
    os.makedirs(day_dir, exist_ok=True)
    rows, msgs = [], []
    for c in codes:
        path = os.path.join(day_dir, '%s.csv.gz' % c)
        try:
            d = xtdata.get_market_data_ex([], [c], period='tick', start_time=day, end_time=day + '235959')
            df = d.get(c)
        except Exception as e:
            msgs.append('%s 失败(%s)' % (c, str(e).split('\n')[0][:60]))
            continue
        if df is None or not len(df):
            msgs.append('%s 0 条' % c)
            continue
        df = flatten(df)
        df = df[df['stime'].str[:8] == day]
        have = existing_rows(path)
        if len(df) < have:
            msgs.append('%s %d 条 < 已存 %d，不覆盖' % (c, len(df), have))
            continue
        tmp = path + '.tmp'
        df.to_csv(tmp, index=False, compression='gzip')
        os.replace(tmp, path)
        rows.append({'日期': day, '代码': c, '行数': len(df), '首': df['stime'].iloc[0][8:],
                     '尾': df['stime'].iloc[-1][8:], '存于': dt.datetime.now().strftime('%H:%M:%S'),
                     '机器': socket.gethostname()})
        msgs.append('%s %d 条 至%s' % (c, len(df), df['stime'].iloc[-1][8:14]))
    if rows:
        update_index(rows)
    print('[%s] %s' % (dt.datetime.now().strftime('%H:%M:%S'), '；'.join(msgs)), flush=True)
    return len(rows)


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


def connect():
    from qmt_bridge import xtdata
    xtdata.enable_hello = False
    for i in range(3):
        try:
            xtdata.connect()
            if i:                              # 重试成功也记一笔，不然 run.log 只剩那行「连桥失败」
                print('第 %d 次重试连上了' % i, flush=True)
            return xtdata
        except Exception as e:
            print('连桥失败（%s），%d 秒后重试' % (str(e).split('\n')[0], 10), flush=True)
            time.sleep(10)
    return None


def status():
    if not os.path.exists(INDEX_FILE):
        print('还没存过')
        return
    t = pd.read_csv(INDEX_FILE, dtype=str, encoding='utf-8-sig')
    pv = t.pivot_table(index='日期', columns='代码', values='行数', aggfunc='first').fillna('-')
    pd.set_option('display.width', 200)
    print(pv.to_string())
    tail = t[t['日期'] == t['日期'].max()]
    late = tail[tail['尾'] < '145700']
    if len(late):
        print('\n注意：%s 这几只最后一笔早于 14:57，可能没存全：%s' % (
            tail['日期'].iloc[0], ', '.join(late['代码'])))


def main():
    ap = argparse.ArgumentParser(description='每天存 tick')
    ap.add_argument('--codes', nargs='*', help='不用 codes.txt，直接给代码')
    ap.add_argument('--once', action='store_true', help='存一次就退出')
    ap.add_argument('--every', type=int, default=15, help='盘中几分钟存一次，默认 15')
    ap.add_argument('--until', default='15:05', help='几点退出，默认 15:05')
    ap.add_argument('--force', action='store_true', help='无视另一台机器的锁')
    ap.add_argument('--status', action='store_true')
    a = ap.parse_args()

    if a.status:
        status()
        return
    codes = [full_code(c) for c in a.codes] if a.codes else load_codes()
    day = dt.date.today().strftime('%Y%m%d')
    if dt.date.today().weekday() >= 5:
        print('周末，不存')
        return
    if not lock_ok(os.path.join(TICK_DIR, day), a.force):
        return
    if not a.once:                             # 计划任务跑的时候没有控制台，输出同时记到当天目录的 run.log
        sys.stdout = Tee(os.path.join(TICK_DIR, day, 'run.log'))
    print('存 %d 只到 %s：%s' % (len(codes), os.path.join(TICK_DIR, day), ' '.join(codes)), flush=True)

    xtdata = connect()
    if xtdata is None:
        sys.exit('桥连不上：确认大QMT「模型交易」里桥接服务在运行')
    if a.once:
        save_once(xtdata, codes, day)
        return

    last = 0.0
    while True:
        now = dt.datetime.now().strftime('%H:%M')
        if now >= a.until:
            break
        if now < '09:26' or '11:31' <= now < '13:00':
            time.sleep(30)
            continue
        gap = 60 if now >= '14:55' else a.every * 60
        if time.time() - last >= gap:
            try:
                save_once(xtdata, codes, day)
            except Exception as e:             # 桥断了就重连，下一轮再试
                print('存失败：%s，重连' % e, flush=True)
                xtdata = connect() or xtdata
            last = time.time()
        time.sleep(10)
    print('收工', flush=True)
    status()
    try:                                       # 观察中的规则用今天的 tick 记一笔前向假账（tools/rule_forward.py）
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import rule_forward
        print('规则前向记录：', flush=True)
        rule_forward.run(day)
    except Exception as e:
        print('规则前向记录失败（不影响 tick 存档）：%s' % e, flush=True)
    try:                                       # 资金足迹按 5 分钟攒进累计表（tools/order_flow.py）
        import order_flow
        order_flow.run(day)
    except Exception as e:
        print('资金足迹记录失败（不影响 tick 存档）：%s' % e, flush=True)


if __name__ == '__main__':
    main()
