# coding:gbk
"""不启动QMT客户端，本地跑一遍策略脚本init/handlebar逻辑用的模拟上下文。

只实现了main.py这类脚本实际用到的QMT接口子集（get_market_data_ex/get_bar_timetag/
draw_text/passorder/get_trade_detail_data），不是QMT ContextInfo的完整实现。
委托一律按下单当根K线的收盘价成交，只用于跑通策略逻辑、暴露语法/逻辑错误，
不是可用于评估策略收益的真实回测引擎，回测结果仍需回到QMT客户端里跑。
"""
import datetime as dt

from .data import FIELDS, SyntheticMarket, trading_calendar

STOCK_BUY = 23
STOCK_SELL = 24


class _DetailData(object):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class PaperAccount(object):
    def __init__(self, cash=1000000.0):
        self.cash = cash
        self.positions = {}  # stock_code -> volume

    def buy(self, stock_code, price, volume):
        if price <= 0 or volume <= 0:
            return
        cost = price * volume
        if cost > self.cash:
            volume = int(self.cash / price / 100) * 100
            cost = price * volume
        if volume <= 0:
            return
        self.cash -= cost
        self.positions[stock_code] = self.positions.get(stock_code, 0) + volume

    def sell(self, stock_code, price, volume):
        held = self.positions.get(stock_code, 0)
        volume = min(volume, held)
        if volume <= 0:
            return
        self.cash += price * volume
        self.positions[stock_code] = held - volume

    def account_detail(self):
        return [_DetailData(m_dAvailable=self.cash, m_dBalance=self.cash, m_dAssureAsset=self.cash)]

    def position_detail(self):
        out = []
        for code, vol in self.positions.items():
            if vol <= 0:
                continue
            instrument, exchange = code.split(".")
            out.append(_DetailData(m_strInstrumentID=instrument, m_strExchangeID=exchange,
                                    m_nVolume=vol, m_nCanUseVolume=vol))
        return out


class MockContextInfo(object):
    """模拟QMT的ContextInfo/C对象。"""

    def __init__(self, stockcode, market, period="1d", start_date=None, end_date=None,
                 cash=1000000.0, seed=None):
        self.stockcode = stockcode
        self.market = market
        self.period = period
        self.barpos = 0

        self.calendar = trading_calendar(start_date, end_date)
        if not self.calendar:
            raise ValueError("回测区间内没有交易日，请检查 start/end 参数")
        self.market_data = SyntheticMarket(self.calendar, seed=seed)
        self.account = PaperAccount(cash=cash)

    def get_bar_timetag(self, barpos):
        date = self.calendar[barpos]
        return int(dt.datetime.strptime(date, "%Y%m%d").timestamp() * 1000)

    def get_market_data_ex(self, field_list=None, stock_list=None, end_time="", period="1d",
                            count=-1, dividend_type="none", fill_data=True, subscribe=True):
        field_list = list(field_list) if field_list else list(FIELDS)
        stock_list = stock_list or [self.stockcode + "." + self.market]
        end_date = (end_time or self.calendar[self.barpos])[:8]

        result = {}
        for code in stock_list:
            df = self.market_data.bars(code)
            sliced = df[df.index <= end_date]
            if count and count > 0:
                sliced = sliced.tail(count)
            result[code] = sliced[field_list]
        return result

    def draw_text(self, chart_id, sub_id, text):
        date = self.calendar[self.barpos]
        print("[draw_text] {} chart={} sub={} -> {}".format(date, chart_id, sub_id, text))


def build_globals(context):
    """返回要注入到策略脚本执行命名空间里的QMT全局函数（passorder等）。"""

    def timetag_to_datetime(timetag, fmt=None):
        fmt = fmt or ("%Y%m%d" if timetag % 86400000 == 57600000 else "%Y%m%d%H%M%S")
        return dt.datetime.fromtimestamp(timetag / 1000).strftime(fmt)

    def passorder(op_type, order_type, accountid, order_code, price_type, price, volume, C=None):
        c = C or context
        bar_date = c.calendar[c.barpos]
        close_price = float(c.market_data.bars(order_code).loc[bar_date, "close"])
        fill_price = close_price if price in (-1, 0, None) else price
        if op_type == STOCK_BUY:
            c.account.buy(order_code, fill_price, volume)
        elif op_type == STOCK_SELL:
            c.account.sell(order_code, fill_price, volume)
        else:
            print("[passorder] 暂不支持的委托类型 optype={}，已忽略".format(op_type))

    def get_trade_detail_data(accountid, accounttype, datatype, strategyname=""):
        if datatype == "account":
            return context.account.account_detail()
        if datatype == "position":
            return context.account.position_detail()
        return []

    return {
        "timetag_to_datetime": timetag_to_datetime,
        "passorder": passorder,
        "get_trade_detail_data": get_trade_detail_data,
    }
