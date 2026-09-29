"""Pyright/Pylance-only type stub (never executed at runtime).

Declares the globals that QMT's client (and qmt_mock/runner.py locally) inject
into a strategy script's namespace at exec time, so Pylance stops flagging
them as undefined in strategies/*.py Purely an editor convenience —
has zero effect on how the scripts run, in QMT or locally.
"""
from typing import Any, Optional

def timetag_to_datetime(timetag: int, fmt: Optional[str] = None) -> str: ...

def passorder(
    opType: int,
    orderType: int,
    accountid: str,
    orderCode: str,
    prType: int,
    price: float,
    volume: int,
    C: Any = None,
) -> None: ...

def get_trade_detail_data(
    accountid: str,
    accounttype: str,
    datatype: str,
    strategyname: str = "",
) -> list: ...
