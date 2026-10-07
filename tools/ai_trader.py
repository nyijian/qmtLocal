# coding:utf-8
"""AI 实时介入：股票池行情 + 历史特征 -> Claude 判断 -> （默认 dry-run）QMT 下单。

**只在 .venv313 下运行**（要装 anthropic SDK，用得到新版 Python 的语法）。

    .venv313\\Scripts\\python.exe tools\\ai_trader.py 000001.SZ 600000.SH
    .venv313\\Scripts\\python.exe tools\\ai_trader.py --pool-file my_pool.txt --interval 60
    .venv313\\Scripts\\python.exe tools\\ai_trader.py 000001.SZ --live     # 真下单（见下面两道闸门）

前提
----
1. 大QMT 已启动并登录，「模型交易」里「桥接服务」在运行（见 qmt_bridge/README.md），
   行情和下单都走这条桥。
2. `.env` 里配置 `ANTHROPIC_API_KEY`（Claude API Key），账号相关配置见 `.env.example`。

两道闸门，缺一个都不会真下单（CLAUDE.md 的硬性规则：不改 ALLOW_ORDER 默认值，
不替用户下单）：
  1. **这边**：不传 `--live` 就是 dry-run，只打印"会 BUY/SELL 什么"，不调用下单接口。
  2. **桥那边**：`qmt_client_scripts/桥接服务.py` 里的 `ALLOW_ORDER` 默认是 `False`，
     这边传了 `--live` 也一样会被挡下（桥抛 RuntimeError，这边照抓照打）。
  第一次跑建议先不传 `--live`，确认 AI 的判断靠不靠谱、Prompt 要不要调，
  再去手动开桥那边的 `ALLOW_ORDER = True`。

架构（避免被 6~8 秒的模型延迟卡住 QMT/行情线程）
----------------------------------------------
  行情推送（桥自己的接收线程）──> 写进 {code: 最新tick} 缓存（加锁）
  调度线程（本脚本主线程，每 --interval 秒扫一遍池子，每只独立冷却 --cooldown 秒）
        │  取快照（实时价 + 本地历史涨停特征 + 申万一级板块）
        ▼
  线程池（--workers 个线程，并发调 Claude，互不等待）
        │  打印 AI 请求、AI 返回的结构化决策
        ▼
  一个专门的下单线程（串行执行，见 qmt_bridge/README.md「还能改的地方」第2条：
  对 ContextInfo / 交易接口的调用别到处并发调，放一个线程上排队最安全）
        │  dry-run 只打日志；--live 时调 qmt_bridge.xttrader.order_stock
        ▼
  打印 QMT 最终委托号 / 失败原因

跟视频原型（DeepSeek）的差异
----------------------------
结构化输出用 Claude 的 tool use 强制走一个固定 JSON Schema（见 `DECISION_TOOL`），
不是"提示词里让它自己吐 JSON 再拿 `json.loads` 赌一把"——Claude 返回的
`tool_use.input` 本身就是按 schema 校验过的 dict，没有"多余解释文字混进 JSON"
或者"漏了某个字段"的解析失败风险。

数据来源的真实程度（别对着这些数字下太大的注）
------------------------------------------------
* 实时涨跌幅：优先用 tick 自带的 `lastClose`；没有就退到本地日线缓存的
  `preClose`（`qmt_bridge/dat_reader.py`），**这份缓存不保证是最新的**，
  滞后几天很常见，跑前自己 `local_history.py --gaps` 看一眼。
* 历史涨停次数：同样读本地日线缓存，用涨跌幅阈值估算涨停（主板 9.5%、
  创业板/科创板 19.5%），**没做 ST 的 5%/减半处理**，照 `dat_reader.py` 自己
  的提醒："不确定的字段不装作确定"——这是近似值，不是交易所口径。
* 板块：桥的 `get_stock_list_in_sector('SW1板块名')` 反查，跟
  `tools/market_overview.py` 的「行业强弱」是同一套申万一级分类。
"""
import argparse
import datetime as dt
import json
import os
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 走桥。miniQMT 恢复之后，把下面几行换成 xtquant / anthropic 对应模块即可。
from qmt_bridge import dat_reader, xtconstant, xtdata  # noqa: E402
from qmt_bridge.dotenv_lite import load_dotenv  # noqa: E402
from qmt_bridge.xttrader import XtQuantTrader  # noqa: E402
from qmt_bridge.xttype import StockAccount  # noqa: E402

try:
    import anthropic
except ImportError:
    raise SystemExit(
        '没装 anthropic SDK。.venv313\\Scripts\\python.exe -m pip install '
        '-i https://pypi.tuna.tsinghua.edu.cn/simple anthropic')

load_dotenv()
DEFAULT_ACCOUNT = os.environ.get('QMT_ACCOUNT_ID', '')
DEFAULT_ACCOUNT_TYPE = os.environ.get('QMT_ACCOUNT_TYPE', 'STOCK')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# 跟 tools/market_overview.py 的 SW1_INDUSTRIES 是同一份常量（各脚本自己带一份，
# 这个仓库现在没有共享的 tools 公共模块，没必要为 31 个字符串单独抽一个）。
SW1_INDUSTRIES = [
    '农林牧渔', '基础化工', '钢铁', '有色金属', '电子', '家用电器', '食品饮料', '纺织服饰',
    '轻工制造', '医药生物', '公用事业', '交通运输', '房地产', '商贸零售', '社会服务', '综合',
    '建筑材料', '建筑装饰', '电力设备', '国防军工', '计算机', '传媒', '通信', '银行',
    '非银金融', '汽车', '机械设备', '煤炭', '石油石化', '环保', '美容护理',
]

SYSTEM_PROMPT = """你是 A 股日内交易的决策助手，纪律严格、不夸大、不臆测。
只根据用户给的结构化数据判断，缺的数据就当缺，不要编造。
涨停/跌停、成交量异常只是参考，不要单纯因为"今天涨得多"就追，也要考虑追高风险。
拿不准、数据矛盾、或者看不出明显机会时，直接给 HOLD，不要为了给结论硬给。
必须调用 report_decision 这个工具给出结果，不要在工具调用之外输出别的文字。"""

DECISION_TOOL = {
    'name': 'report_decision',
    'description': '对给定标的给出买/卖/持有的决策。',
    'input_schema': {
        'type': 'object',
        'properties': {
            'symbol': {'type': 'string', 'description': '股票代码，如 000001.SZ'},
            'action': {'type': 'string', 'enum': ['BUY', 'SELL', 'HOLD']},
            'confidence': {'type': 'number', 'description': '0~1，对这个判断有多大把握'},
            'reason': {'type': 'string', 'description': '一两句话说明依据，中文'},
        },
        'required': ['symbol', 'action', 'reason'],
    },
}


def log(msg):
    print('%s %s' % (dt.datetime.now().strftime('%H:%M:%S'), msg), flush=True)


# ---------------------------------------------------------------- 板块反查

def build_sector_map(prefix='SW1'):
    """桥查一遍 31 个申万一级板块的成分股，反转成 {code: 板块名}。一次性，启动时做。"""
    mapping = {}
    ok = 0
    for name in SW1_INDUSTRIES:
        try:
            codes = xtdata.get_stock_list_in_sector(prefix + name)
        except Exception as e:
            log('！查板块 %s 失败：%s' % (name, e))
            continue
        if not codes:
            continue
        ok += 1
        for code in codes:
            mapping[code] = name
    log('板块反查：%d/%d 个板块有数据，覆盖 %d 只标的' % (ok, len(SW1_INDUSTRIES), len(mapping)))
    return mapping


# ---------------------------------------------------------------- 历史涨停特征

def _limit_ratio(code):
    inst = code.split('.')[0]
    return 0.195 if inst.startswith(('300', '301', '688', '689')) else 0.095


def load_history_features(code, lookback_days):
    """本地日线缓存读一遍，数近 lookback_days 个交易日里涨停了几次、最近一次是哪天。

    读不到本地缓存（没下过/代码错）时返回 None，上层据此在 prompt 里标注"历史数据缺失"，
    不是当成 0 次涨停——"没数据"和"确实没涨停过"是两件不同的事，不能混为一谈。
    """
    try:
        rows = dat_reader.read_daily_raw(code, last_n=lookback_days)
    except FileNotFoundError:
        return None
    if not rows:
        return None
    ratio = _limit_ratio(code)
    limit_up_dates = [
        r['date'] for r in rows
        if r['preClose'] > 0 and (r['close'] - r['preClose']) / r['preClose'] >= ratio
    ]
    last_row = rows[-1]
    gap_days = (dt.date.today() - last_row['date']).days
    return {
        'limit_up_count': len(limit_up_dates),
        'last_limit_up_date': limit_up_dates[-1].isoformat() if limit_up_dates else None,
        'local_cache_date': last_row['date'].isoformat(),
        'local_cache_gap_days': gap_days,
        'local_cache_preclose': last_row['close'],  # 缓存里最后一条的收盘，tick 没给 lastClose 时顶上
    }


# ---------------------------------------------------------------- 行情快照

class QuoteCache:
    """全推订阅的落地点：{code: 最新tick}，加锁给调度线程读。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._ticks = {}

    def on_push(self, datas):
        with self._lock:
            self._ticks.update(datas)

    def get(self, code):
        with self._lock:
            return self._ticks.get(code)


def build_snapshot(code, tick, history):
    preclose = tick.get('lastClose') or (history.get('local_cache_preclose') if history else None)
    pct_chg = None
    if preclose and tick.get('lastPrice'):
        pct_chg = round((tick['lastPrice'] - preclose) / preclose * 100, 2)
    return {
        'symbol': code,
        'last_price': tick.get('lastPrice'),
        'pct_chg_today': pct_chg,
        'open': tick.get('open'),
        'high': tick.get('high'),
        'low': tick.get('low'),
        'volume': tick.get('volume'),
        'amount': tick.get('amount'),
        'sector': None,     # 外层填
        'history': history if history is not None else '本地无历史缓存，没下过或代码不对',
    }


# ---------------------------------------------------------------- 调 Claude

def ask_claude(client, model, max_tokens, snapshot):
    """同步调用，扔进线程池并发跑，不卡调度线程。返回 (decision_dict, raw_request, raw_text)。"""
    request_payload = json.dumps(snapshot, ensure_ascii=False)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        tools=[DECISION_TOOL],
        tool_choice={'type': 'tool', 'name': 'report_decision'},
        messages=[{'role': 'user', 'content': '这是这只标的现在的数据：\n' + request_payload}],
    )
    for block in message.content:
        if block.type == 'tool_use' and block.name == 'report_decision':
            return block.input, request_payload
    # 理论上 tool_choice 强制了一定会给工具调用；真遇到意外格式就老实报错，不瞎猜。
    raise RuntimeError('Claude 没有按约定调用 report_decision，原始返回：%r' % message.content)


# ---------------------------------------------------------------- 下单（串行）

class OrderWorker:
    """专门的下单线程：把"决定要下单"和"真的调用交易接口"分开，交易接口调用不并发。"""

    def __init__(self, trader, account, live, order_volume):
        self.trader = trader
        self.account = account
        self.live = live
        self.order_volume = order_volume
        self._q = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, decision):
        self._q.put(decision)

    def stop(self):
        self._q.put(None)

    def _run(self):
        while True:
            decision = self._q.get()
            if decision is None:
                return
            self._execute(decision)

    def _execute(self, decision):
        action = decision.get('action')
        symbol = decision.get('symbol')
        if action not in ('BUY', 'SELL'):
            return
        if not self.live:
            log('[dry-run] 会 %s %s %d 股，没传 --live，不真下单'
                % (action, symbol, self.order_volume))
            return
        side = xtconstant.STOCK_BUY if action == 'BUY' else xtconstant.STOCK_SELL
        try:
            order_id = self.trader.order_stock(
                self.account, symbol, side, self.order_volume,
                price_type=xtconstant.LATEST_PRICE, price=0,
                strategy_name='ai_trader', order_remark=decision.get('reason', '')[:40])
            log('[下单] %s %s %d 股 -> 委托号 %s' % (action, symbol, self.order_volume, order_id))
        except RuntimeError as e:
            log('[下单被挡] %s %s：%s（桥那边 ALLOW_ORDER 大概还是 False）' % (action, symbol, e))
        except Exception as e:
            log('[下单失败] %s %s：%s: %s' % (action, symbol, type(e).__name__, e))


# ---------------------------------------------------------------- 主循环

def handle_one(client, model, max_tokens, code, tick, history, sector, order_worker):
    snapshot = build_snapshot(code, tick, history)
    snapshot['sector'] = sector or '未知'
    try:
        decision, request_payload = ask_claude(client, model, max_tokens, snapshot)
    except Exception as e:
        log('[AI 请求失败] %s：%s: %s' % (code, type(e).__name__, e))
        return
    log('[AI 请求] %s -> %s' % (code, request_payload))
    log('[AI 决策] %s %s（把握 %s）：%s'
        % (code, decision.get('action'), decision.get('confidence'), decision.get('reason')))
    order_worker.submit(decision)


def main():
    parser = argparse.ArgumentParser(description='AI（Claude）实时介入的交易决策框架，默认 dry-run')
    parser.add_argument('codes', nargs='*', help='股票代码如 000001.SZ')
    parser.add_argument('--pool-file', help='股票池文件，一行一个代码，跟位置参数的代码合并')
    parser.add_argument('--interval', type=float, default=60,
                        help='调度线程扫一遍股票池的间隔秒数，默认 60')
    parser.add_argument('--cooldown', type=float, default=180,
                        help='同一只标的两次调用 Claude 的最小间隔秒数，默认 180')
    parser.add_argument('--model', default='claude-sonnet-5')
    parser.add_argument('--max-tokens', type=int, default=512)
    parser.add_argument('--workers', type=int, default=4, help='并发调 Claude 的线程数')
    parser.add_argument('--limit-up-lookback', type=int, default=20,
                        help='历史涨停统计回看多少个本地缓存交易日，默认 20')
    parser.add_argument('--sector-prefix', default='SW1')
    parser.add_argument('--live', action='store_true',
                        help='真下单（还要桥那边 ALLOW_ORDER=True 才真正发得出去），不传就是 dry-run')
    parser.add_argument('--order-volume', type=int, default=100, help='每次下单的股数，默认 100')
    parser.add_argument('-a', '--account', default=DEFAULT_ACCOUNT)
    parser.add_argument('-t', '--account-type', default=DEFAULT_ACCOUNT_TYPE)
    args = parser.parse_args()

    codes = list(args.codes)
    if args.pool_file:
        with open(args.pool_file, encoding='utf-8-sig') as f:
            codes += [line.strip() for line in f if line.strip() and not line.startswith('#')]
    codes = sorted(set(codes))
    if not codes:
        raise SystemExit('股票池是空的：传代码位置参数，或者 --pool-file。')

    if not ANTHROPIC_API_KEY:
        raise SystemExit('没有 ANTHROPIC_API_KEY：复制 .env.example 为 .env，填上 Claude API Key。')

    if args.live and not args.account:
        raise SystemExit('--live 需要账号：传 -a，或者在 .env 里配 QMT_ACCOUNT_ID。')

    log('股票池 %d 只：%s' % (len(codes), ', '.join(codes)))
    log('模式：%s' % ('LIVE（还受桥那边 ALLOW_ORDER 控制）' if args.live else 'dry-run，只打印不下单'))

    trader = XtQuantTrader('', 0)
    trader.start()
    if trader.connect() != 0:
        raise SystemExit(trader.last_error or
                         '连不上 QMT 桥。确认大QMT已启动，且「模型交易」里的「桥接服务」正在运行。')
    account = StockAccount(args.account, args.account_type) if args.account else None
    if args.live and account is None:
        raise SystemExit('--live 但没有可用账号。')

    sector_map = build_sector_map(args.sector_prefix)
    log('读本地历史涨停特征（回看 %d 个交易日）……' % args.limit_up_lookback)
    history_map = {code: load_history_features(code, args.limit_up_lookback) for code in codes}
    for code, h in history_map.items():
        if h is None:
            log('！%s 没有本地日线缓存，历史涨停特征会标成"缺失"，去 QMT「补充数据」补一下' % code)
        elif h['local_cache_gap_days'] > 5:
            log('！%s 本地缓存滞后 %d 天，历史特征可能不准' % (code, h['local_cache_gap_days']))

    quote_cache = QuoteCache()
    xtdata.subscribe_whole_quote(codes, callback=quote_cache.on_push)
    log('已订阅全推行情，等快照……')

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    order_worker = OrderWorker(trader, account, args.live, args.order_volume)
    executor = ThreadPoolExecutor(max_workers=args.workers)
    last_called = {}   # code -> 上次调用 Claude 的时间戳，做冷却

    try:
        while True:
            now = time.time()
            for code in codes:
                if now - last_called.get(code, 0) < args.cooldown:
                    continue
                tick = quote_cache.get(code)
                if not tick:
                    continue
                last_called[code] = now
                executor.submit(handle_one, client, args.model, args.max_tokens,
                                code, tick, history_map.get(code), sector_map.get(code),
                                order_worker)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log('收到 Ctrl+C，收尾……')
    finally:
        executor.shutdown(wait=False)
        order_worker.stop()
        trader.stop()


if __name__ == '__main__':
    main()
