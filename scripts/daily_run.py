#!/usr/bin/env python
"""Cron entry: iterate watchlist, run propagate for each ticker for today.

Run from the project root in the ``fundteam`` conda env::

    /opt/anaconda3/envs/fundteam/bin/python /Users/kevin/PycharmProjects/Fund/Analyst/scripts/daily_run.py

A recommended crontab entry (weekdays 09:00 Asia/Shanghai)::

    0 9 * * 1-5 /opt/anaconda3/envs/fundteam/bin/python \\
        /Users/kevin/PycharmProjects/Fund/Analyst/scripts/daily_run.py \\
        >> ~/.tradingagents/dashboard/cron.log 2>&1
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.runner import run_analysis
from dashboard.state_reader import load_watchlist, get_today_str


def main():
    today = get_today_str()
    for ticker in load_watchlist():
        try:
            run_analysis(ticker, today)
        except Exception as e:
            print(f"FAILED {ticker} {today}: {e}", file=sys.stderr)
            continue


if __name__ == '__main__':
    main()
