# CN Rail ETA & Loading Prediction — Method Documentation

This document explains how the app turns one shipment workbook into predicted
rail timelines: what data is used, every calculation performed, the assumptions
behind them, and how the predictions are validated.

The repo ships **no shipment data**. The user supplies a single-sheet workbook;
an empty template can be generated with `scripts/make_template.py`.

---

## 1. What the app predicts

Given a **port of discharge**, **origin ramp**, **destination**, and the vessel's
**Port ETA** (or a confirmed vessel **discharge date**), the app predicts three
events as **date ranges** (p10–p90) with a **most-likely date** (p50):

| Event | Meaning | Input event column |
|---|---|---|
| RAMPED | Container loaded on rail at origin ramp | `RAMPED` |
| AT DESTINATION | Rail arrival at destination terminal | `AT DESTINATION` |
| DERAMPED | Container unloaded / available | `DERAMPED` |

The UI shows each prediction as a bar: the bar spans p10 to p90, and a pointer
marks p50. The "80% range" wording means: historically, 80% of journeys fell
inside a range of this width around the estimate (coverage is verified in the
backtest, section 7).

---

## 2. Data input

### 2.1 One workbook, one sheet

The app reads one workbook (`.xlsx`/`.xlsm`; `.csv` is also accepted). The
preferred worksheet name is **`Shipments`**; if it is absent, the first sheet is
used. Each row is one container, with shipment details, the vessel ETA, the
discharge date, the carrier's rail ETA, and any actual rail events:

| Column | Required | Meaning |
|---|---|---|
| `Container Number` | yes | One container per row; used for lookup. |
| `Job#`, `Client`, `Consignee`, `Shipping Line` | no | Shipment metadata. |
| `Port of Discharge` | yes | Vessel discharge port (the model's port group). |
| `Origin Ramp` | yes | CN rail ramp where the container is loaded. |
| `Final Destination` | yes | Rail destination terminal (the transit lane). |
| `Vessel ETA` | yes | Vessel Port ETA — the prediction anchor. |
| `Discharge Date` | no | Actual vessel discharge date, when known. |
| `Rail ETA` | no | Carrier's own continuously-updated rail ETA. |
| `RAMPED` | training | Actual loaded-on-rail timestamp. |
| `DEPARTED` | no | Actual rail departure. |
| `AT DESTINATION` | training | Actual rail arrival timestamp. |
| `DERAMPED` | training | Actual empty/deramp timestamp. |
| `OUT-GATE`, `DELIVERED` | no | Later events; not currently modelled. |
| `Notes` | no | Free text. |

Date cells can be native Excel dates, ISO strings (`2026-10-19 08:00`), or
timezone-tagged strings (`2026-10-19 08:00 ET`). The loader accepts a small set
of common column aliases; see `docs/DATA_FORMAT.md`.

Rows without `RAMPED` are still available for container lookup and live
prediction, but they do not contribute a training journey. At least one row with
`RAMPED` is needed to fit a model.

### 2.2 Official Canadian port-performance data

`external/port_monthly_dwell.csv` contains the official Transport Canada monthly
container-port series (155 port-months, five ports, ~17.3 M TEU). It supplies the
official import rail dwell profile used for the missing-month seasonal prior and
the seasonality-chart overlay; details and leakage rules are in section 12.

---

## 3. The master dataset (per-container journey)

From each row, the actual `RAMPED` timestamp starts a journey; the arrival,
deramp, departure, out-gate and delivery timestamps are attached. Derived columns
(all in **days**, signed):

```
dwell_days   = RAMPED            − Vessel ETA       (port dwell)
transit_days = AT DESTINATION    − RAMPED           (rail transit)
deramp_days  = DERAMPED          − RAMPED
outgate_days = OUT-GATE          − RAMPED
total_days   = AT DESTINATION    − Vessel ETA
```

Two deliberate simplifications, stated openly:

1. **Timezone handling.** Timestamps carrying PT/MT/CT/ET/AT tags are parsed as
   timezone-aware datetimes; when computing day-gaps the timezone info is
   stripped and local wall-clock times are subtracted. At day resolution this can
   shift a gap by a few hours (never more than one day).
2. **`RAMPED` is trusted as loaded-on-rail.** The model assumes the supplied
   event feed is accurate.

The master table is cached to `cache/master.csv`. The cache is invalidated when
the input workbook path, size or modification time changes. The "Update model
data" dialog replaces the data and rebuilds the cache (section 9).

---

## 4. Model 1/3 — Port dwell (Vessel ETA → RAMPED)

**Type:** non-parametric empirical quantiles with hierarchical seasonal
adjustment. No regression, no distributional assumption.

For each port of discharge, collect the historical `dwell_days` and take the
10th, 50th and 90th percentiles (linear interpolation between order statistics).
These are the **base quantiles**.

**Month factor (season).** Within a port, for each calendar month of the anchor
date: `raw = median(month values) / median(all port values)`. The raw factor is
clamped to **[0.6, 1.8]** and shrunk toward 1.0 by sample size:

```
factor_month = (n_month × raw_clamped + k × 1.0) / (n_month + k),   k = 6
```

**Weekday factor.** Same formula on the anchor's weekday, clamped to **[0.7, 1.5]**,
`k = 4`.

**Prediction:**

```
dwell_pxx = base_pxx × factor_month × factor_weekday      (floored at 0)
RAMPED_pxx = anchor_date + dwell_pxx days
```

**Unknown port fallback:** if the chosen port has no history at all, the model
falls back to quantiles over the pooled per-port percentiles (a coarse estimate;
the UI notes this).

**When a confirmed discharge date is given**, it replaces the anchor. Because the
dwell quantiles were learned against *Vessel ETA*, the model subtracts the
elapsed ETA → discharge gap from every dwell quantile (floored at half a day) and
estimates only the **remaining** dwell. This matters mainly when the vessel was
late.

### 4.1 Same-vessel adjustment (when siblings have already ramped)

Containers sharing the same port of discharge and the same vessel ETA date are
treated as one vessel call. If any sibling containers already have a `RAMPED`
event, the whole dwell distribution is shifted so that its p50 equals the
**sibling median ramp date**; the p10–p90 width and shape are unchanged. The
median is symmetric (even n averages the two middle dates) and all sibling dates
are kept (de-duplicating distorted small groups). The app reports the adjustment
as `Same-vessel history: N container(s) …` and adds `+ N same-vessel` to the
source label.

This is the strongest single feature in the model. On the same leave-one-out
rows, using only siblings that had ramped **before** the target scores a lower
RAMPED MAE than using all recorded siblings (about 2.3 d vs 2.45 d); the app uses
all recorded siblings, because queries are normally made once the vessel has
started ramping. When no sibling has ramped yet, the adjustment is skipped.

---

## 5. Model 2/3 — Rail transit (RAMPED → AT DESTINATION)

**Type:** hierarchical empirical quantiles per "lane" (origin ramp → destination).

Cells, from most specific to most general:

| Level | Key | Minimum n to be used |
|---|---|---|
| Lane | ramp \| destination | ≥ 3 (below 3: shrunk, not trusted alone) |
| Port→destination | port \| destination | ≥ 5 |
| Destination | destination | ≥ 10 |
| Global | all lanes pooled | always |

The specific cell is blended with its parent **element-wise on p10/p50/p90** by
sample size:

```
blended = w × cell + (1 − w) × parent,     w = n / (n + k),   k = 6
```

so a lane with 6 trips is a 50/50 mix of itself and its parent, and a lane with
60 trips is ~91% itself. A **month factor** per lane (month of the predicted
RAMPED date; same clamped, shrunk median-ratio formula, k = 5) is applied
multiplicatively; transit quantiles are floored at 0.2 days.

After shrinkage and blending, quantiles are re-ordered (`p10 ≤ p50 ≤ p90`) — the
linear blending of three quantiles can otherwise cross them.

**Predicted arrival dates** come from adding the quantiles of the two stages:

```
AT_DEST_pxx = RAMPED_pxx + transit_pxx
```

This treats dwell uncertainty and transit uncertainty as independent. In
practice the sum is slightly conservative (the two errors can partially offset
in reality), which is preferable to under-promising range width.

**Not modelled today** (transparently listed as future work): departure lag
(store and first `DEPARTED`), out-gate and delivery events are computed in the
dataset but not yet part of predictions.

---

## 6. Model 3/3 — Deramp, and the carrier-ETA blend

### Deramp
`DERAMPED − AT DESTINATION` quantiles by destination (fallback: global). These are
small (median typically well under a day — the UI often shows a single date).
Deramp dates are computed from the **arrival p50**, so the deramp range reflects
deramp uncertainty only, not arrival uncertainty.

### Carrier rail ETA blend (only when the user supplies it)
The optional `Rail ETA` column is the carrier's own updated estimate. The rule
(validated in section 7):

```
gap = carrier_rail_eta − model_arrival_p50

|gap| ≤ 3 days :  arrival_p50 = midpoint(model, carrier), rounded to the nearest
                  day with ties taken toward the carrier date
|gap| > 3 days :  arrival_p50 = carrier date
                  range widened to cover the model estimate too
```

Odd gaps produce a half-day midpoint; the tie is rounded away from the model
(toward the carrier) because the carrier is the more accurate source.

Why: over all backtested shipments the carrier ETA averaged **1.41 d** absolute
error (vs 2.87 d for the model alone). When the two disagreed by more than
3 days, the carrier was right far more often (2.63 d vs 6.11 d mean absolute
error). The app therefore treats the carrier ETA as the primary date wherever it
exists, and uses the model for everything the carrier ETA does not cover
(rows without a rail ETA, ranges, RAMPED/deramp estimates).

### ML assist (optional, gradient boosting)

A second, independent estimate is produced by **histogram gradient-boosted
quantile regressors** (scikit-learn): three models per target (p10/p50/p90) for

- **dwell** (Vessel ETA → RAMPED), and
- **transit** (RAMPED → arrival),

trained on the same master journeys. Features are the port, origin ramp,
destination, anchor month and weekday (one-hot), plus same-vessel sibling
features (number of siblings, their median dwell and spread). Sibling features
are computed leave-one-out inside the training data, so a row never sees its own
result. Settings: 300 iterations, max depth 3, learning rate 0.05,
`min_samples_leaf=10`, L2 = 1.0.

**Blend rule.** The model output is combined with the statistical engine rather
than replacing it:

```
ramp_p50    = engine_ramp_p50    + 0.5 × (ML_dwell_p50 − engine_ramp_p50)
arrival_p50 = average(engine_arrival_p50, ML_dwell_p50 + ML_transit_p50)
```

The engine's calibrated p10–p90 **width is preserved** and moved with the new
centre; the carrier-ETA rule above then runs on the blended arrival. This keeps
the honesty of the empirical ranges while letting the ML model correct the
centre (it generalises better to future/seasonally different months).

**Fallback.** If scikit-learn is not installed, or fitting fails, the app runs
the statistical engine alone and says so in the status line. The ML model is
refitted in the background whenever the workbook data is rebuilt. The ML code is
in `cn_model/ml.py`.

---

## 7. Validation — how accurate is this?

The figures below are a **reference run on the development dataset** (304
journeys with a Vessel ETA, 282 with a rail arrival). Your own results will vary
with your lanes, sample sizes and data quality.

### 7.1 Leave-one-out backtest

Each journey is predicted using a model fitted on all *other* journeys, anchored
on its real Vessel ETA.

| Prediction | n | MAE | Bias | Within 1 d | Within 2 d | In p10–p90 |
|---|---|---|---|---|---|---|
| RAMPED date | 304 | 2.45 d | −0.31 d | 55% | 66% | 88% |
| Arrival (AT DEST) | 282 | 2.87 d | −0.93 d | 46% | 63% | 92% |
| Deramp | 273 | 2.92 d | −1.32 d | 39% | 61% | — |

Coverage of the 80% range being 88–92% means the ranges are honestly sized
(if anything, slightly conservative).

### 7.2 Time-split test (more realistic for future shipments)

Train on journeys whose Vessel ETA is before 2026-04-01 (138), predict the 166
after. This removes the advantage of a model that has seen "itself" in training
and tests the ML assist where it matters — future voyages:

| Method | RAMPED MAE | Arrival MAE |
|---|---|---|
| Statistical engine (with same-vessel history) | 2.92 d | 3.36 d |
| Engine + ML assist (shipped) | **2.55 d** | **2.95 d** |
| ML alone | 2.46 d | 2.66 d |

The ML model alone is noticeably more accurate on this future split, but its
quantile coverage in cross-validation is narrower (76–83% instead of ~90%), so
the app keeps the engine's calibrated range and uses ML only to correct the
centre. On the same 5-fold cross-validation the engine scores 2.48 d / 2.88 d and
the blend 2.62 d / 2.84 d; cross-validation flatters the statistical engine
because it can memorise the same-vessel siblings of the target, while the time
split is the better guide for a genuinely new shipment.

### 7.3 Benchmarks and the blend

| Method | n | MAE | Bias |
|---|---|---|---|
| Model alone (engine, LOO) | 282 | 2.87 d | −0.93 d |
| Carrier rail ETA (as-is) | 281 | **1.41 d** | +1.17 d |
| Blend rule of section 6 | 281 | 1.57 d | +1.07 d |

The carrier tends to be ~1 day *late* on average (positive bias) — useful to know.

### 7.4 Accuracy by lane (arrival MAE, leave-one-out)

| Lane | n | RAMPED MAE | Arrival MAE |
|---|---|---|---|
| ROBERTS BANK → BRAMPTON INTER TERM | 86 | 1.26 d | 1.31 d |
| MONTREAL VIAU → EDMONTON INTER TERM | 56 | 0.95 d | 1.88 d |
| ROBERTS BANK → CALGARY LOG PARK | 26 | 3.46 d | 3.50 d |
| MONTREAL TERMONT → BRAMPTON INTER TERM | 20 | 2.55 d | 2.65 d |
| MONTREAL VIAU → BRAMPTON INTER TERM | 20 | 4.80 d | 5.05 d |
| MONTREAL VIAU → CALGARY LOG PARK | 10 | 3.70 d | 2.80 d |

### 7.5 Accuracy by port (RAMPED date)

| Port | n | MAE | Bias |
|---|---|---|---|
| VANCOUVER | 146 | 1.82 d | −0.12 d |
| MONTREAL | 131 | 2.69 d | −0.60 d |
| PRINCE RUPERT | 12 | 4.33 d | +2.17 d |
| HALIFAX | 10 | 5.20 d | −3.00 d |
| NEW YORK | 4 | 5.50 d | +1.00 d |

---

## 8. Assumptions and limitations (consolidated)

1. **Vessel ETA is a usable proxy for discharge.** When a confirmed discharge
   date is entered, the ETA → discharge elapsed time is subtracted from the dwell
   quantiles (section 4), so the model estimates only the remaining dwell.
2. **The future resembles the past.** No vessel schedule, congestion index,
   weather, holiday or crew features are used at prediction time. Seasonal
   effects are captured through monthly median ratios and, for the ML assist,
   through month/weekday features. Ports and lanes with no history fall back to
   pooled distributions; a port-month with **no** history borrows the leak-free
   official Transport Canada seasonal factor (section 12.1).
3. **Small samples borrow strength.** Cells with few trips are blends with their
   parent distribution (k = 6). The UI always shows `n` so the user can judge.
4. **Ranges combine stages additively** (dwell pxx + transit pxx), assuming
   independence between the two stages; this is mildly conservative.
5. **Vessel-batch effects are used when observable.** Containers on the same
   vessel ramp together much more tightly than the port-wide spread (e.g. a
   within-vessel standard deviation of ~1.7 d vs ~3.5 d overall in the reference
   data). Once at least one sibling container has a recorded `RAMPED` event, the
   whole ramp distribution is shifted to the sibling median (section 4.1). For a
   brand-new vessel with no ramped siblings, the port-wide distribution (wide
   range) remains the honest answer.
6. **ML assist is deliberately subordinate.** The gradient-boosted model only
   shifts the centre; the range width still comes from the calibrated empirical
   engine. If scikit-learn is unavailable, the app silently falls back to the
   engine. With ~300 journeys, the ML model can overfit rare lanes, which is why
   it is blended 50/50 rather than used directly.
7. **Deramp ranges reflect deramp only** (computed from arrival p50), and rare
   long dwells after arrival (demurrage cases) will be missed.
8. **Day-level resolution.** Times are handled at day granularity with local
   wall-clock subtraction (section 3), so sub-day precision is deliberately absent.
9. **Workbook structure is assumed stable.** The loader validates the required
   columns, but aliases and terminal names are fixed in code; extend `PORT_RAMPS`
   / `canon_dest` for new terminals.
10. **No editing of source data.** The app never modifies the uploaded workbook;
    it copies it into the project and keeps timestamped backups in `backups/`.

---

## 9. Updating the model with fresh data

1. Click **"Update model data…"** in the app header.
2. Browse to the latest single-sheet workbook. The app validates the required
   columns and shows a row/event summary. Leave the current file untouched to
   rebuild from it.
3. Press **Rebuild model**. The app backs up the current workbook to `backups/`,
   stores the new one, then in a background thread:
   - rebuilds `cache/master.csv`,
   - refits all quantiles/factors,
   - retrains the ML assist model (if scikit-learn is installed),
   - scores a holdout split for the Model lab diagnostics,
   - re-indexes containers for lookup,
   reporting each stage in the dialog with a progress bar.

The model always learns from **all** completed journeys in the uploaded file, so
accuracy tends to improve as the file accumulates history.

---

## 10. Reading the prediction output

- **Range bar:** bar = p10 to p90 of the predicted date (80% historical
  coverage); the pointer and the bold date = p50 (most likely). The "from/to"
  labels under the bar are the range ends.
- **`n=`** — how many historical journeys fed that stage (port dwell card: port
  sample size; arrival card: lane sample size). Small n → wider, less certain
  range, by construction.
- **`≈ +X days from anchor`** — the predicted elapsed days from the anchor; p10/p90
  day offsets are given for context. These offsets are measured from the Vessel
  ETA (or confirmed discharge) even on the arrival card.
- **Prediction notes** list every adjustment applied (seasonal factors, anchor
  choice, same-vessel alignment, ML assist, carrier-ETA blending, fallbacks).
  Source labels gain `+ N same-vessel` and `+ ML` when those adjustments run.
- **Distributions tab** — the historical dwell/transit distributions with the
  predicted p10/p50/p90 drawn on top; shows exactly where the prediction sits in
  history.
- **Seasonality tab** — monthly medians with the predicted month highlighted and
  the predicted day-value as a dashed line; the dwell chart also overlays the
  official Transport Canada monthly dwell profile (dashed, hollow markers),
  scaled to the local level, so months with little or no history are still
  visible.
- **Model lab tab** — holdout diagnostics: MAE bars for RAMPED and arrival
  (engine vs ML assist vs carrier ETA) and arrival error lines by anchor month,
  with 80%-band coverage in the header. The app uses the last 35% of journeys by
  Vessel ETA as its holdout, so it is a rolling diagnostic rather than the fixed
  April 2026 split in section 7.2.

---

## 11. Reproducing the numbers

```
# install runtime dependencies
pip install -r requirements.txt

# create an empty workbook template (or add --demo for synthetic rows)
python scripts/make_template.py --out data/CN_Rail_Data.xlsx

# rebuild the master table from the workbook
python -m cn_model.data

# leave-one-out backtest, 5-fold engine-vs-ML comparison, lane/port breakdowns
python -m cn_model.backtest

# run the app
python app.py
```

Code map: `cn_model/data.py` (single-sheet loading + master table),
`cn_model/engine.py` (quantile model, same-vessel adjustment, ML/carrier blends,
official missing-month prior; tunables in `TUNING`), `cn_model/ml.py`
(gradient-boosted quantile assist, optional official features),
`cn_model/backtest.py` (validation + UI diagnostics), `cn_model/external.py`
(official Transport Canada loader and seasonal profiles), `app.py` (UI, update
dialog, charts).

---

## 12. Official data (partially adopted)

The official [Transport Canada Marine and Port Dashboard](https://tdih-cdit.tc.canada.ca/en/dashboard/marine-and-port-dashboard)
publishes a downloadable "Monthly port performance – containers" file covering
January 2024 to July 2026 for Canada's five container ports (155 port-months,
about 17.3 M TEU). Its columns include container volumes (TEU), vessel calls,
average vessel time at berth, and **average import rail container dwell time
(days)** — the same quantity the stage-1 model predicts, but aggregated per
port-month. The file is stored in `external/port_monthly_dwell.csv` and parsed
by `cn_model/external.py` for reproducibility. All official features are
leak-free: only months from years *before* the predicted year are used
(`seasonal_factor`).

### 12.1 Adopted uses

1. **Missing-month seasonal prior (engine).** When the shipment data has **no**
   journey for that port in that calendar month (`n_month == 0`), the official
   same-calendar-month seasonal factor replaces the month factor entirely and
   the notes explain it: *"No <Month> history for <Port>; official prior
   factor ×…"*. Controlled by `TUNING['use_external_missing']` (default `True`).
   When shipment month history exists, official data is **not** blended in.
   Sensitivity is tiny: LOO MAE moves from 2.444→2.454 d (RAMPED) and
   2.858→2.869 d (arrival), so the prior is retained because it only affects
   otherwise-unseen port-months and replaces a blind factor of 1.0.
2. **Seasonality chart overlay (UI).** `monthly_profile()` returns the official
   median import rail dwell per month; the app scales it to the local median
   dwell for the selected port and draws it as a dashed "official ×k" line with
   hollow markers, so months with sparse history still show a full 12-month
   shape. This is display-only.
3. **Data strip.** The header shows the official coverage (month rows, port
   count, M TEU) alongside journey/container counts to keep the data provenance
   visible.

### 12.2 Evaluated and rejected for point predictions

Several ways of using the official values as model inputs were implemented and
backtested, all with leak-free rules. The blend figures in items 1–2 below are
from the evaluation run performed **before** the same-vessel and ML adjustments
were added, so they are not directly comparable with section 7; the relative
comparison is the point:

1. **Seasonal prior on dwell** — official same-calendar-month dwell / port
   median over prior years, blended with the shipment-derived month factor by
   sample size (weights from `n/(n+k)`):

   | Variant | RAMPED MAE | Arrival MAE |
   |---|---|---|
   | No external data (engine at test time) | **3.45 d** | **3.61 d** |
   | External seasonal, k = 4 | 3.60 d | 3.59 d |
   | External seasonal, k = 8 | 3.71 d | 3.68 d |
   | External seasonal, k = 16 | 3.81 d | 3.73 d |
   | External seasonal, k = 32 | 3.91 d | 3.81 d |

2. **Current congestion level** — official dwell over the last 1/3/6 months
   relative to a trailing 6/12-month median, applied as a multiplier to dwell.
   All variants made accuracy worse (RAMPED MAE 3.67–3.95 d, arrival 3.73–3.96 d).

3. **Official features in the ML assist** — 11 leak-free columns (levels,
   trailing means, ratios, seasonal values) added to the feature matrix. On the
   current time-split test the base ML scores 2.46 d / 2.66 d; the external
   variants scored 2.57–3.02 d / 2.78–3.07 d depending on column subset and lag,
   and a sparse-cell gate still landed at 2.55 d / 2.78 d. The feature is kept
   behind `MLPredictor(use_external=True)` (default `False`) for future
   re-evaluation as the official series grows.

Why the levels do not transfer:

- **Different anchor definition.** The official metric measures from actual
  vessel discharge; the dwell model measures from the *Vessel ETA*, so it also
  contains vessel waiting time. Levels differ systematically, leaving only the
  seasonal *shape* usable.
- **Only two prior years per calendar month** (one for 2025 predictions), so the
  official "seasonal factor" is a single noisy observation, often clamped.
- **Weak signal.** Across the 27 overlapping port-months the correlation between
  the shipment-level median dwell and the official monthly dwell is only
  ρ ≈ 0.33, and several of those months have tiny samples.
- **Container-level data is finer.** The model already learns month factors from
  every journey, including many months the official series averages away.

A more promising external source would be *daily* vessel line-ups with
anchorage/berth delays (e.g. the Port of Vancouver container vessel line-up),
because that addresses the Vessel-ETA-to-discharge lag directly rather than
monthly average dwell.
