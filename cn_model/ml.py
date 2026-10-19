"""Gradient-boosted quantile models that assist the statistical engine.

Optional component: if scikit-learn is not installed (or fitting fails), the
app keeps using the statistical engine alone.

The models learn dwell (Port ETA -> RAMPED) and rail transit (RAMPED -> arrival)
from the same master journeys. Inputs: port / ramp / destination / month /
weekday plus same-vessel sibling statistics. Three quantile models (p10, p50,
p90) are fitted per target with histogram gradient boosting.

The app blends the ML centre 50/50 with the statistical engine's centre and
keeps the engine's calibrated interval width (see cn_model/engine.py).
"""
from __future__ import annotations

import statistics

try:
    import pandas as pd
    from sklearn.ensemble import HistGradientBoostingRegressor
    HAVE_ML = True
except Exception:  # pragma: no cover
    HAVE_ML = False

from cn_model.external import EXT_COLS, default_series, official_features

CATS = ('pod', 'ramp', 'dest', 'month', 'dow')
NUM = ('sib_n', 'sib_dwell', 'sib_spread')
QS = (0.1, 0.5, 0.9)


def _sib_stats(dwells):
    vals = sorted(float(v) for v in dwells or [] if v is not None)
    if not vals:
        return 0.0, -1.0, -1.0
    return float(len(vals)), statistics.median(vals), float(vals[-1] - vals[0])


def _sibling_dwells(data, r):
    out = []
    for s in data:
        if s is r or s['container'] == r['container']:
            continue
        if (s['pod'] == r['pod'] and s.get('port_eta') and r.get('port_eta')
                and s.get('ramp_ts')
                and s['port_eta'].date() == r['port_eta'].date()):
            out.append((s['ramp_ts'].date() - s['port_eta'].date()).days)
    return out


class MLPredictor:
    def __init__(self, use_external=False, external_lag=2, external_cols=None):
        self.ok = False
        self.models = {}
        self.cat_cols = []
        self.reason = ''
        self.use_external = use_external
        self.external_lag = external_lag
        cols = list(EXT_COLS) if external_cols is None else list(external_cols)
        self.ext_cols = cols if use_external else []
        self.num_cols = list(NUM) + self.ext_cols

    def fit(self, rows):
        if not HAVE_ML:
            self.reason = 'scikit-learn not installed'
            return self
        data = [r for r in rows if r.get('port_eta')]
        if len(data) < 40:
            self.reason = 'not enough journeys'
            return self
        try:
            series = default_series() if self.use_external else None
            recs = []
            for r in data:
                n, med, spread = _sib_stats(_sibling_dwells(data, r))
                rec = {
                    'pod': r['pod'] or 'NA', 'ramp': r['ramp_station'] or 'NA',
                    'dest': r['rail_dest'] or 'NA',
                    'month': r['port_eta'].month, 'dow': r['port_eta'].weekday(),
                    'sib_n': n, 'sib_dwell': med, 'sib_spread': spread,
                    'dwell': r.get('dwell_days'), 'transit': r.get('transit_days'),
                }
                if self.use_external:
                    rec.update(official_features(series, r['pod'], r['port_eta'],
                                                 self.external_lag))
                recs.append(rec)
            df = pd.DataFrame(recs)
            dummies = pd.get_dummies(df[list(CATS)].astype(str), dummy_na=False)
            self.cat_cols = list(dummies.columns)
            X = pd.concat([dummies, df[self.num_cols].astype(float)], axis=1)
            for target in ('dwell', 'transit'):
                mask = df[target].notna().values
                if mask.sum() < 30:
                    continue
                for q in QS:
                    model = HistGradientBoostingRegressor(
                        loss='quantile', quantile=q, max_iter=300, max_depth=3,
                        learning_rate=0.05, min_samples_leaf=10,
                        l2_regularization=1.0, random_state=7)
                    model.fit(X[mask], df[target].values[mask])
                    self.models[(target, q)] = model
            self.ok = all((t, q) in self.models for t in ('dwell', 'transit') for q in QS)
            if not self.ok:
                self.reason = 'missing targets'
        except Exception as exc:  # pragma: no cover
            self.reason = f'{type(exc).__name__}: {exc}'
            self.ok = False
        return self

    def predict(self, pod, ramp, dest, port_eta, sibling_dwells=None):
        if not self.ok:
            return None
        n, med, spread = _sib_stats(sibling_dwells)
        row = {
            'pod': pod or 'NA', 'ramp': ramp or 'NA', 'dest': dest or 'NA',
            'month': port_eta.month, 'dow': port_eta.weekday(),
            'sib_n': n, 'sib_dwell': med, 'sib_spread': spread,
        }
        if self.use_external:
            row.update(official_features(default_series(), pod, port_eta,
                                         self.external_lag))
        rec = pd.DataFrame([row])
        dummies = pd.get_dummies(rec[list(CATS)].astype(str), dummy_na=False)
        dummies = dummies.reindex(columns=self.cat_cols, fill_value=0)
        X = pd.concat([dummies, rec[self.num_cols].astype(float)], axis=1)

        out = {}
        for target in ('dwell', 'transit'):
            vals = [float(self.models[(target, q)].predict(X)[0]) for q in QS]
            vals = sorted(vals)
            out[target] = {'p10': vals[0], 'p50': vals[1], 'p90': vals[2]}
        return out
