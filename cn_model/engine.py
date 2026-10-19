"""Prediction engine.

Two-stage empirical model:
  Stage 1  dwell   : Port ETA (vessel discharge anchor) -> RAMPED (loaded on rail)
                     factors: port of discharge, month of year, weekday of anchor
  Stage 2  transit : RAMPED -> AT DESTINATION (rail arrival)
                     factors: lane (origin ramp -> destination), month, weekday of ramp
  Stage 3  deramp  : AT DESTINATION -> DERAMPED (same/next shift)

All predictions are quantile-based (p10 / p50 / p90) with hierarchical shrinkage
so sparse cells borrow strength from their parent distribution.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta

from cn_model.external import default_series, seasonal_factor

SHRINK_K = 6.0

# tunables (overridable for backtesting)
TUNING = dict(
    dwell_month_k=6.0, dwell_month_floor=0.6, dwell_month_cap=1.8, use_dwell_month=True,
    dwell_dow_k=4.0, dwell_dow_floor=0.7, dwell_dow_cap=1.5, use_dwell_dow=True,
    transit_month_k=5.0, transit_month_floor=0.6, transit_month_cap=1.8, use_transit_month=True,
    use_external_month=False, external_k=8.0,
    use_external_missing=True,
    hol_floor=0.5, hol_cap=1.6,   # dps
)


def _quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] * (hi - pos) + sorted_vals[hi] * (pos - lo)


def _q3(vals):
    s = sorted(v for v in vals if v is not None)
    return {'p10': _quantile(s, 0.10), 'p50': _quantile(s, 0.50), 'p90': _quantile(s, 0.90), 'n': len(s)}


def _shrink_cell(cell_q, cell_n, parent_q, k=SHRINK_K):
    """Blend a cell's quantiles toward the parent by sample size."""
    w = cell_n / (cell_n + k)
    out = {}
    for key in ('p10', 'p50', 'p90'):
        cv, pv = cell_q.get(key), parent_q.get(key)
        if cv is None:
            out[key] = pv
        elif pv is None:
            out[key] = cv
        else:
            out[key] = w * cv + (1 - w) * pv
    return _fix_q(out)


def _median(vals):
    s = sorted(vals)
    return s[len(s) // 2] if s else None


def _fix_q(q, floor=0.0):
    """Make quantiles monotonic; shrinkage can otherwise cross them."""
    p50 = q.get('p50')
    if p50 is None:
        return q
    out = dict(q)
    p10 = q.get('p10')
    p90 = q.get('p90')
    out['p50'] = max(floor, p50)
    out['p10'] = min(max(floor, p10 if p10 is not None else out['p50']), out['p50'])
    out['p90'] = max(p90 if p90 is not None else out['p50'], out['p50'])
    return out


def _fix_dates(d, floor, center_floor=None):
    """Keep a p10/p50/p90 date triple ordered and at/after the anchor."""
    p50 = d['p50']
    if center_floor is not None:
        p50 = max(p50, center_floor)
    p10 = min(max(d['p10'], floor), p50)
    p90 = max(d['p90'], p50)
    return {'p10': p10, 'p50': p50, 'p90': p90}


def _month_factor(vals_by_key, parent_med, k=SHRINK_K, floor=0.6, cap=1.8):
    """Multiplicative month factors with shrinkage toward 1.0."""
    out = {}
    for key, vals in vals_by_key.items():
        n = len(vals)
        if parent_med in (None, 0):
            continue
        raw = _median(vals) / parent_med
        raw = max(floor, min(cap, raw))
        out[key] = (n * raw + k * 1.0) / (n + k)
    return out


class ETAPredictor:
    def __init__(self, rows=None, stats=None, external=None):
        self.external = default_series() if external is None else external
        self.stats = stats if stats is not None else self._fit(rows or [])

    # ------------------------------------------------------------------ fit
    def _fit(self, rows):
        dwell_by_pod = defaultdict(list)
        dwell_by_pod_month = defaultdict(list)
        dwell_by_pod_dow = defaultdict(list)
        transit_by_lane = defaultdict(list)
        transit_by_lane_month = defaultdict(list)
        transit_by_pod_dest = defaultdict(list)
        transit_by_dest = defaultdict(list)
        deramp_by_dest = defaultdict(list)
        depart_by_lane = defaultdict(list)

        for r in rows:
            pod = r.get('pod')
            ramp = r.get('ramp_station')
            dest = r.get('rail_dest')
            pe = r.get('port_eta')
            rt = r.get('ramp_ts')
            d = {}
            if pe and r.get('dwell_days') is not None:
                d['dwell'] = float(r['dwell_days'])
                dwell_by_pod[pod].append(d['dwell'])
                dwell_by_pod_month[(pod, pe.month)].append(d['dwell'])
                dwell_by_pod_dow[(pod, pe.weekday())].append(d['dwell'])
            if rt and r.get('transit_days') is not None:
                lane = (ramp, dest)
                transit_by_lane[lane].append(float(r['transit_days']))
                transit_by_lane_month[(lane, rt.month)].append(float(r['transit_days']))
                transit_by_pod_dest[(pod, dest)].append(float(r['transit_days']))
                transit_by_dest[dest].append(float(r['transit_days']))
                if r.get('first_dep_ts'):
                    depart_by_lane[lane].append((r['first_dep_ts'] - rt).total_seconds() / 86400.0)
            if r.get('at_dest_ts') and r.get('deramp_ts'):
                deramp_by_dest[dest].append((r['deramp_ts'] - r['at_dest_ts']).total_seconds() / 86400.0)

        stats = {}
        stats['dwell_pod'] = {pod: _q3(v) for pod, v in dwell_by_pod.items()}
        stats['dwell_month_factor'] = {}
        stats['dwell_dow_factor'] = {}
        stats['dwell_month_n'] = {f'{p}|{m}': len(v) for (p, m), v in dwell_by_pod_month.items()}
        for pod in dwell_by_pod:
            parent = _median(dwell_by_pod[pod])
            if TUNING['use_dwell_month']:
                cells = {m: v for (p, m), v in dwell_by_pod_month.items() if p == pod}
                stats['dwell_month_factor'][pod] = _month_factor(
                    cells, parent, k=TUNING['dwell_month_k'],
                    floor=TUNING['dwell_month_floor'], cap=TUNING['dwell_month_cap'])
            if TUNING['use_dwell_dow']:
                cells2 = {dw: v for (p, dw), v in dwell_by_pod_dow.items() if p == pod}
                stats['dwell_dow_factor'][pod] = _month_factor(
                    cells2, parent, k=TUNING['dwell_dow_k'],
                    floor=TUNING['dwell_dow_floor'], cap=TUNING['dwell_dow_cap'])

        stats['transit_lane'] = {f'{l[0]}|{l[1]}': _q3(v) for l, v in transit_by_lane.items()}
        stats['transit_pod_dest'] = {f'{k[0]}|{k[1]}': _q3(v) for k, v in transit_by_pod_dest.items()}
        stats['transit_dest'] = {k: _q3(v) for k, v in transit_by_dest.items()}
        stats['transit_all'] = _q3([x for v in transit_by_lane.values() for x in v])

        stats['transit_month_factor'] = {}
        if TUNING['use_transit_month']:
            for lane in transit_by_lane:
                parent = _median(transit_by_lane[lane])
                cells = {m: v for (l, m), v in transit_by_lane_month.items() if l == lane}
                stats['transit_month_factor'][f'{lane[0]}|{lane[1]}'] = _month_factor(
                    cells, parent, k=TUNING['transit_month_k'],
                    floor=TUNING['transit_month_floor'], cap=TUNING['transit_month_cap'])

        stats['deramp_dest'] = {k: _q3(v) for k, v in deramp_by_dest.items()}
        stats['deramp_all'] = _q3([x for v in deramp_by_dest.values() for x in v])
        stats['depart_lane'] = {f'{l[0]}|{l[1]}': _q3(v) for l, v in depart_by_lane.items()}
        return stats

    # ------------------------------------------------------------------ queries
    def lanes(self):
        out = []
        for key, q in self.stats['transit_lane'].items():
            ramp, dest = key.split('|')
            out.append((ramp, dest, q['n'], q))
        return sorted(out, key=lambda x: -x[2])

    def known_pods(self):
        return sorted(self.stats['dwell_pod'].keys())

    def known_dests(self):
        return sorted(self.stats['transit_dest'].keys())

    def known_ramps(self):
        ramps = {}
        for key, q in self.stats['transit_lane'].items():
            ramp, dest = key.split('|')
            ramps.setdefault(ramp, {'destinations': [], 'n': 0})
            ramps[ramp]['destinations'].append(dest)
            ramps[ramp]['n'] += q['n']
        return dict(sorted(ramps.items()))

    # ------------------------------------------------------------------ predict
    def _transit_cell(self, pod, ramp, dest):
        lane_key = f'{ramp}|{dest}'
        pod_dest_key = f'{pod}|{dest}'
        lane = self.stats['transit_lane'].get(lane_key)
        pod_dest = self.stats['transit_pod_dest'].get(pod_dest_key)
        dest_q = self.stats['transit_dest'].get(dest)
        base = self.stats['transit_all']
        source = 'global'
        if dest_q and dest_q['n'] >= 10:
            base, source = dest_q, f'destination {dest}'
        if pod_dest and pod_dest['n'] >= 5:
            base = _shrink_cell(pod_dest, pod_dest['n'], base)
            source = f'port->destination'
        if lane and lane['n'] >= 3:
            base = _shrink_cell(lane, lane['n'], base)
            source = f'lane {ramp} -> {dest}'
        elif lane:
            base = _shrink_cell(lane, lane['n'], base)
        return dict(base), source

    def predict(self, pod=None, ramp=None, dest=None, port_eta=None, discharge_date=None,
                tracker_rail_eta=None, sibling_ramp_dates=None, ml_days=None):
        """Predict RAMPED and arrival dates.

        pod / ramp / dest : strings (ramp and pod can both be given; ramp drives transit)
        port_eta          : datetime/date of vessel ETA at port (the anchor)
        discharge_date    : optional actual vessel discharge date (overrides anchor)
        tracker_rail_eta  : optional carrier rail ETA (blend input)
        sibling_ramp_dates: optional ramp dates of other containers on the same vessel
                            (same port of discharge + port ETA). Anchors the ramp
                            estimate on the group's observed behaviour.
        ml_days           : optional dict {'dwell': {...}, 'transit': {...}} with
                            p10/p50/p90 day estimates from the ML assist model.
                            Blended 50/50 on the centre; interval width kept.
        """
        notes = []
        anchor = discharge_date or port_eta
        if isinstance(anchor, datetime):
            anchor_d = anchor.date()
        elif anchor is not None:
            anchor_d = anchor
        else:
            raise ValueError('need port_eta or discharge_date')

        # ---- stage 1: dwell
        dwell_q = self.stats['dwell_pod'].get(pod)
        dwell_source = f'port {pod}'
        if not dwell_q:
            all_dwell = [v for q in self.stats['dwell_pod'].values() for v in (q['p10'], q['p50'], q['p90'])]
            dwell_q = _q3(all_dwell) if all_dwell else {'p10': 5, 'p50': 8, 'p90': 14, 'n': 0}
            dwell_source = 'global fallback'
            notes.append(f'No history for port {pod}; using global dwell estimate.')
        dwell_q = dict(dwell_q)
        mf = self.stats['dwell_month_factor'].get(pod, {}).get(anchor_d.month, 1.0)
        df = self.stats['dwell_dow_factor'].get(pod, {}).get(anchor_d.weekday(), 1.0)
        n_month = self.stats.get('dwell_month_n', {}).get(f'{pod}|{anchor_d.month}', 0)
        ef = None
        ext_prior = False
        if TUNING.get('use_external_month', True):
            ef = seasonal_factor(self.external, pod, anchor_d.year, anchor_d.month)
        elif n_month == 0 and TUNING.get('use_external_missing', True):
            ef = seasonal_factor(self.external, pod, anchor_d.year, anchor_d.month)
            ext_prior = ef is not None
        if ef is not None:
            if ext_prior:
                mf = ef
                notes.append(f'No {anchor_d.strftime("%B")} history for {pod.title()}; '
                             f'official prior factor x{ef:.2f} applied to port dwell.')
            else:
                w_ext = n_month / (n_month + TUNING['external_k'])
                mf = math.exp(w_ext * math.log(max(mf, 1e-6)) + (1 - w_ext) * math.log(ef))
                if abs(ef - 1) >= 0.05:
                    notes.append(f'Official {pod.title()} {anchor_d.strftime("%B")} dwell factor '
                                 f'x{ef:.2f} vs prior-year median; blended into seasonal adjustment.')
        if abs(mf - 1) > 0.04:
            notes.append(f'Seasonal factor for {anchor_d.strftime("%B")}: x{mf:.2f} on port dwell.')
        dwell = {k: max(0.0, dwell_q[k] * mf * df) for k in ('p10', 'p50', 'p90')}

        if discharge_date is not None and port_eta is not None:
            eta_d = port_eta.date() if isinstance(port_eta, datetime) else port_eta
            elapsed = max(0, (anchor_d - eta_d).days)
            if elapsed:
                dwell = {k: max(0.5, dwell[k] - elapsed) for k in dwell}
                notes.append(f'Confirmed discharge is {elapsed}d after the port ETA; '
                             f'only the remaining dwell is estimated.')

        ramp_dates = {k: anchor_d + timedelta(days=dwell[k]) for k in dwell}

        # ---- same-vessel adjustment
        sib_n = 0
        if sibling_ramp_dates:
            sib = []
            for d in sibling_ramp_dates:
                if isinstance(d, datetime):
                    d = d.date()
                if d is not None:
                    sib.append(d)
            sib = sorted(sib)
            if sib:
                sib_n = len(sib)
                ords = [d.toordinal() for d in sib]
                mid = len(ords) // 2
                if len(ords) % 2:
                    ord_med = ords[mid]
                else:
                    ord_med = (ords[mid - 1] + ords[mid]) / 2.0
                g = date.fromordinal(int(round(ord_med)))
                delta = g - ramp_dates['p50']
                ramp_dates = {k: v + delta for k, v in ramp_dates.items()}
                dwell = {k: (ramp_dates[k] - anchor_d).days for k in ramp_dates}
                notes.append(f'Same-vessel history: {sib_n} container(s) with the same port ETA '
                             f'already ramped; ramp estimate aligned to their median '
                             f'({g:%d %b %Y}).')
        ramp_dates = _fix_dates(ramp_dates, anchor_d)
        dwell = {k: float((ramp_dates[k] - anchor_d).days) for k in ramp_dates}
        ramp_source = f'{dwell_source} + {sib_n} same-vessel' if sib_n else dwell_source

        # ---- stage 2: transit
        transit, t_source = self._transit_cell(pod, ramp, dest)
        mf2 = self.stats['transit_month_factor'].get(f'{ramp}|{dest}', {}).get(ramp_dates['p50'].month, 1.0)
        if abs(mf2 - 1) > 0.05:
            notes.append(f'Seasonal factor for ramp month {ramp_dates["p50"].strftime("%B")}: x{mf2:.2f} on transit.')
        transit = {k: max(0.2, transit[k] * mf2) for k in ('p10', 'p50', 'p90')}

        # combine dwell + transit uncertainty for arrival interval (p10/p90 are additive)
        arrival_dates = {
            'p10': ramp_dates['p10'] + timedelta(days=transit['p10']),
            'p50': ramp_dates['p50'] + timedelta(days=transit['p50']),
            'p90': ramp_dates['p90'] + timedelta(days=transit['p90']),
        }
        arrival_dates = _fix_dates(arrival_dates, ramp_dates['p10'], ramp_dates['p50'])

        # ---- optional ML assist: 50/50 centre blend, statistical interval width kept
        ml_used = bool(ml_days and ml_days.get('dwell') and ml_days.get('transit'))
        if ml_used:
            md, mt = ml_days['dwell'], ml_days['transit']
            ramp_shift = (md['p50'] - dwell['p50']) / 2.0
            dwell = {k: dwell[k] + ramp_shift for k in dwell}
            ramp_dates = {k: anchor_d + timedelta(days=round(dwell[k])) for k in dwell}
            dwell = {k: float((ramp_dates[k] - anchor_d).days) for k in dwell}

            eng_arr_days = (arrival_dates['p50'] - anchor_d).days
            arr_shift = ((md['p50'] + mt['p50']) - eng_arr_days) / 2.0
            arr_offsets = {k: (arrival_dates[k] - anchor_d).days + arr_shift for k in arrival_dates}
            arrival_dates = {k: anchor_d + timedelta(days=round(v)) for k, v in arr_offsets.items()}
            arrival_dates = _fix_dates(arrival_dates, ramp_dates['p10'], ramp_dates['p50'])
            transit = _fix_q({k: float((arrival_dates[k] - ramp_dates[k]).days)
                              for k in transit}, floor=0.2)
            notes.append('ML assist: gradient-boosted quantile estimate blended 50/50 '
                         'with the statistical model.')
            ramp_source = f'{ramp_source} + ML'
            t_source = f'{t_source} + ML'

        # ---- stage 3: deramp
        dq = self.stats['deramp_dest'].get(dest) or self.stats['deramp_all']
        deramp_dates = {k: arrival_dates['p50'] + timedelta(days=dq[k]) for k in ('p10', 'p50', 'p90')}

        # ---- optional tracker blend
        if tracker_rail_eta is not None:
            arr_before = arrival_dates['p50']
            t = tracker_rail_eta
            if isinstance(t, datetime):
                t = t.date()
            ra = arrival_dates['p50']
            gap = (t - ra).days
            if abs(gap) <= 3:
                half = gap / 2.0
                if gap > 0:
                    half = math.floor(half + 0.5)
                elif gap < 0:
                    half = math.ceil(half - 0.5)
                else:
                    half = 0
                blended = ra + timedelta(days=half)
                arrival_dates['p50'] = blended
                arrival_dates['p10'] = min(arrival_dates['p10'], blended)
                arrival_dates['p90'] = max(arrival_dates['p90'], blended)
                notes.append(f'Carrier rail ETA {t} matches model within {abs(gap)}d; using 50/50 average.')
            else:
                arrival_dates['p50'] = t
                arrival_dates['p10'] = min(arrival_dates['p10'], t)
                arrival_dates['p90'] = max(arrival_dates['p90'], t)
                notes.append(f'Carrier rail ETA {t} differs from model by {gap:+d}d; '
                             f'carrier updates are usually more accurate, using carrier date as primary.')
            arrival_dates = _fix_dates(arrival_dates, ramp_dates['p10'])
            dshift = arrival_dates['p50'] - arr_before
            if dshift:
                deramp_dates = {k: v + dshift for k, v in deramp_dates.items()}
                transit = _fix_q({k: float((arrival_dates[k] - ramp_dates[k]).days)
                                  for k in transit}, floor=0.2)

        n_lane = None
        for key, q in self.stats['transit_lane'].items():
            r0, d0 = key.split('|')
            if r0 == ramp and d0 == dest:
                n_lane = q['n']
        return dict(
            anchor=anchor_d,
            ramp={'p10': ramp_dates['p10'], 'p50': ramp_dates['p50'], 'p90': ramp_dates['p90'],
                  'n': dwell_q['n'], 'source': ramp_source, 'days': dwell,
                  'anchor_days': {k: (ramp_dates[k] - anchor_d).days for k in ramp_dates}},
            arrival={'p10': arrival_dates['p10'], 'p50': arrival_dates['p50'], 'p90': arrival_dates['p90'],
                     'n': n_lane, 'source': t_source, 'days': transit,
                     'anchor_days': {k: (arrival_dates[k] - anchor_d).days for k in arrival_dates}},
            deramp={'p10': deramp_dates['p10'], 'p50': deramp_dates['p50'], 'p90': deramp_dates['p90'],
                    'days': dq,
                    'anchor_days': {k: (deramp_dates[k] - anchor_d).days for k in deramp_dates}},
            notes=notes,
        )

    # ------------------------------------------------------------------ io
    def save(self, path):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.stats, f, indent=1)

    @classmethod
    def load(cls, path):
        with open(path, encoding='utf-8') as f:
            return cls(stats=json.load(f))


if __name__ == '__main__':
    from cn_model.data import build_master
    rows = build_master()
    m = ETAPredictor(rows)
    for ramp, dest, n, q in m.lanes()[:12]:
        print(f'  n={n:4d}  {ramp:22s} -> {dest:22s} p50={q["p50"]:.1f}d')
    print('pods:', m.known_pods())
    demo = m.predict(pod='VANCOUVER', ramp='ROBERTS BANK', dest='BRAMPTON INTER TERM',
                     port_eta=datetime(2026, 10, 20))
    print(json.dumps({k: str(v) for k, v in demo.items() if k != 'notes'}, indent=1))
    print('notes:', demo['notes'])
