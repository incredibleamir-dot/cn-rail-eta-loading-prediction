"""Backtest: leave-one-out over all journeys.

For each journey, fit the model on every OTHER journey and predict with the
same anchor the app would receive (the vessel Port ETA), then compare with
actual events:
   ramp_actual     = RAMPED timestamp
   arrival_actual  = AT DESTINATION timestamp

Also compares against the carrier's own "Rail ETA" where available,
and evaluates an optional 50/50 blend with the carrier value.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime

from cn_model.data import build_master
from cn_model.engine import ETAPredictor


def daterr(pred, actual):
    if not pred or not actual or isinstance(actual, str):
        return None
    if isinstance(pred, datetime):
        pred = pred.date()
    return (pred - actual.date()).days


def sibling_ramps(train, r):
    """Ramp dates of other containers sharing the same port and Port ETA date."""
    return [s['ramp_ts'] for s in train
            if s['pod'] == r['pod'] and s['port_eta'] and s['ramp_ts']
            and s['port_eta'].date() == r['port_eta'].date()]


def sibling_dwells(train, r):
    """Observed port-ETA dwell of same-vessel containers in the training rows."""
    out = []
    for s in train:
        if (s['pod'] == r['pod'] and s['port_eta'] and s['ramp_ts']
                and s['port_eta'].date() == r['port_eta'].date()):
            out.append((s['ramp_ts'].date() - s['port_eta'].date()).days)
    return out


def _folds(n, k=5, seed=42):
    import random
    idx = list(range(n))
    random.Random(seed).shuffle(idx)
    return [sorted(idx[i::k]) for i in range(k)]


def evaluate_ml(rows, k=5, seed=42, use_external=False):
    """5-fold CV: engine alone vs engine + ML assist on identical splits."""
    from cn_model.ml import HAVE_ML, MLPredictor
    if not HAVE_ML:
        print('ML assist: scikit-learn not installed, skipped.')
        return
    base = {'ramp': [], 'arr': []}
    ens = {'ramp': [], 'arr': []}
    cov_b = {'ramp': [0, 0], 'arr': [0, 0]}
    cov_e = {'ramp': [0, 0], 'arr': [0, 0]}
    for fold in _folds(len(rows), k, seed):
        test = [rows[i] for i in fold]
        train = [r for i, r in enumerate(rows) if i not in set(fold)]
        engine = ETAPredictor(train)
        ml = MLPredictor(use_external=use_external).fit(train)
        for r in test:
            pool = [s for s in rows if s is not r]
            sd = sibling_ramps(pool, r)
            out = engine.predict(pod=r['pod'], ramp=r['ramp_station'], dest=r['rail_dest'],
                                 port_eta=r['port_eta'], sibling_ramp_dates=sd)
            md = None
            if ml.ok:
                md = ml.predict(pod=r['pod'], ramp=r['ramp_station'], dest=r['rail_dest'],
                                port_eta=r['port_eta'], sibling_dwells=sibling_dwells(pool, r))
            out2 = engine.predict(pod=r['pod'], ramp=r['ramp_station'], dest=r['rail_dest'],
                                  port_eta=r['port_eta'], sibling_ramp_dates=sd, ml_days=md)
            if r['ramp_ts']:
                a = r['ramp_ts'].date()
                base['ramp'].append(daterr(out['ramp']['p50'], r['ramp_ts']))
                ens['ramp'].append(daterr(out2['ramp']['p50'], r['ramp_ts']))
                cov_b['ramp'][1] += 1
                cov_e['ramp'][1] += 1
                cov_b['ramp'][0] += out['ramp']['p10'] <= a <= out['ramp']['p90']
                cov_e['ramp'][0] += out2['ramp']['p10'] <= a <= out2['ramp']['p90']
            if r['at_dest_ts']:
                a = r['at_dest_ts'].date()
                base['arr'].append(daterr(out['arrival']['p50'], r['at_dest_ts']))
                ens['arr'].append(daterr(out2['arrival']['p50'], r['at_dest_ts']))
                cov_b['arr'][1] += 1
                cov_e['arr'][1] += 1
                cov_b['arr'][0] += out['arrival']['p10'] <= a <= out['arrival']['p90']
                cov_e['arr'][0] += out2['arrival']['p10'] <= a <= out2['arrival']['p90']
    print()
    print(f'=== 5-FOLD CV (seed {seed}): engine vs engine+ML'
          f'{" + official external" if use_external else ""} ===')
    for label, d, cov in (('engine', base, cov_b), ('engine+ML', ens, cov_e)):
        er = [x for x in d['ramp'] if x is not None]
        ea = [x for x in d['arr'] if x is not None]
        if er:
            print(f'{label:10s} ramp   MAE={statistics.mean(abs(x) for x in er):5.2f}d '
                  f'bias={statistics.mean(er):+5.2f}d  cov={cov["ramp"][0]/max(1, cov["ramp"][1])*100:3.0f}%')
        if ea:
            print(f'{label:10s} arrival MAE={statistics.mean(abs(x) for x in ea):5.2f}d '
                  f'bias={statistics.mean(x for x in ea):+5.2f}d  cov={cov["arr"][0]/max(1, cov["arr"][1])*100:3.0f}%')


def summarize(name, errs):
    errs = [e for e in errs if e is not None]
    if not errs:
        print(f'{name:34s} n=0')
        return None
    absd = [abs(e) for e in errs]
    within1 = sum(1 for e in absd if e <= 1) / len(absd)
    within2 = sum(1 for e in absd if e <= 2) / len(absd)
    mae = statistics.mean(absd)
    bias = statistics.mean(errs)
    print(f'{name:34s} n={len(errs):3d}  MAE={mae:5.2f}d  bias={bias:+5.2f}d  |err|<=1d: {within1*100:4.0f}%  <=2d: {within2*100:4.0f}%')
    return mae


def build_diagnostics(rows, train_frac=0.65, use_external=False):
    """Holdout diagnostics for the UI: engine vs ML assist, per-month arrival error."""
    ordered = sorted([r for r in rows if r.get('port_eta')], key=lambda r: r['port_eta'])
    if len(ordered) < 60:
        return None
    cut = max(40, min(len(ordered) - 10, int(len(ordered) * train_frac)))
    train, test = ordered[:cut], ordered[cut:]
    engine = ETAPredictor(train)
    ml = None
    try:
        from cn_model.ml import HAVE_ML, MLPredictor
        if HAVE_ML:
            ml = MLPredictor(use_external=use_external).fit(train)
    except Exception:
        ml = None

    er, ea, mr, ma, ca = [], [], [], [], []
    cov = {'ramp_e': 0, 'ramp_m': 0, 'arr_e': 0, 'arr_m': 0}
    n_ramp = n_arr = 0
    months = defaultdict(lambda: {'e': [], 'm': []})
    for r in test:
        pool = [s for s in ordered if s is not r]
        sd = sibling_ramps(pool, r)
        out = engine.predict(pod=r['pod'], ramp=r['ramp_station'], dest=r['rail_dest'],
                             port_eta=r['port_eta'], sibling_ramp_dates=sd)
        md = None
        if ml is not None and ml.ok:
            md = ml.predict(r['pod'], r['ramp_station'], r['rail_dest'], r['port_eta'],
                            sibling_dwells(pool, r))
        out2 = engine.predict(pod=r['pod'], ramp=r['ramp_station'], dest=r['rail_dest'],
                              port_eta=r['port_eta'], sibling_ramp_dates=sd, ml_days=md)
        label = f'{r["port_eta"]:%b %y}'
        if r['ramp_ts']:
            e = daterr(out['ramp']['p50'], r['ramp_ts'])
            m = daterr(out2['ramp']['p50'], r['ramp_ts'])
            er.append(e)
            mr.append(m)
            n_ramp += 1
            cov['ramp_e'] += out['ramp']['p10'] <= r['ramp_ts'].date() <= out['ramp']['p90']
            cov['ramp_m'] += out2['ramp']['p10'] <= r['ramp_ts'].date() <= out2['ramp']['p90']
        if r['at_dest_ts']:
            e = daterr(out['arrival']['p50'], r['at_dest_ts'])
            m = daterr(out2['arrival']['p50'], r['at_dest_ts'])
            ea.append(e)
            ma.append(m)
            n_arr += 1
            months[label]['e'].append(abs(e))
            if m is not None:
                months[label]['m'].append(abs(m))
            cov['arr_e'] += out['arrival']['p10'] <= r['at_dest_ts'].date() <= out['arrival']['p90']
            cov['arr_m'] += out2['arrival']['p10'] <= r['at_dest_ts'].date() <= out2['arrival']['p90']
        if r['tracker_rail_eta']:
            ca.append(daterr(r['tracker_rail_eta'], r['at_dest_ts']))

    def mae(xs):
        xs = [x for x in xs if x is not None]
        return statistics.mean(abs(x) for x in xs) if xs else None

    month_rows = []
    for label in sorted(months, key=lambda s: datetime.strptime(s, '%b %y')):
        e = months[label]['e']
        m = months[label]['m']
        month_rows.append((label, statistics.mean(e) if e else None,
                           statistics.mean(m) if m else None, len(e)))
    return {
        'n_train': len(train), 'n_test': len(test),
        'cut': test[0]['port_eta'].date(),
        'ramp_engine': mae(er), 'ramp_ml': mae(mr),
        'arr_engine': mae(ea), 'arr_ml': mae(ma), 'arr_carrier': mae(ca),
        'cov_ramp_engine': (cov['ramp_e'] / n_ramp) if n_ramp else None,
        'cov_ramp_ml': (cov['ramp_m'] / n_ramp) if n_ramp else None,
        'cov_arr_engine': (cov['arr_e'] / n_arr) if n_arr else None,
        'cov_arr_ml': (cov['arr_m'] / n_arr) if n_arr else None,
        'months': month_rows,
        'ml_ok': bool(ml is not None and ml.ok),
    }


def main():
    rows = build_master()
    rows = [r for r in rows if r['port_eta']]
    print(f'backtest rows: {len(rows)}')

    # LOO
    preds = []
    for i, r in enumerate(rows):
        train = rows[:i] + rows[i + 1:]
        m = ETAPredictor(train)
        out = m.predict(pod=r['pod'], ramp=r['ramp_station'], dest=r['rail_dest'],
                        port_eta=r['port_eta'], sibling_ramp_dates=sibling_ramps(train, r))
        preds.append((r, out))

    print()
    print('=== MODEL (anchor = Port ETA) ===')
    summarize('RAMPED date', [daterr(o['ramp']['p50'], r['ramp_ts']) for r, o in preds])
    summarize('arrival (AT DEST) date', [daterr(o['arrival']['p50'], r['at_dest_ts']) for r, o in preds])
    summarize('deramp date', [daterr(o['deramp']['p50'], r['deramp_ts']) for r, o in preds])

    # interval coverage
    cov80_arr = sum(1 for r, o in preds if r['at_dest_ts'] and o['arrival']['p10'] and o['arrival']['p90']
                    and o['arrival']['p10'] <= r['at_dest_ts'].date() <= o['arrival']['p90'])
    n_arr = sum(1 for r, o in preds if r['at_dest_ts'])
    cov80_ramp = sum(1 for r, o in preds if r['ramp_ts']
                     and o['ramp']['p10'] <= r['ramp_ts'].date() <= o['ramp']['p90'])
    print(f'RAMPED actual within p10-p90:  {cov80_ramp}/{len(preds)} = {cov80_ramp/len(preds)*100:.0f}%')
    print(f'arrival actual within p10-p90: {cov80_arr}/{n_arr} = {cov80_arr/n_arr*100:.0f}%')

    print()
    print('=== Carrier rail ETA baseline on the same rows ===')
    summarize('carrier ETA -> arrival', [daterr(r['tracker_rail_eta'], r['at_dest_ts']) for r, o in preds])

    print()
    print('=== BLEND (engine rule: 50/50 if gap<=3 else carrier) -> arrival ===')
    from cn_model.engine import ETAPredictor as _P
    blend_errs = []
    for i, (r, o) in enumerate(preds):
        if not r['tracker_rail_eta'] or not r['at_dest_ts']:
            continue
        train = rows[:i] + rows[i + 1:]
        out2 = ETAPredictor(train).predict(
            pod=r['pod'], ramp=r['ramp_station'], dest=r['rail_dest'],
            port_eta=r['port_eta'], tracker_rail_eta=r['tracker_rail_eta'],
            sibling_ramp_dates=sibling_ramps(train, r))
        blend_errs.append(daterr(out2['arrival']['p50'], r['at_dest_ts']))
    summarize('blend rule -> arrival', blend_errs)

    evaluate_ml(rows)

    print()
    print('=== MODEL by lane (arrival, p50) ===')
    byl = defaultdict(list)
    for r, o in preds:
        byl[(r['ramp_station'], r['rail_dest'])].append((daterr(o['arrival']['p50'], r['at_dest_ts']),
                                                         daterr(o['ramp']['p50'], r['ramp_ts'])))
    for k, v in sorted(byl.items(), key=lambda kv: -len(kv[1])):
        ae = [x[0] for x in v if x[0] is not None]
        re = [x[1] for x in v if x[1] is not None]
        if len(ae) < 3:
            continue
        print(f'  {k[0]:22s} -> {k[1]:22s} n={len(ae):3d}  rampMAE={statistics.mean(abs(x) for x in re):5.2f}  arrMAE={statistics.mean(abs(x) for x in ae):5.2f}')

    print()
    print('=== MODEL by pod (ramp date) ===')
    byp = defaultdict(list)
    for r, o in preds:
        byp[r['pod']].append(daterr(o['ramp']['p50'], r['ramp_ts']))
    for k, v in sorted(byp.items(), key=lambda kv: -len(kv[1])):
        v = [x for x in v if x is not None]
        if len(v) >= 3:
            print(f'  {k:14s} n={len(v):3d}  rampMAE={statistics.mean(abs(x) for x in v):5.2f}  bias={statistics.mean(v):+5.2f}')

    # worst cases
    print()
    print('=== worst 12 arrival misses ===')
    scored = []
    for r, o in preds:
        e = daterr(o['arrival']['p50'], r['at_dest_ts'])
        if e is not None:
            scored.append((abs(e), e, r['container'], r['pod'], r['ramp_station'], r['rail_dest'],
                           o['arrival']['p50'], r['at_dest_ts'].date(), r['port_eta'].date()))
    for s in sorted(scored, reverse=True)[:12]:
        print(f'  err={s[0]:2d}d pred={s[6]} actual={s[7]} anchor={s[8]} {s[2]} {s[3]} {s[4]}->{s[5]}')


if __name__ == '__main__':
    main()
