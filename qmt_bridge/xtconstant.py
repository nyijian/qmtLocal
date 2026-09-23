# coding:utf-8
"""xtconstant 的替身：常量值抄官方 xtquant.xtconstant。

只收录**两边取值确实一致**的那些。大QMT 的 passorder 和 miniQMT 的 order_stock
在委托方向（23/24）、限价/最新价（11/5）、委托状态（48~57）上是同一套数，
所以这些可以照搬。

市价单那几档（五档即成剩撤、对手方最优价等）两边的编号对不上，**没有收录**，
也不做任何翻译：`price_type` 是原样传给 `passorder` 的 prType 的。要下市价单，
查大QMT 帮助里 passorder 的 prType 表，把数字直接填进去。
"""

# ---- 委托方向（passorder 的 opType） ----------------------------------
STOCK_BUY = 23
STOCK_SELL = 24

# ---- 报价方式（passorder 的 prType） ----------------------------------
LATEST_PRICE = 5             # 最新价
FIX_PRICE = 11               # 指定价（限价）

# ---- 下单量的口径（passorder 的 orderType） ---------------------------
FIX_VOLUME = 1101            # 按股数/手数，volume 取整数
FIX_AMOUNT = 1102            # 按金额，volume 是浮点

# ---- 委托状态（委托对象的 order_status） ------------------------------
ORDER_UNREPORTED = 48        # 未报
ORDER_WAIT_REPORTING = 49    # 待报
ORDER_REPORTED = 50          # 已报
ORDER_REPORTED_CANCEL = 51   # 已报待撤
ORDER_PARTSUCC_CANCEL = 52   # 部成待撤
ORDER_PART_CANCEL = 53       # 部撤
ORDER_CANCELED = 54          # 已撤
ORDER_PART_SUCC = 55         # 部成
ORDER_SUCCEEDED = 56         # 已成
ORDER_JUNK = 57              # 废单

ORDER_STATUS_TEXT = {
    ORDER_UNREPORTED: '未报',
    ORDER_WAIT_REPORTING: '待报',
    ORDER_REPORTED: '已报',
    ORDER_REPORTED_CANCEL: '已报待撤',
    ORDER_PARTSUCC_CANCEL: '部成待撤',
    ORDER_PART_CANCEL: '部撤',
    ORDER_CANCELED: '已撤',
    ORDER_PART_SUCC: '部成',
    ORDER_SUCCEEDED: '已成',
    ORDER_JUNK: '废单',
}

# 还能撤的状态。query_stock_orders(cancelable_only=True) 用的就是这个集合。
CANCELABLE_STATUS = frozenset([
    ORDER_UNREPORTED, ORDER_WAIT_REPORTING, ORDER_REPORTED, ORDER_PART_SUCC,
])

# ---- 买卖方向 / 开平（原始字段，直接照搬 QMT 的取值） -----------------
DIRECTION_FLAG_BUY = 48
DIRECTION_FLAG_SELL = 49
OFFSET_FLAG_OPEN = 48        # 股票里就是"买入"
OFFSET_FLAG_CLOSE = 49       # 股票里就是"卖出"

# ---- 账号类型 ---------------------------------------------------------
FUTURE_ACCOUNT = 1
SECURITY_ACCOUNT = 2
CREDIT_ACCOUNT = 3
FUTURE_OPTION_ACCOUNT = 4
STOCK_OPTION_ACCOUNT = 5
HUGANGTONG_ACCOUNT = 6
SHENGANGTONG_ACCOUNT = 11
