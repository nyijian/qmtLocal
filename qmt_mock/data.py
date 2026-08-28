# coding:utf-8
"""本地无QMT客户端时，为策略脚本生成可重复的模拟K线数据。"""
import datetime as dt
import random

import pandas as pd

FIELDS = ["open", "high", "low", "close", "volume", "amount"]


def trading_calendar(start_date, end_date):
    """start_date/end_date: 'YYYYMMDD'。只按周一到周五生成，不排节假日，仅供本地跑逻辑用。"""
    start = dt.datetime.strptime(start_date, "%Y%m%d").date()
    end = dt.datetime.strptime(end_date, "%Y%m%d").date()
    dates = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y%m%d"))
        d += dt.timedelta(days=1)
    return dates


class SyntheticMarket(object):
    """按股票代码生成确定性的随机游走K线，同一代码/同一seed每次生成结果一致。"""

    def __init__(self, calendar, base_price=10.0, seed=None):
        self.calendar = calendar
        self.base_price = base_price
        self.seed = seed
        self._cache = {}

    def bars(self, stock_code):
        if stock_code not in self._cache:
            self._cache[stock_code] = self._generate(stock_code)
        return self._cache[stock_code]

    def _generate(self, stock_code):
        rnd = random.Random("{}:{}".format(self.seed, stock_code))
        price = self.base_price
        rows = []
        for date in self.calendar:
            change = rnd.uniform(-0.03, 0.03)
            open_p = round(price, 2)
            close_p = round(max(price * (1 + change), 0.01), 2)
            high_p = round(max(open_p, close_p) * (1 + rnd.uniform(0, 0.01)), 2)
            low_p = round(min(open_p, close_p) * (1 - rnd.uniform(0, 0.01)), 2)
            volume = rnd.randint(1000, 20000) * 100
            amount = round(volume * close_p, 2)
            rows.append((open_p, high_p, low_p, close_p, volume, amount))
            price = close_p
        return pd.DataFrame(rows, index=self.calendar, columns=FIELDS)
