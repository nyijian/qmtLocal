# coding:utf-8
"""不走桥，直接读大QMT本地缓存的日线数据。收盘后、桥挂了的时候用这个。

    .venv\\Scripts\\python.exe local_history.py                       # 默认几只持仓
    .venv\\Scripts\\python.exe local_history.py 000300.SH 600000.SH   # 指定代码
    .venv\\Scripts\\python.exe local_history.py --gaps                # 只看哪些代码该去补数据了
    .venv\\Scripts\\python.exe local_history.py -n 20 600366.SH       # 打印最近 20 条

前提只有一个：QMT 装在本机、datadir 里有这个代码的日线缓存。QMT 开不开、桥通不通，
跟这个完全没关系——这条路读的是磁盘上的文件，见 qmt_bridge/dat_reader.py 顶部的说明。

这份本地缓存**可能滞后**：它好像只在你在 QMT 里主动看过某代码之后才刷新，不是每天
自动补。滞后多少天，下面每只代码后面会标出来；滞后了就去 QMT「数据管理」→
「补充数据」手动点一下，然后再跑这个脚本。
"""
import argparse
import sys

from qmt_bridge import dat_reader

# 不传代码时默认看哪几只。改成你自己关心的代码。
DEFAULT_CODES = ['600366.SH', '600436.SH', '605208.SH']


def show_gaps(codes):
    print('%-12s %-12s %s' % ('代码', '本地最后一条', '滞后天数'))
    for code, last, gap in dat_reader.scan_gaps(codes):
        if last is None:
            print('%-12s %-12s %s' % (code, '本地没有缓存', '-'))
        else:
            flag = '' if (gap or 0) <= 1 else '  <- 该去「补充数据」补一下了'
            print('%-12s %-12s %s天%s' % (code, last, gap, flag))


def show_history(code, n):
    try:
        df, gap = dat_reader.read_daily(code)
    except FileNotFoundError:
        print('%s：本地没有缓存，先在 QMT「数据管理」→「补充数据」下载一下' % code)
        return
    tag = '（最新）' if (gap or 0) <= 1 else '（滞后 %d 天，去「补充数据」补一下）' % gap
    print('=== %s %s ===' % (code, tag))
    print(df.tail(n).to_string(
        formatters={
            'open': '{:.3f}'.format, 'high': '{:.3f}'.format,
            'low': '{:.3f}'.format, 'close': '{:.3f}'.format,
            'preClose': '{:.3f}'.format,
            'volume': '{:,d}'.format, 'amount': '{:,d}'.format,
        }))
    print()


def main():
    parser = argparse.ArgumentParser(description='离线读大QMT本地日线缓存')
    parser.add_argument('codes', nargs='*', default=None,
                        help='股票代码，如 000300.SH；不传就用默认持仓列表')
    parser.add_argument('-n', type=int, default=10, help='打印最近几条，默认10')
    parser.add_argument('--gaps', action='store_true',
                        help='只看每个代码本地缓存滞后几天，不打印明细')
    args = parser.parse_args()

    codes = args.codes or DEFAULT_CODES

    if args.gaps:
        show_gaps(codes)
        return

    for code in codes:
        show_history(code, args.n)


if __name__ == '__main__':
    sys.exit(main())
