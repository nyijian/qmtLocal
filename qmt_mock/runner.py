# coding:gbk
"""不打开QMT客户端，直接在本地跑一遍QMT策略脚本的init/handlebar逻辑。

用法:
    .venv\\Scripts\\python.exe -m qmt_mock.runner main.py --code 600000.SH --start 20240101 --end 20240301
"""
import argparse
import sys
import traceback

from .context import MockContextInfo, build_globals


def run(strategy_path, code, start, end, period="1d", cash=1000000.0, seed=None):
    stockcode, market = code.split(".")
    context = MockContextInfo(stockcode, market, period=period, start_date=start, end_date=end,
                               cash=cash, seed=seed)

    # QMT客户端策略编辑器存的是GBK，本地也可能写成UTF-8，两种都要能读。
    # 先按UTF-8严格解，失败再按GBK解（UTF-8中文字节按GBK解不会报错，顺序不能反）。
    with open(strategy_path, "rb") as f:
        raw = f.read()
    for _enc in ("utf-8", "gbk"):
        try:
            source = raw.decode(_enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise RuntimeError("策略文件既不是UTF-8也不是GBK: %s" % strategy_path)

    namespace = build_globals(context)
    namespace["__name__"] = "__qmt_strategy__"
    code_obj = compile(source, strategy_path, "exec")
    exec(code_obj, namespace)

    if "init" not in namespace or "handlebar" not in namespace:
        raise RuntimeError("策略文件必须定义 init(C) 和 handlebar(C)")

    namespace["init"](context)
    for barpos in range(len(context.calendar)):
        context.barpos = barpos
        namespace["handlebar"](context)

    print("\n===== 模拟运行结束（合成行情，仅用于跑通逻辑） =====")
    print("最终可用资金: {:,.2f}".format(context.account.cash))
    positions = context.account.position_detail()
    if not positions:
        print("持仓: 无")
    for p in positions:
        print("持仓 {}.{}: {}".format(p.m_strInstrumentID, p.m_strExchangeID, p.m_nVolume))


def main():
    parser = argparse.ArgumentParser(description="本地模拟运行QMT策略脚本（无需打开QMT客户端）")
    parser.add_argument("strategy", help="策略.py文件路径")
    parser.add_argument("--code", default="000300.SH", help="主图/测试标的，如 600000.SH")
    parser.add_argument("--start", default="20240101", help="起始日期 YYYYMMDD")
    parser.add_argument("--end", default="20240601", help="结束日期 YYYYMMDD")
    parser.add_argument("--period", default="1d")
    parser.add_argument("--cash", type=float, default=1000000.0)
    parser.add_argument("--seed", default=None, help="随机种子，固定后每次生成的模拟行情一致")
    args = parser.parse_args()

    try:
        run(args.strategy, args.code, args.start, args.end, args.period, args.cash, args.seed)
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
