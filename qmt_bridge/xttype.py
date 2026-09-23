# coding:utf-8
"""xttype 的替身：账号对象，签名照抄官方 xtquant.xttype。

    from qmt_bridge.xttype import StockAccount
    acc = StockAccount('55004374')            # 默认 STOCK
    acc = StockAccount('55004374', 'CREDIT')  # 融资融券
"""


class StockAccount(object):

    def __init__(self, account_id, account_type='STOCK'):
        self.account_id = str(account_id)
        self.account_type = account_type

    def __repr__(self):
        return 'StockAccount(%r, %r)' % (self.account_id, self.account_type)

    def __eq__(self, other):
        return (isinstance(other, StockAccount)
                and other.account_id == self.account_id
                and str(other.account_type).upper() == str(self.account_type).upper())

    def __hash__(self):
        return hash((self.account_id, str(self.account_type).upper()))
