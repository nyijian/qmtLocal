# coding:utf-8
"""用 akshare 下载财务、龙虎榜、融资融券、涨停池、ETF 数据到 real_data/akshare/，增量更新。

**只在 .venv313 下运行**（akshare 需要新版 Python），可以用 3.6 装不上的语法。

    .venv313\\Scripts\\python.exe tools\\fetch_akshare.py                      # 四类都下：近 5 年，两融个股明细近 3 年，涨停池近 30 天
    .venv313\\Scripts\\python.exe tools\\fetch_akshare.py finance lhb          # 只下其中几类
    .venv313\\Scripts\\python.exe tools\\fetch_akshare.py margin --detail-years 5   # 两融明细也要 5 年
    .venv313\\Scripts\\python.exe tools\\fetch_akshare.py zt_pool --zt-days 60     # 涨停池多拉点天数
    .venv313\\Scripts\\python.exe tools\\fetch_akshare.py etf --etf-years 10       # ETF 历史行情（不在默认四类里，单独跑）
    .venv313\\Scripts\\python.exe tools\\fetch_akshare.py --force              # 已有的也重下

落盘位置（CSV，UTF-8 带 BOM，Excel 能直接打开）。根目录取 .env 里的 QMTLOCAL_DATA_DIR
（放在 OneDrive 里，两台机器共用；见 docs/01-环境搭建.md），没配就用项目里的 real_data/：

    <QMTLOCAL_DATA_DIR>/akshare/
      finance/yjbb/20260630.csv     业绩报表（每股收益、营收、净利润及同比、ROE、每股现金流…）
      finance/zcfz/20260630.csv     资产负债表摘要
      finance/lrb/20260630.csv      利润表摘要
      finance/xjll/20260630.csv     现金流量表摘要
      lhb/2026-09.csv               龙虎榜明细，一个月一个文件（同一只股票同一天可能因不同上榜原因出现多行）
      lhb_jg/2026-09.csv            机构专用席位买卖：每只上榜股的机构买入/卖出额、买卖家数
      lhb_yyb/2026-09.csv           活跃营业部：每个营业部每天的买入/卖出额（含沪股通/深股通专用）
      margin/summary_sse.csv        沪市两融汇总，每天一行
      margin/summary_szse.csv       深市两融汇总，每天一行
      margin/detail_sse/20260924.csv   沪市两融个股明细，一天一个文件
      margin/detail_szse/20260924.csv  深市两融个股明细
      zt_pool/20260924.csv          涨停股池，一天一个文件（含连板数、所属行业等）
      zt_pool_zbgc/20260924.csv     炸板股池，一天一个文件
      etf/etf_list.csv              全市场 ETF 代码/名称/最新价快照（新浪），只存当前这一份，不按日期存
      etf_hist/510300.csv           单只 ETF 近 --etf-years 年的日线行情（开高低收、成交量额、换手率），一只一个文件

读回来时股票代码要按字符串读，否则前导 0 会丢：
    pd.read_csv(path, dtype={'股票代码': str})     # 龙虎榜是 '代码'，两融明细是 '标的证券代码' / '证券代码'

增量规则（重跑只补缺的，中途 Ctrl+C 也不会留下半截文件）：
  * 财务：报告期截止后 4 个月内（年报 4 月底才披露完）每次都重下，过了这个窗口、文件已存在就跳过。
  * 龙虎榜：当月每次都重下，已经结束的月份文件存在就跳过。
  * 两融明细/深市汇总：按交易日逐日下，文件或日期已存在就跳过。当天的数据要到下一个交易日才公布，只下到昨天。
  * 沪市汇总：一次请求能取整段，每次都整段重下。
  * 涨停池/炸板池：按交易日逐日下，文件存在且不是当天就跳过；当天的文件每次都重下
    （收盘前查到的是半天的数据，不能当成最终结果存死）。只回看 `--zt-days` 天，不是 `--years`
    那几年——这是给近两周热点扫描用的，没必要囤历史。
  * ETF：列表每次都重新存一份（当前快照，不是历史）。每只 ETF 的历史行情整只重下
    （`fund_etf_hist_em` 一次请求就能拿整段，不是逐日接口，不用管"补缺"），但本地文件
    最后一天如果离今天不超过 3 天就跳过，省得每次都把全部 ETF 重新拉一遍。
    **ETF 的"份额"没有历史查询接口，这里不下**——akshare 只有
    `fund_etf_spot_em` 这个当前快照能看最新份额，要跟踪份额变化只能从现在开始
    每天存一次快照自己攒，这个单独算一类，还没做。
失败的请求会重试 3 次，还不行就记下来继续，最后汇总打印；再跑一次就会补上。

网络：
  * 默认**不走代理**（这些都是国内站点）。本机开着系统代理时，深交所经代理会报 ProxyError；要走代理加 --use-proxy。
  * 深交所有反爬，请求太密会被临时封（表现是连接被直接断开）。深交所的请求单独放慢（--szse-sleep，默认 3 秒）；
    同一类连续失败 5 次就停掉这一类，提示稍后重跑——被封时硬重试只会越封越久。

两台机器共用 OneDrive 目录（谁用谁下，不做强制限制）：
  * 两台同时跑会让 OneDrive 生成「文件名-机器名.csv」冲突副本。开跑时如果发现另一台 3 小时内跑过，会打印警告（不拦）；
    跑完会扫一遍冲突副本，有就列出来，手动删掉多余的那份即可。
  * 每次运行的机器、起止时间记在 _sync/last_run.json。
  * OneDrive 上传时会短暂锁文件，替换文件遇到 PermissionError 会重试几次。
"""
import argparse
import datetime as dt
import json
import os
import re
import socket
import sys
import time
import warnings

warnings.filterwarnings('ignore')        # akshare 内部的 pandas FutureWarning 刷屏

import akshare as ak
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from qmt_bridge.dotenv_lite import load_dotenv  # noqa: E402

load_dotenv()
DATA_DIR = os.environ.get('QMTLOCAL_DATA_DIR') or os.path.join(ROOT, 'real_data')
OUT = os.path.join(DATA_DIR, 'akshare')
SYNC = os.path.join(DATA_DIR, '_sync')
HOST = socket.gethostname()
OTHER_HOST_WINDOW = 3 * 3600      # 另一台机器多久以内跑过就提醒

FINANCE_TABLES = {
    'yjbb': ak.stock_yjbb_em,     # 业绩报表
    'zcfz': ak.stock_zcfz_em,     # 资产负债表
    'lrb': ak.stock_lrb_em,       # 利润表
    'xjll': ak.stock_xjll_em,     # 现金流量表
}
FINANCE_FINAL_AFTER = 120         # 报告期结束多少天后认为披露完毕、不再重下
BREAKER_LIMIT = 5                 # 同一类连续失败几次就放弃这一类

failures: list[str] = []


class Blocked(Exception):
    """同一类请求连续失败太多次，多半是被限流了。"""


class Breaker:
    def __init__(self, name: str):
        self.name = name
        self.streak = 0

    def record(self, ok: bool) -> None:
        self.streak = 0 if ok else self.streak + 1
        if self.streak >= BREAKER_LIMIT:
            raise Blocked('%s 连续失败 %d 次，可能被限流了，这一类先停下，过一阵再重跑'
                          % (self.name, self.streak))


# ---------------------------------------------------------------- 工具

def log(msg: str) -> None:
    print('%s %s' % (dt.datetime.now().strftime('%H:%M:%S'), msg), flush=True)


def fetch(label: str, fn, *, retries: int = 3, pause: float = 0.5,
          breaker: Breaker | None = None) -> pd.DataFrame | None:
    """调接口，失败重试（间隔递增）；最终失败记入 failures 返回 None。"""
    for attempt in range(1, retries + 1):
        try:
            df = fn()
            time.sleep(pause)
            if breaker:
                breaker.record(True)
            return df
        except Exception as e:
            if attempt == retries:
                failures.append('%s: %s: %s' % (label, type(e).__name__, str(e)[:120]))
                log('  失败 %s（%s）' % (label, type(e).__name__))
                if breaker:
                    breaker.record(False)
                return None
            time.sleep(max(pause, 1) * 4 * attempt)
    return None


def save(df: pd.DataFrame, path: str) -> None:
    """先写临时文件再替换，中断时不会留下半截 CSV。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df = df.drop(columns=['序号'], errors='ignore')
    tmp = path + '.tmp'
    df.to_csv(tmp, index=False, encoding='utf-8-sig')
    for attempt in range(6):              # OneDrive 正在上传时会短暂锁住目标文件
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(1 + attempt)


# ---------------------------------------------------------------- 多机共用

def _read_last_run() -> dict:
    try:
        with open(os.path.join(SYNC, 'last_run.json'), encoding='utf-8-sig') as f:   # 手工编辑过可能带 BOM
            return json.load(f)
    except (OSError, ValueError):
        return {}


def warn_if_other_host_active() -> None:
    last = _read_last_run()
    if not last or last.get('host') == HOST:
        return
    started = last.get('started_ts', 0)
    if last.get('status') == 'running' or time.time() - started < OTHER_HOST_WINDOW:
        log('！另一台机器 %s 在 %s 跑过下载（状态：%s）。两台同时跑会产生 OneDrive 冲突副本，'
            '确认它已经跑完再继续。' % (last.get('host'), last.get('started'), last.get('status')))


def write_last_run(status: str, kinds: list[str], started: float) -> None:
    os.makedirs(SYNC, exist_ok=True)
    info = {
        'host': HOST, 'status': status, 'kinds': kinds,
        'started': dt.datetime.fromtimestamp(started).strftime('%Y-%m-%d %H:%M:%S'),
        'started_ts': started,
        'finished': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S') if status != 'running' else None,
        'failures': len(failures),
    }
    tmp = os.path.join(SYNC, 'last_run.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(SYNC, 'last_run.json'))


# 正常文件名：YYYYMMDD.csv、YYYY-MM.csv、summary_sse.csv 这种；OneDrive 冲突副本会在后面加「-机器名」
_NORMAL_NAME = re.compile(r'^(\d{8}|\d{4}-\d{2}|summary_s[sz]{1,2}e)\.csv$')


def find_conflict_copies() -> list[str]:
    out = []
    for dp, _, files in os.walk(OUT):
        for fn in files:
            if fn.endswith('.csv') and not _NORMAL_NAME.match(fn):
                out.append(os.path.relpath(os.path.join(dp, fn), DATA_DIR))
    return sorted(out)


_calendar: list[dt.date] = []


def trading_days(start: dt.date, end: dt.date) -> list[dt.date]:
    if not _calendar:
        cal = fetch('交易日历', ak.tool_trade_date_hist_sina)
        if cal is None:
            raise SystemExit('取不到交易日历，检查网络后重试。')
        _calendar.extend(pd.to_datetime(cal['trade_date']).dt.date)
    return [d for d in _calendar if start <= d <= end]


def report_periods(start: dt.date, end: dt.date) -> list[dt.date]:
    out = []
    for y in range(start.year, end.year + 1):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            p = dt.date(y, m, d)
            if start <= p <= end:
                out.append(p)
    return out


# ---------------------------------------------------------------- 三类数据

def fetch_finance(start: dt.date, today: dt.date, force: bool, pause: float) -> None:
    periods = report_periods(start, today)
    log('财务：%d 个报告期 × %d 张表（%s ~ %s）' % (len(periods), len(FINANCE_TABLES), periods[0], periods[-1]))
    for p in periods:
        still_changing = (today - p).days <= FINANCE_FINAL_AFTER
        for name, fn in FINANCE_TABLES.items():
            path = os.path.join(OUT, 'finance', name, p.strftime('%Y%m%d') + '.csv')
            if os.path.exists(path) and not force and not still_changing:
                continue
            df = fetch('财务 %s %s' % (name, p), lambda fn=fn: fn(date=p.strftime('%Y%m%d')), pause=pause)
            if df is not None and len(df):
                save(df, path)
                log('  %s %s：%d 行' % (name, p, len(df)))


LHB_TABLES = {
    'lhb': ('龙虎榜', ak.stock_lhb_detail_em),          # 个股上榜明细（含东财「解读」标签、上榜后涨跌）
    'lhb_jg': ('机构买卖', ak.stock_lhb_jgmmtj_em),      # 每只上榜股的机构专用席位买卖金额、家数
    'lhb_yyb': ('活跃营业部', ak.stock_lhb_hyyyb_em),    # 每个营业部每天的买卖金额（含沪深股通专用）
}


def fetch_lhb(start: dt.date, today: dt.date, force: bool, pause: float) -> None:
    months = pd.period_range(start, today, freq='M')
    log('龙虎榜：%d 个月（%s ~ %s）× %d 张表' % (len(months), months[0], months[-1], len(LHB_TABLES)))
    for folder, (label, fn) in LHB_TABLES.items():
        for m in months:
            path = os.path.join(OUT, folder, '%s.csv' % m)
            current = m == pd.Period(today, freq='M')
            if os.path.exists(path) and not force and not current:
                continue
            a = max(m.start_time.date(), start)
            b = min(m.end_time.date(), today)
            if not trading_days(a, b):          # 区间里没有交易日时 akshare 会抛 TypeError
                continue
            df = fetch('%s %s' % (label, m), lambda fn=fn: fn(
                start_date=a.strftime('%Y%m%d'), end_date=b.strftime('%Y%m%d')), pause=pause)
            if df is not None and len(df):
                save(df, path)
                log('  %s %s：%d 行' % (label, m, len(df)))


def fetch_margin(start: dt.date, detail_start: dt.date, today: dt.date, force: bool,
                 pause: float, szse_pause: float) -> None:
    """汇总从 start 起；个股明细从 detail_start 起（明细一天一次请求，年限单独控制）。"""
    yesterday = today - dt.timedelta(days=1)
    days = trading_days(start, yesterday)
    detail_days = [d for d in days if d >= detail_start]
    log('两融：汇总 %d 个交易日（%s ~ %s），个股明细 %d 个交易日（%s 起）'
        % (len(days), days[0], days[-1], len(detail_days), detail_days[0]))

    # 沪市汇总：一次取整段
    df = fetch('两融汇总 沪', lambda: ak.stock_margin_sse(
        start_date=days[0].strftime('%Y%m%d'), end_date=days[-1].strftime('%Y%m%d')), pause=pause)
    if df is not None and len(df):
        save(df.sort_values('信用交易日期'), os.path.join(OUT, 'margin', 'summary_sse.csv'))
        log('  沪市汇总：%d 天' % len(df))

    # 每一段各自熔断：深交所被封了，不耽误上交所那段接着下
    for section, span in ((_szse_summary, days), (_detail_sse, detail_days), (_detail_szse, detail_days)):
        try:
            section(span, force, pause, szse_pause)
        except Blocked as e:
            log('  ！' + str(e))
            failures.append(str(e))


def _szse_summary(days, force, pause, szse_pause) -> None:
    """深市汇总：接口只能逐日取，攒到一个文件里。"""
    sz_path = os.path.join(OUT, 'margin', 'summary_szse.csv')
    sz = pd.read_csv(sz_path, dtype={'日期': str}) if os.path.exists(sz_path) and not force else pd.DataFrame()
    have = set(sz['日期']) if len(sz) else set()
    todo = [d for d in days if d.strftime('%Y%m%d') not in have]
    log('  深市汇总：还差 %d 天' % len(todo))
    breaker = Breaker('深市两融汇总')
    rows = []
    try:
        for d in todo:
            key = d.strftime('%Y%m%d')
            r = fetch('两融汇总 深 %s' % key, lambda: ak.stock_margin_szse(date=key),
                      pause=szse_pause, breaker=breaker)
            if r is not None and len(r):
                rows.append(r.assign(日期=key))
            if len(rows) >= 50:                  # 定期落盘，中断了也不白跑
                sz = _merge_szse(sz, rows, sz_path)
                rows = []
    finally:
        if rows:
            sz = _merge_szse(sz, rows, sz_path)
        log('  深市汇总：共 %d 天' % len(sz))


def _detail_sse(days, force, pause, szse_pause) -> None:
    _detail('sse', ak.stock_margin_detail_sse, days, force, pause)


def _detail_szse(days, force, pause, szse_pause) -> None:
    _detail('szse', ak.stock_margin_detail_szse, days, force, szse_pause)


def _detail(market: str, fn, days, force: bool, pause: float) -> None:
    """个股明细：逐日一个文件。"""
    folder = os.path.join(OUT, 'margin', 'detail_' + market)
    todo = [d for d in days if force or not os.path.exists(
        os.path.join(folder, d.strftime('%Y%m%d') + '.csv'))]
    log('  %s 明细：还差 %d 天' % (market, len(todo)))
    breaker = Breaker('%s 两融明细' % market)
    done = 0
    try:
        for i, d in enumerate(todo, 1):
            key = d.strftime('%Y%m%d')
            df = fetch('两融明细 %s %s' % (market, key), lambda: fn(date=key),
                       pause=pause, breaker=breaker)
            if df is not None and len(df):
                save(df, os.path.join(folder, key + '.csv'))
                done += 1
            if i % 50 == 0:
                log('    %s 明细进度 %d/%d' % (market, i, len(todo)))
    finally:
        log('  %s 明细：新下 %d 天' % (market, done))


def _merge_szse(sz: pd.DataFrame, rows: list, path: str) -> pd.DataFrame:
    sz = pd.concat([sz] + rows, ignore_index=True)
    cols = ['日期'] + [c for c in sz.columns if c != '日期']
    sz = sz[cols].drop_duplicates('日期', keep='last').sort_values('日期')
    save(sz, path)
    return sz


# ---------------------------------------------------------------- 涨停/炸板池

def _fetch_zbgc(key: str, pause: float) -> object:
    """炸板股池只认"最近一段交易日"，查太老的日子必定报 ValueError（实测大概 25 个
    交易日左右，akshare 没写死数字、也没必要帮它记一个可能随版本变的魔数）。
    返回 df；'cutoff' 表示踩到这个硬限制，调用方据此停手，别当成普通失败重试——
    重试没用，只会白白再报 3 次错。"""
    for attempt in range(1, 3):
        try:
            df = ak.stock_zt_pool_zbgc_em(date=key)
            time.sleep(pause)
            return df
        except ValueError as e:
            if '交易日' in str(e):
                return 'cutoff'
            if attempt == 2:
                failures.append('炸板池 %s: ValueError: %s' % (key, str(e)[:120]))
                log('  失败 炸板池 %s（ValueError）' % key)
                return None
            time.sleep(pause)
        except Exception as e:
            if attempt == 2:
                failures.append('炸板池 %s: %s: %s' % (key, type(e).__name__, str(e)[:120]))
                log('  失败 炸板池 %s（%s）' % (key, type(e).__name__))
                return None
            time.sleep(pause * 2)
    return None


def fetch_zt_pool(zt_days: int, today: dt.date, force: bool, pause: float) -> None:
    """涨停池 + 炸板池：逐日一个文件。只回看 zt_days 天——这俩是给近两周热点扫描用的，
    不是财务/两融那种要囤多年历史的数据，默认窗口小很多。"""
    start = today - dt.timedelta(days=int(zt_days * 1.6))   # 多留一截，扣掉周末/假期还够 zt_days 个交易日
    days = trading_days(start, today)[-zt_days:] if zt_days else trading_days(start, today)
    if not days:
        log('涨停池：回看区间里没有交易日')
        return
    log('涨停池/炸板池：%d 个交易日（%s ~ %s）' % (len(days), days[0], days[-1]))
    breaker_zt = Breaker('涨停池')
    done_zt = done_zb = 0

    # 涨停池没有"最近N天"的限制，顺着时间正常补。
    try:
        for d in days:
            key = d.strftime('%Y%m%d')
            current = d == today     # 当天收盘前查到的是半天数据，每次都重下，不能当成最终结果存死
            zt_path = os.path.join(OUT, 'zt_pool', key + '.csv')
            if force or current or not os.path.exists(zt_path):
                df = fetch('涨停池 %s' % key, lambda key=key: ak.stock_zt_pool_em(date=key),
                           pause=pause, breaker=breaker_zt)
                if df is not None and len(df):
                    save(df, zt_path)
                    done_zt += 1
    except Blocked as e:
        log('  ！' + str(e))
        failures.append(str(e))

    # 炸板池反过来从最近的日子往回补，一碰到"查太老"的硬限制就停——
    # 顺着时间正着查的话，会把limit之外的每一天都当成失败重试一遍，白费请求。
    for d in reversed(days):
        key = d.strftime('%Y%m%d')
        current = d == today
        zb_path = os.path.join(OUT, 'zt_pool_zbgc', key + '.csv')
        if not (force or current or not os.path.exists(zb_path)):
            continue
        result = _fetch_zbgc(key, pause)
        if isinstance(result, str) and result == 'cutoff':
            log('  炸板池接口只认最近一段交易日，%s 及更早的查不到，这是 akshare 接口的硬限制，不是故障，剩下的天数跳过'
                % key)
            break
        if result is not None and len(result):
            save(result, zb_path)
            done_zb += 1

    log('  涨停池：新下/更新 %d 天；炸板池：新下/更新 %d 天' % (done_zt, done_zb))


# ---------------------------------------------------------------- ETF

def _etf_hist_is_fresh(path: str, today: dt.date) -> bool:
    """本地文件最后一天离今天不超过 3 天（容忍周末/假期）就算新鲜，不用重下整段。"""
    if not os.path.exists(path):
        return False
    try:
        last = pd.read_csv(path, usecols=['日期'], dtype=str)['日期'].iloc[-1]
        last_date = dt.datetime.strptime(last, '%Y-%m-%d').date()
    except Exception:
        return False
    return (today - last_date).days <= 3


def fetch_etf(etf_years: float, today: dt.date, force: bool, pause: float) -> None:
    """ETF 列表 + 每只 ETF 近 etf_years 年的日线行情（价格，不是份额，见模块开头说明）。"""
    etf_list = fetch('ETF列表', lambda: ak.fund_etf_category_sina(symbol='ETF基金'), pause=pause)
    if etf_list is None or not len(etf_list):
        log('  ！拿不到 ETF 列表，这一类直接跳过')
        return
    save(etf_list, os.path.join(OUT, 'etf', 'etf_list.csv'))

    code_col = '代码' if '代码' in etf_list.columns else etf_list.columns[0]
    # 新浪的代码带 sz/sh 前缀（如 'sz159998'），fund_etf_hist_em 的 symbol 只认后面 6 位数字。
    codes = sorted({str(c)[-6:] for c in etf_list[code_col] if str(c)[-6:].isdigit()})
    log('ETF：列表 %d 只，近 %.0f 年历史（%s ~ %s）' % (len(codes), etf_years,
        (today - dt.timedelta(days=int(etf_years * 365.25))).strftime('%Y%m%d'), today.strftime('%Y%m%d')))

    start = (today - dt.timedelta(days=int(etf_years * 365.25))).strftime('%Y%m%d')
    end = today.strftime('%Y%m%d')
    folder = os.path.join(OUT, 'etf_hist')
    breaker = Breaker('ETF历史行情')
    done = skipped = 0
    try:
        for i, code in enumerate(codes, 1):
            path = os.path.join(folder, code + '.csv')
            if not force and _etf_hist_is_fresh(path, today):
                skipped += 1
                continue
            df = fetch('ETF历史 %s' % code, lambda code=code: ak.fund_etf_hist_em(
                symbol=code, period='daily', start_date=start, end_date=end, adjust=''),
                pause=pause, breaker=breaker)
            if df is not None and len(df):
                save(df, path)
                done += 1
            if i % 100 == 0:
                log('  ETF历史进度 %d/%d（新下 %d，跳过 %d）' % (i, len(codes), done, skipped))
    except Blocked as e:
        log('  ！' + str(e))
        failures.append(str(e))
    log('  ETF历史：新下/更新 %d 只，已新鲜跳过 %d 只（共 %d 只）' % (done, skipped, len(codes)))


def main() -> None:
    parser = argparse.ArgumentParser(description='akshare 财务/龙虎榜/两融/涨停池/ETF数据下载（增量）')
    parser.add_argument('kinds', nargs='*', choices=['finance', 'lhb', 'margin', 'zt_pool', 'etf'],
                        help='要下哪几类，不写就是默认四类（finance/lhb/margin/zt_pool）；'
                             'etf 比较重，不在默认里，要单独指定')
    parser.add_argument('--years', type=float, default=5,
                        help='财务、龙虎榜、两融汇总往前取几年，默认 5')
    parser.add_argument('--detail-years', type=float, default=3,
                        help='两融个股明细往前取几年，默认 3（一天一次请求，深交所还要放慢）')
    parser.add_argument('--zt-days', type=int, default=30,
                        help='涨停池/炸板池往前取多少个交易日，默认 30（近两周热点扫描够用，不用拉多年历史）')
    parser.add_argument('--etf-years', type=float, default=10,
                        help='每只 ETF 历史行情往前取几年，默认 10')
    parser.add_argument('--sleep', type=float, default=0.5, help='每次请求后停多少秒，默认 0.5')
    parser.add_argument('--szse-sleep', type=float, default=3.0,
                        help='深交所请求之间停多少秒，默认 3（它有反爬）')
    parser.add_argument('--force', action='store_true', help='已存在的文件也重新下载')
    parser.add_argument('--use-proxy', action='store_true',
                        help='走系统/环境变量里的代理（默认直连，都是国内站点）')
    args = parser.parse_args()

    if not args.use_proxy:
        # 只影响本进程；requests 看到 no_proxy=* 就连系统代理（注册表里的）也一并绕开
        os.environ['NO_PROXY'] = os.environ['no_proxy'] = '*'

    today = dt.date.today()
    start = today - dt.timedelta(days=int(args.years * 365.25))
    detail_start = max(start, today - dt.timedelta(days=int(args.detail_years * 365.25)))
    kinds = args.kinds or ['finance', 'lhb', 'margin', 'zt_pool']
    log('下载 %s，范围 %s ~ %s（两融明细从 %s 起，涨停池近 %d 个交易日），输出到 %s'
        % ('/'.join(kinds), start, today, detail_start, args.zt_days, OUT))

    warn_if_other_host_active()
    t0 = time.time()
    write_last_run('running', kinds, t0)
    try:
        if 'finance' in kinds:
            fetch_finance(start, today, args.force, args.sleep)
        if 'lhb' in kinds:
            fetch_lhb(start, today, args.force, args.sleep)
        if 'margin' in kinds:
            fetch_margin(start, detail_start, today, args.force, args.sleep, args.szse_sleep)
        if 'zt_pool' in kinds:
            fetch_zt_pool(args.zt_days, today, args.force, args.sleep)
        if 'etf' in kinds:
            fetch_etf(args.etf_years, today, args.force, args.sleep)
    except BaseException:
        write_last_run('interrupted', kinds, t0)
        raise
    write_last_run('failed' if failures else 'done', kinds, t0)

    log('完成，用时 %.1f 分钟' % ((time.time() - t0) / 60))
    conflicts = find_conflict_copies()
    if conflicts:
        log('！发现 %d 个疑似 OneDrive 冲突副本（两台机器同时写过），核对后删掉多余的：' % len(conflicts))
        for c in conflicts:
            print('  - ' + c)
    if failures:
        log('%d 个请求失败（再跑一次会自动补）：' % len(failures))
        for f in failures:
            print('  - ' + f)
        sys.exit(1)


if __name__ == '__main__':
    main()
