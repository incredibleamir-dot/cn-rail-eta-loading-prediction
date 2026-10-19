# Data format

The app reads **one workbook** (`.xlsx` / `.xlsm`; `.csv` is also accepted) with
**one sheet** named `Shipments`. Each row is one container. Shipment details,
the vessel ETA, the discharge date, the carrier's rail ETA, and the actual rail
events all live on that single row.

No shipment data is included in this repository. Generate an empty template:

```bash
python scripts/make_template.py --out data/CN_Rail_Data.xlsx
```

For a runnable synthetic demo (screenshots use this):

```bash
python scripts/make_template.py --demo --out data/CN_Rail_Data.xlsx
```

`data/*.xlsx` is gitignored.

## Sheet and columns

Preferred sheet name: `Shipments`. If that sheet does not exist, the first sheet
in the workbook is read.

| Column | Required | Example | Notes |
|---|---|---|---|
| `Container Number` | yes | `MSDU4797470` | One container per row, no line breaks or commas. |
| `Job#` | no | `JOB12345` | Free text. |
| `Client` | no | `Acme` | Free text. |
| `Consignee` | no | `Acme Distribution` | Free text. |
| `Shipping Line` | no | `Maersk` | Free text. |
| `Port of Discharge` | yes | `MONTREAL` | Vessel port; groups the dwell model. |
| `Origin Ramp` | yes | `MONTREAL VIAU TERM` | Rail load terminal. |
| `Final Destination` | yes | `BRAMPTON INTER TERM` | Rail destination terminal. |
| `Vessel ETA` | yes | `2026-09-23 08:00` | Prediction anchor (must be present). |
| `Discharge Date` | no | `2026-09-24 08:00` | Actual vessel discharge, when known. |
| `Rail ETA` | no | `2026-10-08 18:40` | Carrier's own rail ETA; optional blend input. |
| `RAMPED` | training | `2026-09-29 23:00` | Actual loaded-on-rail event. |
| `DEPARTED` | no | `2026-09-30 06:00` | Actual rail departure. |
| `AT DESTINATION` | training | `2026-10-09 20:00` | Actual rail arrival. |
| `DERAMPED` | training | `2026-10-10 05:28` | Actual empty/deramp event. |
| `OUT-GATE` | no | `2026-10-11 09:00` | Not currently modelled. |
| `DELIVERED` | no | `2026-10-12 14:00` | Not currently modelled. |
| `Notes` | no | `Priority` | Free text. |

"Training" means the column is not mandatory on every row, but at least one row
must contain a `RAMPED` timestamp, and the model learns arrival/deramp only from
rows where those timestamps exist. Rows without `RAMPED` are still indexed for
container lookup.

## Dates and time zones

Accepted forms:

- Excel date/datetime cells
- `YYYY-MM-DD`
- `YYYY-MM-DD HH:MM`
- `YYYY-MM-DDTHH:MM:SS`
- timezone-tagged `YYYY-MM-DD HH:MM ET` (also `PT`, `MT`, `CT`, `AT`)
- `DD/MM/YYYY` and `DD/MM/YYYY HH:MM`

Native Excel dates are preferred. Avoid text such as `TBA` for required fields.
An empty value is allowed for optional events.

Day-gaps are computed on local wall-clock time, at day resolution. A timestamp
with a timezone tag is parsed with that zone and then its date is used for
day-level gaps.

## Column aliases

The loader normalizes case, spaces and underscores and accepts common aliases,
including:

- `Container`, `Container No`, `EquipmentId` → `Container Number`
- `Job`, `Job Number` → `Job#`
- `Carrier` → `Shipping Line`
- `POD`, `Port` → `Port of Discharge`
- `Ramp`, `Ramp Station`, `Rail Origin` → `Origin Ramp`
- `Destination`, `Rail Destination` → `Final Destination`
- `Port ETA`, `ETA Date`, `ETA`, `Vessel Arrival` → `Vessel ETA`
- `Actual Discharge`, `Vessel Discharge`, `Discharged` → `Discharge Date`
- `Carrier Rail ETA`, `Tracker Rail ETA` → `Rail ETA`
- `Loaded on Rail`, `Rail Loaded` → `RAMPED`
- `Rail Departure` → `DEPARTED`
- `Arrival`, `Rail Arrival` → `AT DESTINATION`
- `Empty`, `Deramp` → `DERAMPED`
- `Outgate`, `Out Gate` → `OUT-GATE`
- `Delivery`, `Pad Placement`, `At Receiver` → `DELIVERED`

## Minimal example

```csv
Container Number,Port of Discharge,Origin Ramp,Final Destination,Vessel ETA,Discharge Date,Rail ETA,RAMPED,AT DESTINATION,DERAMPED
MSDU4797470,MONTREAL,MONTREAL VIAU TERM,BRAMPTON INTER TERM,2026-09-23 08:00,2026-09-24 08:00,2026-10-08 18:40,2026-09-29 23:00,2026-10-09 20:00,2026-10-10 05:28
```

## Validation

The app checks the file before copying it into `data/`:

- sheet can be read;
- required columns exist;
- at least one row has a vessel ETA;
- at least one row has a `RAMPED` timestamp;
- container numbers are non-empty.

If a check fails, the dialog shows the reason and leaves the current data file
untouched.

## Custom ports and terminals

The built-in presets cover the main Canadian container ports and CN ramps. For
other terminals, extend `PORT_RAMPS` in `cn_model/data.py` and add canonical
destination rules to `canon_dest`. The model can still fall back to pooled
distributions for a port or lane with no history, but the ramp/destination must
be present in the form's options.
