"""External monthly port-performance data from Transport Canada's Marine and
Port Dashboard (Transportation Data and Information Hub).

Source page:
    https://tdih-cdit.tc.canada.ca/en/dashboard/marine-and-port-dashboard
File:
    "Monthly port performance - containers.csv"

Columns used:
    Port, Year, Month,
    Container Volumes (TEUS),
    Number of Vessel Calls (#),
    Avg. Import Rail Container Dwell Time (days)

The loader only consumes same-calendar-month values from years BEFORE the
prediction year, so the external seasonal factor never sees the month being
predicted. This keeps the backtest honest.
"""
from __future__ import annotations

import csv
import os
import statistics

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTERNAL_CSV = os.path.join(BASE_DIR, 'external', 'port_monthly_dwell.csv')

MONTH_NUM = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}

PORT_MAP = {
    'vancouver': 'VANCOUVER',
    'montreal': 'MONTREAL',
    'halifax': 'HALIFAX',
    'prince rupert': 'PRINCE RUPERT',
    'saint john': 'SAINT JOHN',
}


def _num(text):
    if text is None:
        return None
    t = text.strip().replace(',', '')
    if not t or t in ('-', '..', '...'):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def load_port_performance(path=EXTERNAL_CSV):
    """Return {(port, year, month): {'dwell', 'calls', 'teu'}}."""
    series = {}
    if not os.path.exists(path):
        return series
    with open(path, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            port = PORT_MAP.get((row.get('Port') or '').strip().lower())
            month = MONTH_NUM.get((row.get('Month') or '').strip()[:3].lower())
            if not port or month is None:
                continue
            try:
                year = int((row.get('Year') or '').strip())
            except ValueError:
                continue
            dwell = _num(row.get('Avg. Import Rail Container Dwell Time (days)'))
            if dwell is None:
                continue
            series[(port, year, month)] = {
                'dwell': dwell,
                'calls': _num(row.get('Number of Vessel Calls (#)')),
                'teu': _num(row.get('Container Volumes (TEUS)')),
            }
    return series


_SERIES = None


def default_series():
    global _SERIES
    if _SERIES is None:
        _SERIES = load_port_performance()
    return _SERIES


def seasonal_factor(series, port, year, month):
    """Official same-calendar-month dwell relative to the port's prior-year median.

    Uses only years < `year`. Returns None when no prior-year data exists.
    Clamped to [0.6, 1.8] like the shipment-level factors.
    """
    hist = [v['dwell'] for (p, y, m), v in series.items()
            if p == port and m == month and y < year]
    base_vals = [v['dwell'] for (p, y, m), v in series.items() if p == port and y < year]
    if not hist or not base_vals:
        return None
    base = statistics.median(base_vals)
    if base <= 0:
        return None
    raw = statistics.median(hist) / base
    return max(0.6, min(1.8, raw))


EXT_COLS = (
    'off_dwell_last', 'off_dwell_prev', 'off_dwell_trail3', 'off_dwell_ratio6',
    'off_dwell_delta', 'off_calls_last', 'off_teu_last', 'off_teu_trail3',
    'off_teu_ratio6', 'off_teu_per_call', 'off_dwell_seasonal',
)


def _nan_features():
    return {k: float('nan') for k in EXT_COLS}


def monthly_profile(series, port):
    """Month -> median official import rail dwell across all published years."""
    buckets = {m: [] for m in range(1, 13)}
    for (p, y, m), v in series.items():
        if p == port and v.get('dwell') is not None:
            buckets[m].append(v['dwell'])
    out = {}
    for m, vals in buckets.items():
        if vals:
            out[m] = statistics.median(vals)
    return out


def official_features(series, port, anchor, lag_months=2):
    """Leak-free official port features for a shipment anchored on `anchor`.

    Only months at least `lag_months` before the anchor's calendar month are
    used, mimicking the publication delay of the official monthly file. The
    feature values are levels and congestion ratios (relative to the port's own
    trailing history), so they describe the port environment without needing to
    match the official dwell definition exactly.
    """
    out = _nan_features()
    if series is None or port is None or anchor is None:
        return out
    cutoff = anchor.year * 12 + (anchor.month - 1) - lag_months
    vals = []
    for (p, y, m), v in series.items():
        if p != port:
            continue
        idx = y * 12 + (m - 1)
        if idx <= cutoff:
            vals.append((idx, m, v))
    vals.sort()
    if not vals:
        return out

    def col(key, rows):
        return [r[2][key] for r in rows if r[2].get(key) is not None]

    last = vals[-1][2]
    prev = vals[-2][2] if len(vals) > 1 else {}
    last3 = vals[-3:]
    last6 = vals[-6:]
    dwell3 = col('dwell', last3)
    dwell6 = col('dwell', last6)
    teu3 = col('teu', last3)
    base6 = statistics.median(dwell6) if dwell6 else None

    if last.get('dwell') is not None:
        out['off_dwell_last'] = last['dwell']
        if base6:
            out['off_dwell_ratio6'] = last['dwell'] / base6
        if prev.get('dwell') is not None:
            out['off_dwell_delta'] = last['dwell'] - prev['dwell']
    if prev.get('dwell') is not None:
        out['off_dwell_prev'] = prev['dwell']
    if dwell3:
        out['off_dwell_trail3'] = statistics.mean(dwell3)
    if last.get('calls') is not None:
        out['off_calls_last'] = last['calls']
    if last.get('teu') is not None:
        out['off_teu_last'] = last['teu']
        if last.get('calls'):
            out['off_teu_per_call'] = last['teu'] / last['calls']
        base_teu = statistics.median(col('teu', last6)) if teu3 else None
        if base_teu:
            out['off_teu_ratio6'] = last['teu'] / base_teu
    if teu3:
        out['off_teu_trail3'] = statistics.mean(teu3)

    same = [v['dwell'] for (p, y, m), v in series.items()
            if p == port and m == anchor.month and y < anchor.year
            and (y * 12 + (m - 1)) <= cutoff and v.get('dwell') is not None]
    all_dwell = [v['dwell'] for (p, y, m), v in series.items()
                 if p == port and (y * 12 + (m - 1)) <= cutoff
                 and v.get('dwell') is not None]
    if same and all_dwell:
        base = statistics.median(all_dwell)
        if base > 0:
            out['off_dwell_seasonal'] = statistics.median(same) / base
    return out
