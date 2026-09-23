# coding:utf-8
"""连到大QMT策略运行时的数据桥：行情 + 交易。

    from qmt_bridge import xtdata                    # 行情
    from qmt_bridge.xttrader import XtQuantTrader    # 交易
    from qmt_bridge.xttype import StockAccount
    from qmt_bridge import xtconstant

接口签名跟官方 xtquant 一致，两边的差异分别写在 xtdata.py 和 xttrader.py
的模块注释里。

xttrader 不在这里 import —— 它要拉起 xtdata 的连接，让它按需加载，
只取行情的脚本没必要碰交易那套。
"""

from . import xtconstant
from . import xtdata

__all__ = ['xtdata', 'xtconstant']
