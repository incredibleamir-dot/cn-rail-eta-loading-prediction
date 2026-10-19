"""CN Rail ETA & Loading Prediction - desktop app (tkinter + ttkbootstrap).

Pick a port of discharge, origin ramp, destination and the vessel Port ETA
(or confirmed discharge date); the model predicts the RAMPED (loaded on rail)
and AT DESTINATION / DERAMPED dates as a RANGE (p10-p90) with a pointer
marking the most likely date (p50), learned from the historical journeys in the workbook.

The "Update data" dialog accepts the latest single-sheet workbook
(downloaded unchanged) and rebuilds the model in the background.

Run:  python app.py
"""
from __future__ import annotations

import os
import queue
import shutil
import statistics
import threading
from datetime import datetime, time as dtime, timedelta

import tkinter as tk
from tkinter import filedialog, messagebox, font as tkfont
import ttkbootstrap as ttk

from cn_model.data import (
    BASE_DIR, DATA_CSV, DATA_XLSX, PORT_RAMPS, all_known_containers,
    build_master, canon_dest, canon_pod, data_path, validate_data_file,
)
from cn_model.engine import ETAPredictor
from cn_model.ml import MLPredictor
from cn_model.external import default_series, monthly_profile
from cn_model.backtest import build_diagnostics

APP_TITLE = 'CN Rail ETA & Loading Prediction'
FONT = 'Segoe UI'
MONO = 'Consolas'
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')


def diag_summary(diag):
    if not diag:
        return 'Diagnostics: see the Model lab.'
    ramp = diag.get('ramp_engine')
    arr = diag.get('arr_engine')
    parts = []
    if ramp is not None:
        parts.append(f'RAMPED {ramp:.2f}d')
    if arr is not None:
        parts.append(f'arrival {arr:.2f}d')
    return ('Holdout MAE: ' + ' · '.join(parts) + '.') if parts else 'Diagnostics: see the Model lab.'


def fmt_long(d) -> str:
    if d is None:
        return '—'
    if isinstance(d, datetime):
        d = d.date()
    return d.strftime('%a, %d %b %Y')


def fmt_short(d, with_year=False) -> str:
    if d is None:
        return '—'
    if isinstance(d, datetime):
        d = d.date()
    return d.strftime('%d %b %Y' if with_year else '%d %b')


def fmt_range(d10, d90) -> str:
    if d10 is None or d90 is None:
        return '—'
    if d10 == d90:
        return fmt_short(d10, True)
    if d10.year != d90.year:
        return f'{fmt_short(d10, True)}  –  {fmt_short(d90, True)}'
    return f'{fmt_short(d10)}  –  {fmt_short(d90, True)}'


def _mix(hex_a: str, hex_b: str, t: float) -> str:
    a = tuple(int(hex_a[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(hex_b[i:i + 2], 16) for i in (1, 3, 5))
    m = tuple(round(a[i] * (1 - t) + b[i] * t) for i in range(3))
    return '#%02x%02x%02x' % m


_FONT_CACHE = {}


def _measure(text, font_spec):
    f = _FONT_CACHE.get(font_spec)
    if f is None:
        f = tkfont.Font(font=font_spec)
        _FONT_CACHE[font_spec] = f
    return f.measure(text)


def _ellipsize(text, max_px, font_spec):
    if _measure(text, font_spec) <= max_px:
        return text
    while text and _measure(text + '…', font_spec) > max_px:
        text = text[:-1]
    return (text + '…') if text else ''


# ------------------------------------------------------------------ range bar
class RangeBar(tk.Canvas):
    """Date range bar: band spans p10..p90, pointer marks the likely (p50) date."""

    def __init__(self, master, height=92, **kw):
        super().__init__(master, height=height, highlightthickness=0, bd=0, **kw)
        self._q = None
        self._palette = dict(
            bg='#ffffff', border='#d6d6d6', primary='#0a58ca',
            fg='#212529', muted='#686d71',
        )
        self.bind('<Configure>', lambda e: self._draw())

    def set_palette(self, palette: dict):
        self._palette = palette
        self.configure(bg=palette['bg'])
        self._draw()

    def set_range(self, d10, d50, d90):
        self._q = (d10, d50, d90)
        self._draw()

    def clear(self):
        self._q = None
        self._draw()

    def _draw(self):
        self.delete('all')
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 60:
            return
        p = self._palette
        if not self._q:
            self.create_text(w / 2, h / 2, text='No prediction yet',
                             fill=p['muted'], font=(FONT, 10), anchor='center')
            return

        d10, d50, d90 = self._q
        span = max((d90 - d10).days, 1)
        pad = max(2.0, span * 0.10)
        d0 = d10 - timedelta(days=pad)
        d1 = d90 + timedelta(days=pad)
        total = (d1 - d0).days or 1

        x_left, x_right = 14.0, w - 14.0

        def X(d):
            return x_left + (d - d0).days / total * (x_right - x_left)

        x10, x50, x90 = X(d10), X(d50), X(d90)
        ybar, hbar = h * 0.52, 22

        self.create_line(x_left, ybar, x_right, ybar, fill=p['border'], width=2)

        band_fill = _mix(p['primary'], p['bg'], 0.62)
        self.create_rectangle(x10, ybar - hbar / 2, x90, ybar + hbar / 2,
                              fill=band_fill, outline=p['primary'], width=2)

        core = max(4.0, (x90 - x10) * 0.10)
        self.create_rectangle(max(x10, x50 - core), ybar - hbar / 2,
                              min(x90, x50 + core), ybar + hbar / 2,
                              fill=p['primary'], outline='')

        self.create_line(x50, ybar - hbar / 2, x50, ybar - hbar / 2 - 12,
                         fill=p['primary'], width=3)
        self.create_polygon(x50 - 7, ybar - hbar / 2 - 12,
                            x50 + 7, ybar - hbar / 2 - 12,
                            x50, ybar - hbar / 2 - 3,
                            fill=p['primary'], outline='')
        self.create_oval(x50 - 6, ybar - 6, x50 + 6, ybar + 6,
                         fill=p['primary'], outline=p['bg'], width=2)

        def clamp(x, lo=42, hi=None):
            hi = (w - 42) if hi is None else hi
            return min(max(x, lo), hi)

        self.create_text(clamp(x50), ybar - hbar / 2 - 16, text=fmt_long(d50),
                         fill=p['fg'], font=(FONT, 10, 'bold'), anchor='s')

        y_lbl = ybar + hbar / 2 + 10
        if d10 == d90:
            self.create_text((x10 + x90) / 2, y_lbl, text=f'{fmt_short(d10)} (same day)',
                             fill=p['muted'], font=(FONT, 9), anchor='n')
        elif x90 - x10 < 110:
            self.create_text((x10 + x90) / 2, y_lbl,
                             text=f'from {fmt_short(d10)}  to  {fmt_short(d90)}',
                             fill=p['muted'], font=(FONT, 9), anchor='n')
        else:
            self.create_text(clamp(x10), y_lbl, text='from ' + fmt_short(d10),
                             fill=p['muted'], font=(FONT, 9), anchor='n')
            self.create_text(clamp(x90), y_lbl, text='to ' + fmt_short(d90),
                             fill=p['muted'], font=(FONT, 9), anchor='n')


# ------------------------------------------------------------------ charts
class Chart(tk.Canvas):
    """Histogram ('hist') or monthly median trend ('trend') with prediction overlays."""

    def __init__(self, master, height=250, **kw):
        super().__init__(master, height=height, highlightthickness=0, bd=0, **kw)
        self.mode = None
        self._data = None
        self._palette = dict(bg='#ffffff', border='#d6d6d6', fg='#212529',
                             muted='#686d71', accent='#0a58ca')
        self.bind('<Configure>', lambda e: self._draw())

    def set_palette(self, palette: dict):
        self._palette = palette
        self.configure(bg=palette['bg'])
        self._draw()

    def placeholder(self, text='Waiting for data…'):
        self.mode = 'placeholder'
        self._data = (text,)
        self._draw()

    def show_hist(self, values, lines, title):
        self.mode = 'hist'
        self._data = (list(values or []), lines or None, title)
        self._draw()

    def show_trend(self, medians, ns, highlight, pred, title, unit='d',
                   external=None, external_label=None):
        self.mode = 'trend'
        self._data = (medians, ns, highlight, pred, title, unit, external, external_label)
        self._draw()

    def show_bars(self, items, title, note=''):
        self.mode = 'bars'
        self._data = (items, title, note)
        self._draw()

    def show_error_lines(self, series, title, note=''):
        self.mode = 'errors'
        self._data = (series, title, note)
        self._draw()

    # ---------------------------------------------------------------- draw
    def _draw(self):
        self.delete('all')
        w, h = self.winfo_width(), self.winfo_height()
        if w < 80 or h < 60:
            return
        p = self._palette
        if self.mode == 'placeholder':
            self.create_text(w / 2, h / 2, text=self._data[0], fill=p['muted'],
                             font=(FONT, 10), anchor='center')
            return
        if self.mode == 'hist':
            self._draw_hist(w, h, p)
        elif self.mode == 'trend':
            self._draw_trend(w, h, p)
        elif self.mode == 'bars':
            self._draw_bars(w, h, p)
        elif self.mode == 'errors':
            self._draw_error_lines(w, h, p)

    def _draw_hist(self, w, h, p):
        values, lines, title = self._data
        l, r, top, bot = 10, 12, 46, 22
        yt, yb = top, h - bot
        title = _ellipsize(title, w - l - r - 92, (FONT, 9, 'bold'))
        self.create_text(l, 8, text=title, fill=p['fg'], font=(FONT, 9, 'bold'), anchor='nw')
        self.create_text(w - r, 8, text=f'n={len(values)} trips', fill=p['muted'],
                         font=(FONT, 8), anchor='ne')

        base = max(values) if values else 0
        if lines:
            base = max(base, lines.get('p90') or 0)
        if base <= 0:
            self.create_text(w / 2, h / 2, text='No history and no prediction for this selection',
                             fill=p['muted'], font=(FONT, 9), anchor='center')
            return
        step = max(1, int(round(base / 14)) or 1)
        nbins = int(base // step) + 1
        xmax = (nbins + 0.4) * step

        counts = [0] * nbins
        for v in values:
            i = min(int(v // step), nbins - 1)
            counts[i] += 1
        hmax = max(counts) or 1

        def X(v):
            return l + (v / xmax) * (w - l - r)

        def Y(c):
            return yb - (c / hmax) * (yb - yt)

        self.create_line(l, yb, w - r, yb, fill=p['border'])
        if lines:
            xa, xb = X(lines.get('p10') or 0), X(lines.get('p90') or 0)
            self.create_rectangle(xa, yt, max(xb, xa + 2), yb,
                                  fill=_mix(p['accent'], p['bg'], 0.88), outline='')

        bar_fill = _mix(p['accent'], p['bg'], 0.55)
        for i, c in enumerate(counts):
            if not c:
                continue
            x0, x1 = X(i * step) + 1, X((i + 1) * step) - 1
            self.create_rectangle(x0, Y(c), max(x1, x0 + 2), yb, fill=bar_fill, outline='')

        for i in range(0, nbins + 1, max(1, nbins // 6)):
            self.create_text(X(i * step), yb + 4, text=str(i * step), fill=p['muted'],
                             font=(FONT, 8), anchor='n')

        if lines:
            for key, dash, width in (('p10', (4, 3), 1), ('p90', (4, 3), 1), ('p50', None, 2)):
                v = lines.get(key)
                if v is None:
                    continue
                self.create_line(X(v), yt, X(v), yb, fill=p['accent'] if key == 'p50' else p['muted'],
                                 width=width, dash=dash)
            pl = lines.get('p50')
            if pl is not None:
                self.create_text(min(max(X(pl), 46), w - 60), yt - 2,
                                 text=f'likely {pl:.1f}d', fill=p['accent'],
                                 font=(FONT, 9, 'bold'), anchor='s')

    def _draw_trend(self, w, h, p):
        (medians, ns, highlight, pred, title, unit,
         external, external_label) = self._data
        l, r, top, bot = 26, 16, 54, 32
        yt, yb = top, h - bot
        title = _ellipsize(title, w - l - 12 - (132 if external else 0), (FONT, 9, 'bold'))
        self.create_text(l - 16, 8, text=title, fill=p['fg'], font=(FONT, 9, 'bold'), anchor='nw')
        if external:
            lx = w - r - 124
            self.create_line(lx, 30, lx + 14, 30, fill=p['accent'], width=2)
            self.create_text(lx + 19, 30, text='history', fill=p['muted'],
                             font=(FONT, 8), anchor='w')
            self.create_line(lx + 62, 30, lx + 76, 30, fill=p['muted'], width=1, dash=(4, 3))
            self.create_text(lx + 81, 30, text=(external_label or 'official')[:14],
                             fill=p['muted'], font=(FONT, 8), anchor='w')

        ys = [v for v in medians.values() if v is not None]
        if external:
            ys.extend(v for v in external.values() if v is not None)
        if pred is not None:
            ys.append(pred)
        if not ys:
            self.create_text(w / 2, h / 2, text='No history for this selection',
                             fill=p['muted'], font=(FONT, 9), anchor='center')
            return
        ymax = max(ys) * 1.18 or 1

        def X(m):
            return l + (m - 1) / 11 * (w - l - r)

        def Y(v):
            return yb - (v / ymax) * (yb - yt)

        for frac in (0, 0.5, 1):
            yy = Y(ymax * frac)
            self.create_line(l, yy, w - r, yy, fill=p['border'], dash=(2, 4))
            self.create_text(l - 4, yy, text=f'{ymax * frac:.0f}', fill=p['muted'],
                             font=(FONT, 8), anchor='e')

        if highlight:
            slot = (w - l - r) / 11
            self.create_rectangle(X(highlight) - slot / 2, yt, X(highlight) + slot / 2, yb,
                                  fill=_mix(p['accent'], p['bg'], 0.88), outline='')

        if pred is not None:
            self.create_line(l, Y(pred), w - r, Y(pred), fill=p['accent'],
                             width=1, dash=(5, 3))
            self.create_text(w - r, max(Y(pred) - 4, yt - 2),
                             text=f'predicted {pred:.1f}{unit}',
                             fill=p['accent'], font=(FONT, 8, 'bold'), anchor='se')

        if external:
            ext = [(m, external[m]) for m in range(1, 13) if external.get(m) is not None]
            seg = []

            def flush_ext():
                if len(seg) >= 2:
                    coords = []
                    for mm, vv in seg:
                        coords.extend([X(mm), Y(vv)])
                    self.create_line(coords, fill=p['muted'], width=1, dash=(4, 3))
                seg.clear()

            prev_m = None
            for m, v in ext:
                if prev_m is not None and m != prev_m + 1:
                    flush_ext()
                seg.append((m, v))
                prev_m = m
            flush_ext()
            for m, v in ext:
                if medians.get(m) is None:
                    self.create_oval(X(m) - 3, Y(v) - 3, X(m) + 3, Y(v) + 3,
                                     outline=p['muted'], width=1, fill=p['bg'])

        pts = [(m, medians[m]) for m in range(1, 13) if medians.get(m) is not None]
        seg = []

        def flush():
            if len(seg) >= 2:
                coords = []
                for mm, vv in seg:
                    coords.extend([X(mm), Y(vv)])
                self.create_line(coords, fill=p['accent'], width=2)
            seg.clear()

        prev_m = None
        for m, v in pts:
            if prev_m is not None and m != prev_m + 1:
                flush()
            seg.append((m, v))
            prev_m = m
        flush()

        for m, v in pts:
            self.create_oval(X(m) - 3, Y(v) - 3, X(m) + 3, Y(v) + 3,
                             fill=p['accent'], outline=p['bg'])

        for m in range(1, 13):
            self.create_text(X(m), yb + 4, text='JFMAMJJASOND'[m - 1], fill=p['muted'],
                             font=(FONT, 8), anchor='n')
            n = ns.get(m) or 0
            if n:
                self.create_text(X(m), yb + 15, text=str(n), fill=p['muted'],
                                 font=(MONO, 7), anchor='n')

    def _draw_bars(self, w, h, p):
        items, title, note = self._data
        l, r, top, bot = 12, 14, 46, 14
        self.create_text(l, 8, text=_ellipsize(title, w - l - r - 120, (FONT, 9, 'bold')),
                         fill=p['fg'], font=(FONT, 9, 'bold'), anchor='nw')
        if note:
            self.create_text(w - r, 9, text=note, fill=p['muted'],
                             font=(MONO, 7), anchor='ne')
        items = [(lab, val, col) for lab, val, col in items if val is not None]
        if not items:
            self.create_text(w / 2, h / 2, text='No validation data yet',
                             fill=p['muted'], font=(FONT, 9), anchor='center')
            return
        maxv = max(v for _, v, _ in items) * 1.20 or 1
        label_w = min(158, (w - l - r) * 0.46)
        x0 = l + label_w
        x1 = w - r - 34
        row_h = (h - top - bot) / len(items)

        for frac in (0, 0.5, 1):
            xx = x0 + (x1 - x0) * frac
            self.create_line(xx, top - 4, xx, h - bot, fill=p['border'], dash=(2, 4))
            self.create_text(xx, h - bot + 2, text=f'{maxv * frac:.1f}', fill=p['muted'],
                             font=(MONO, 7), anchor='n')

        for i, (lab, val, col) in enumerate(items):
            cy = top + i * row_h + row_h / 2
            self.create_text(x0 - 8, cy, text=lab, fill=p['fg'],
                             font=(FONT, 8), anchor='e')
            bh = max(8, min(15, row_h * 0.52))
            xx = x0 + (val / maxv) * (x1 - x0)
            self.create_rectangle(x0, cy - bh / 2, max(xx, x0 + 2), cy + bh / 2,
                                  fill=col, outline='')
            self.create_text(max(xx, x0 + 2) + 5, cy, text=f'{val:.2f}d',
                             fill=p['fg'], font=(MONO, 8, 'bold'), anchor='w')

    def _draw_error_lines(self, w, h, p):
        series, title, note = self._data
        self.create_text(12, 8, text=_ellipsize(title, w - 150, (FONT, 9, 'bold')),
                         fill=p['fg'], font=(FONT, 9, 'bold'), anchor='nw')
        if note:
            self.create_text(w - 14, 9, text=note, fill=p['muted'],
                             font=(MONO, 7), anchor='ne')
        series = [(lab, col, [(x, v) for x, v in vals if v is not None])
                  for lab, col, vals in series]
        series = [s for s in series if s[2]]
        if not series:
            self.create_text(w / 2, h / 2, text='No monthly validation data yet',
                             fill=p['muted'], font=(FONT, 9), anchor='center')
            return
        labels = [x for x, v in series[0][2]]
        l, r, top, bot = 34, 14, 58, 30
        yt, yb = top, h - bot
        ymax = max(v for _, _, vals in series for _, v in vals) * 1.22 or 1
        n = max(1, len(labels) - 1)

        def X(i):
            return l + (i / n) * (w - l - r)

        def Y(v):
            return yb - (v / ymax) * (yb - yt)

        for frac in (0, 0.5, 1):
            yy = Y(ymax * frac)
            self.create_line(l, yy, w - r, yy, fill=p['border'], dash=(2, 4))
            self.create_text(l - 4, yy, text=f'{ymax * frac:.0f}', fill=p['muted'],
                             font=(MONO, 7), anchor='e')

        lx = w - r - 150
        for j, (lab, col, vals) in enumerate(series):
            ly = 28 + j * 13
            self.create_line(lx, ly, lx + 13, ly, fill=col, width=2)
            self.create_text(lx + 17, ly, text=lab, fill=p['muted'],
                             font=(FONT, 8), anchor='w')
            coords = []
            for i, (_, v) in enumerate(vals):
                coords.extend([X(i), Y(v)])
            if len(coords) >= 4:
                self.create_line(coords, fill=col, width=2, smooth=False)
            for i, (_, v) in enumerate(vals):
                self.create_oval(X(i) - 3, Y(v) - 3, X(i) + 3, Y(v) + 3,
                                 fill=col, outline=p['bg'])

        step = 1 if len(labels) <= 7 else 2
        for i, lab in enumerate(labels):
            if i % step == 0 or i == len(labels) - 1:
                self.create_text(X(i), yb + 4, text=lab, fill=p['muted'],
                                 font=(MONO, 7), anchor='n')


# ------------------------------------------------------------------ prediction card
class PredictionCard:
    """Card with a range headline, likely date, range bar and source footer."""

    def __init__(self, parent, title, accent_role, style):
        self.style = style
        self.accent_role = accent_role
        c = style.colors
        self.frame = tk.Frame(parent, bg=c.inputbg,
                              highlightbackground=c.border, highlightthickness=1, bd=0)
        self.frame.columnconfigure(0, weight=1)
        self.frame.bind('<Configure>', self._on_resize)

        head = tk.Frame(self.frame, bg=c.inputbg)
        head.grid(row=0, column=0, sticky='ew', padx=14, pady=(12, 0))
        head.columnconfigure(1, weight=1)
        self.dot = tk.Frame(head, width=6, height=18, bg=self.accent(), bd=0)
        self.dot.grid(row=0, column=0)
        self.title_lbl = ttk.Label(head, text=title.upper(), style='CardHead.TLabel')
        self.title_lbl.grid(row=0, column=1, sticky='w', padx=(8, 0))
        self.n_lbl = ttk.Label(head, text='', style='CardMuted.TLabel')
        self.n_lbl.grid(row=0, column=2, sticky='e')

        self.range_lbl = ttk.Label(self.frame, text='—', style='CardBig.TLabel')
        self.range_lbl.grid(row=1, column=0, sticky='w', padx=14, pady=(6, 0))

        self.likely_lbl = ttk.Label(self.frame, text='', style='CardMuted.TLabel',
                                    justify='left')
        self.likely_lbl.grid(row=2, column=0, sticky='w', padx=14, pady=(2, 4))

        self.bar = RangeBar(self.frame)
        self.bar.grid(row=3, column=0, sticky='ew', padx=14)

        self.source_lbl = ttk.Label(self.frame, text='', style='CardMuted.TLabel',
                                    justify='left')
        self.source_lbl.grid(row=4, column=0, sticky='w', padx=14, pady=(0, 12))

    def accent(self):
        return getattr(self.style.colors, self.accent_role)

    def _on_resize(self, event):
        wrap = max(140, event.width - 30)
        self.likely_lbl.configure(wraplength=wrap)
        self.source_lbl.configure(wraplength=wrap)

    def restyle(self):
        c = self.style.colors
        self.frame.configure(bg=c.inputbg, highlightbackground=c.border)
        for child in self.frame.winfo_children():
            if isinstance(child, tk.Frame) and child is not self.dot:
                child.configure(bg=c.inputbg)
        self.dot.configure(bg=self.accent())
        self.bar.set_palette(self.palette())

    def palette(self):
        c = self.style.colors
        return dict(bg=c.inputbg, border=c.border, primary=self.accent(),
                    fg=c.fg, muted=c.secondary)

    def set_prediction(self, q, source, n=None):
        p10, p50, p90 = q.get('p10'), q.get('p50'), q.get('p90')
        self.range_lbl.configure(text=fmt_range(p10, p90))
        days = q.get('anchor_days') or q.get('days') or {}
        likely = f'Likely: {fmt_long(p50)}'
        if days.get('p50') is not None:
            likely += f"   ·   ≈ +{days['p50']:.1f} days from anchor"
        if days.get('p10') is not None and days.get('p90') is not None:
            likely += f"  (p10 +{days['p10']:.1f}d / p90 +{days['p90']:.1f}d)"
        self.likely_lbl.configure(text=likely)
        self.n_lbl.configure(text=f'n={n}' if n else '')
        self.source_lbl.configure(text=source or '')
        self.bar.set_range(p10, p50, p90)

    def clear(self):
        self.range_lbl.configure(text='—')
        self.likely_lbl.configure(text='')
        self.n_lbl.configure(text='')
        self.source_lbl.configure(text='')
        self.bar.clear()


# ------------------------------------------------------------------ update dialog
class UpdateDataDialog(ttk.Toplevel):
    def __init__(self, master, on_rebuild):
        super().__init__(title='Update model data', transient=master, resizable=(False, False))
        self.grab_set()
        self.on_rebuild = on_rebuild
        self._new = None

        frame = ttk.Frame(self, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Update model data', font=(FONT, 13, 'bold'))\
            .grid(row=0, column=0, columnspan=3, sticky='w')
        ttk.Label(frame, wraplength=590, justify='left', text=(
            'Select the latest single-sheet workbook (sheet "Shipments"). '
            'The app validates the columns, backs up the current data file, stores '
            'the new one and rebuilds the model in the background.\n\n'
            'A template can be generated with: python scripts/make_template.py'))\
            .grid(row=1, column=0, columnspan=3, sticky='w', pady=(6, 14))

        ttk.Label(frame, text='CN_Rail_Data.xlsx — one row per container',
                  font=(FONT, 9, 'bold')).grid(row=2, column=0, columnspan=3, sticky='w')
        entry = ttk.Entry(frame, width=62)
        current = data_path()
        entry.insert(0, current if os.path.exists(current) else 'No data workbook selected')
        entry.configure(state='readonly')
        entry.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(2, 10))
        self.browse_btn = ttk.Button(frame, text='Browse…', bootstyle='secondary-outline',
                                     command=self._browse)
        self.browse_btn.grid(row=3, column=2, padx=(8, 0), pady=(2, 10))
        self._entry = entry

        self.status_lbl = ttk.Label(frame, text='', foreground='#b45309')
        self.status_lbl.grid(row=4, column=0, columnspan=3, sticky='w', pady=(0, 8))

        self.progress_frame = ttk.Frame(frame)
        self.progress_frame.grid(row=5, column=0, columnspan=3, sticky='ew', pady=(0, 10))
        self.progress_bar = ttk.Progressbar(self.progress_frame, mode='indeterminate',
                                            bootstyle='primary-striped', length=560)
        self.progress_bar.pack(fill='x')
        self.progress_lbl = ttk.Label(self.progress_frame, text='', wraplength=560,
                                      justify='left')
        self.progress_lbl.pack(anchor='w', pady=(4, 0))
        self.progress_frame.grid_remove()

        btns = ttk.Frame(frame)
        btns.grid(row=6, column=0, columnspan=3, sticky='e')
        self.cancel_btn = ttk.Button(btns, text='Cancel', bootstyle='secondary-outline',
                                     command=self.destroy)
        self.cancel_btn.pack(side='right')
        self.rebuild_btn = ttk.Button(btns, text='Rebuild model', bootstyle='primary',
                                      command=self._rebuild)
        self.rebuild_btn.pack(side='right', padx=(0, 8))

    def _browse(self):
        path = filedialog.askopenfilename(
            parent=self, title='Select data workbook',
            filetypes=[('Excel workbook', '*.xlsx *.xlsm'),
                       ('CSV file', '*.csv'), ('All files', '*.*')])
        if not path:
            return
        ok, msg = validate_data_file(path)
        if not ok:
            messagebox.showerror(APP_TITLE, msg, parent=self)
            return
        self._new = path
        self._entry.configure(state='normal')
        self._entry.delete(0, 'end')
        self._entry.insert(0, path)
        self._entry.configure(state='readonly')
        self.status_lbl.configure(text=f'{os.path.basename(path)}: {msg}')

    def _rebuild(self):
        if not self._new and not os.path.exists(data_path()):
            messagebox.showinfo(APP_TITLE,
                                'Choose the single-sheet data workbook first.',
                                parent=self)
            return
        self.rebuild_btn.configure(state='disabled')
        self.browse_btn.configure(state='disabled')
        self.cancel_btn.configure(text='Close')
        self.status_lbl.configure(text='')
        self.progress_frame.grid()
        self.progress_bar.configure(bootstyle='primary-striped')
        self.progress_bar.start(14)
        self.set_progress('Copying the new data workbook…')
        try:
            self.on_rebuild(self._new)
        except Exception as exc:
            self.set_progress(f'Update failed: {exc}', error=True)

    def set_progress(self, text, done=False, error=False):
        if not self.winfo_exists():
            return
        c = ttk.Style().colors
        color = c.danger if error else (c.success if done else c.fg)
        self.progress_lbl.configure(text=text, foreground=color)
        if done or error:
            self.progress_bar.stop()
            self.progress_bar.configure(
                bootstyle=('danger-striped' if error else 'success-striped'))
            self.rebuild_btn.configure(state='normal')
            self.browse_btn.configure(state='normal')


# ------------------------------------------------------------------ app
class App(ttk.Window):
    def __init__(self):
        super().__init__(title=APP_TITLE, themename='bootstrap-light',
                         size=(1320, 900), minsize=(1120, 780))
        self._dark = False
        self._loading = True
        self.rows = []
        self.predictor = None
        self.ml = None
        self.diag = None
        self.ext_series = default_series()
        self._kpi = {}
        self.known = {}
        self.lookup_row = None
        self.last_out = None
        self._update_dialog = None
        self._q = queue.Queue()
        self._pod_opts = {}
        self._ramp_opts = {}
        self._dest_opts = {}

        self._configure_styles()
        self._build_topbar()
        self._build_body()
        self._build_statusbar()

        threading.Thread(target=self._worker, args=(False,), daemon=True).start()
        self.after(120, self._poll)

    # ------------------------------------------------------------------ styles
    def _configure_styles(self):
        c = self.style.colors
        self.style.configure('Card.TLabel', background=c.inputbg, foreground=c.fg, font=(FONT, 10))
        self.style.configure('CardBold.TLabel', background=c.inputbg, foreground=c.fg, font=(FONT, 10, 'bold'))
        self.style.configure('CardHead.TLabel', background=c.inputbg, foreground=c.fg, font=(FONT, 8, 'bold'))
        self.style.configure('CardBig.TLabel', background=c.inputbg, foreground=c.fg, font=(FONT, 15, 'bold'))
        self.style.configure('CardMuted.TLabel', background=c.inputbg, foreground=c.secondary, font=(FONT, 9))
        self.style.configure('Card.TFrame', background=c.inputbg)
        self.style.configure('Page.TFrame', background=c.bg)
        self.style.configure('Small.TLabel', background=c.bg, foreground=c.secondary, font=(FONT, 9))
        self.style.configure('Micro.TLabel', background=c.bg, foreground=c.secondary, font=(FONT, 8, 'bold'))

    # ------------------------------------------------------------------ layout
    def _build_topbar(self):
        c = self.style.colors
        bar = tk.Frame(self, bg=c.bg, highlightbackground=c.border, highlightthickness=1, bd=0)
        bar.pack(side='top', fill='x')
        inner = tk.Frame(bar, bg=c.bg)
        inner.pack(fill='x', padx=20, pady=10)

        badge = tk.Label(inner, text='CN', bg=c.primary, fg=c.selectfg,
                         font=(FONT, 13, 'bold'), width=3, height=1)
        badge.pack(side='left')
        titles = tk.Frame(inner, bg=c.bg)
        titles.pack(side='left', padx=(12, 0))
        ttk.Label(titles, text=APP_TITLE, style='TLabel',
                  font=(FONT, 13, 'bold')).pack(anchor='w')
        ttk.Label(titles, text='Port discharge  →  rail load (RAMPED)  →  destination arrival',
                  style='Small.TLabel').pack(anchor='w')

        self.theme_btn = ttk.Button(inner, text='Dark mode', bootstyle='secondary-outline',
                                    command=self._toggle_theme)
        self.theme_btn.pack(side='right')
        self.update_btn = ttk.Button(inner, text='Update model data…', bootstyle='secondary-outline',
                                     command=self._open_update_dialog)
        self.update_btn.pack(side='right', padx=(0, 8))

        strip_bg = _mix(c.bg, c.border, 0.22)
        strip = tk.Frame(bar, bg=strip_bg)
        strip.pack(fill='x')
        row = tk.Frame(strip, bg=strip_bg)
        row.pack(fill='x', padx=20, pady=(4, 5))
        self._kpi = {}
        self._kpi_caps = []
        self._strip = strip
        self._strip_row = row

        def kpi(key, label, value='—'):
            box = tk.Frame(row, bg=strip_bg)
            box.pack(side='left', padx=(0, 22))
            cap = tk.Label(box, text=label, bg=strip_bg, fg=c.secondary,
                           font=(MONO, 7, 'bold'))
            cap.pack(anchor='w')
            self._kpi_caps.append(cap)
            val = tk.Label(box, text=value, bg=strip_bg, fg=c.fg,
                           font=(MONO, 10, 'bold'))
            val.pack(anchor='w')
            self._kpi[key] = val

        series = default_series()
        teu = sum(v.get('teu') or 0 for v in series.values())
        kpi('journeys', 'JOURNEYS')
        kpi('arrivals', 'ARRIVALS')
        kpi('containers', 'CONTAINERS')
        kpi('model', 'MODEL', 'BOOTING')
        kpi('official', 'OFFICIAL DATA',
            f'{len(series)} PORT-MO · {len({p for p, _, _ in series})} PORTS · {teu/1e6:.1f}M TEU')
        kpi('refresh', 'MODEL BUILT', '—')
        self._topbar = bar
        self._topbar_inner = inner
        self._titles = titles

    def _build_body(self):
        c = self.style.colors
        body = tk.Frame(self, bg=c.bg)
        body.pack(side='top', fill='both', expand=True)
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self._build_form(body)
        self._build_results(body)
        self._body = body

    def _build_form(self, parent):
        c = self.style.colors
        left = ttk.Labelframe(parent, text='  Shipment details  ', padding=(16, 8, 16, 16))
        left.grid(row=0, column=0, sticky='nsw', padx=(20, 10), pady=16)
        left.columnconfigure(0, weight=1)
        r = 0

        def caption(text, hint=None):
            nonlocal r
            ttk.Label(left, text=text.upper(), style='Micro.TLabel')\
                .grid(row=r, column=0, sticky='w', pady=(10, 2))
            r += 1
            if hint:
                ttk.Label(left, text=hint, style='Small.TLabel', wraplength=330)\
                    .grid(row=r, column=0, sticky='w', pady=(6, 0))
                r += 1

        caption('Port of discharge', 'Where the vessel discharges.')
        self.pod_cb = ttk.Combobox(left, state='readonly', width=36)
        self.pod_cb.grid(row=r, column=0, sticky='ew')
        self.pod_cb.bind('<<ComboboxSelected>>', lambda e: self._on_pod_changed())
        r += 1

        caption('Origin ramp (rail load station)', 'Terminal where the container loads on rail.')
        self.ramp_cb = ttk.Combobox(left, state='readonly', width=36)
        self.ramp_cb.grid(row=r, column=0, sticky='ew')
        self.ramp_cb.bind('<<ComboboxSelected>>', lambda e: self._on_ramp_changed())
        r += 1

        caption('Final destination')
        self.dest_cb = ttk.Combobox(left, state='readonly', width=36)
        self.dest_cb.grid(row=r, column=0, sticky='ew')
        self.dest_cb.bind('<<ComboboxSelected>>', lambda e: self._predict())
        r += 1

        caption('Vessel Port ETA (anchor)', 'The date the vessel is expected at the port.')
        self.eta_de = ttk.DateEntry(left, date_format='%d %b %Y')
        self.eta_de.grid(row=r, column=0, sticky='ew')
        self.eta_de.entry.bind('<FocusOut>', lambda e: self._predict())
        self.eta_de.entry.bind('<Return>', lambda e: self._predict())
        r += 1

        self.discharge_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text='Actual vessel discharge date is known',
                        variable=self.discharge_var, bootstyle='round-toggle',
                        command=self._toggle_discharge).grid(row=r, column=0, sticky='w', pady=(12, 2))
        r += 1
        self.discharge_de = ttk.DateEntry(left, date_format='%d %b %Y')
        self.discharge_de.grid(row=r, column=0, sticky='ew')
        self.discharge_de.entry.bind('<FocusOut>', lambda e: self._predict())
        self.discharge_de.entry.bind('<Return>', lambda e: self._predict())
        self._set_dateentry_enabled(self.discharge_de, False)
        r += 1

        ttk.Separator(left).grid(row=r, column=0, sticky='ew', pady=12)
        r += 1

        caption('Container lookup (optional)')
        row = tk.Frame(left, bg=c.bg)
        row.grid(row=r, column=0, sticky='ew')
        row.columnconfigure(0, weight=1)
        self.container_e = ttk.Entry(row)
        self.container_e.grid(row=0, column=0, sticky='ew')
        self.container_e.bind('<Return>', lambda e: self._lookup_container())
        ttk.Button(row, text='Load', bootstyle='secondary-outline',
                   command=self._lookup_container).grid(row=0, column=1, padx=(8, 0))
        r += 1
        self.lookup_lbl = ttk.Label(left, text='', style='Small.TLabel', wraplength=330)
        self.lookup_lbl.grid(row=r, column=0, sticky='w', pady=(4, 0))
        r += 1

        self.tracker_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text='Carrier rail ETA (optional)',
                        variable=self.tracker_var, bootstyle='round-toggle',
                        command=self._toggle_tracker).grid(row=r, column=0, sticky='w', pady=(12, 2))
        r += 1
        self.tracker_de = ttk.DateEntry(left, date_format='%d %b %Y')
        self.tracker_de.grid(row=r, column=0, sticky='ew')
        self.tracker_de.entry.bind('<FocusOut>', lambda e: self._predict())
        self.tracker_de.entry.bind('<Return>', lambda e: self._predict())
        self._set_dateentry_enabled(self.tracker_de, False)
        r += 1
        ttk.Label(left, text='When provided, the carrier ETA is blended into the arrival estimate.',
                  style='Small.TLabel', wraplength=330).grid(row=r, column=0, sticky='w', pady=(4, 0))
        r += 1

        self.predict_btn = ttk.Button(left, text='PREDICT  RANGE', bootstyle='primary',
                                      command=self._predict, state='disabled')
        self.predict_btn.grid(row=r, column=0, sticky='ew', pady=(18, 0), ipady=6)
        self._form = left

    def _build_results(self, parent):
        c = self.style.colors
        right = tk.Frame(parent, bg=c.bg)
        right.grid(row=0, column=1, sticky='nsew', padx=(10, 20), pady=16)
        right.columnconfigure(0, weight=1)

        cards = tk.Frame(right, bg=c.bg)
        cards.grid(row=0, column=0, sticky='ew')
        for i in range(3):
            cards.columnconfigure(i, weight=1, uniform='cards')

        self.card_ramp = PredictionCard(cards, 'Rail load (RAMPED)', 'primary', self.style)
        self.card_arr = PredictionCard(cards, 'At destination (rail arrival)', 'secondary', self.style)
        self.card_deramp = PredictionCard(cards, 'Empty / deramped', 'success', self.style)
        for i, card in enumerate((self.card_ramp, self.card_arr, self.card_deramp)):
            card.frame.grid(row=0, column=i, sticky='nsew', padx=(0 if i == 0 else 8, 0))

        notes = tk.Frame(right, bg=c.inputbg, highlightbackground=c.border,
                         highlightthickness=1, bd=0)
        notes.grid(row=1, column=0, sticky='ew', pady=(14, 0))
        ttk.Label(notes, text='Prediction notes', style='CardBold.TLabel')\
            .pack(anchor='w', padx=14, pady=(10, 0))
        self.notes_text = tk.Text(notes, height=3, wrap='word', bd=0,
                                  bg=c.inputbg, fg=c.fg, relief='flat',
                                  font=(FONT, 9), highlightthickness=0,
                                  padx=14, pady=8, state='disabled')
        self.notes_text.pack(fill='x')
        self._notes_card = notes

        context = ttk.Labelframe(right, text='  Prediction context  ', padding=(8, 4, 8, 8))
        context.grid(row=2, column=0, sticky='nsew', pady=(14, 0))
        right.rowconfigure(2, weight=1)
        context.rowconfigure(0, weight=1)
        context.columnconfigure(0, weight=1)

        self.notebook = ttk.Notebook(context)
        self.notebook.grid(row=0, column=0, sticky='nsew')

        tab1 = ttk.Frame(self.notebook, padding=6)
        tab1.columnconfigure(0, weight=1, uniform='charts')
        tab1.columnconfigure(1, weight=1, uniform='charts')
        tab1.rowconfigure(0, weight=1)
        self.chart_dwell = Chart(tab1)
        self.chart_transit = Chart(tab1)
        self.chart_dwell.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        self.chart_transit.grid(row=0, column=1, sticky='nsew', padx=(8, 0))
        self.notebook.add(tab1, text='  Distributions  ')

        tab2 = ttk.Frame(self.notebook, padding=6)
        tab2.columnconfigure(0, weight=1, uniform='charts')
        tab2.columnconfigure(1, weight=1, uniform='charts')
        tab2.rowconfigure(0, weight=1)
        self.trend_dwell = Chart(tab2)
        self.trend_transit = Chart(tab2)
        self.trend_dwell.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        self.trend_transit.grid(row=0, column=1, sticky='nsew', padx=(8, 0))
        self.notebook.add(tab2, text='  Seasonality  ')

        tab3 = ttk.Frame(self.notebook, padding=6)
        tab3.columnconfigure(0, weight=1, uniform='charts')
        tab3.columnconfigure(1, weight=1, uniform='charts')
        tab3.rowconfigure(0, weight=1)
        self.chart_score = Chart(tab3)
        self.chart_errors = Chart(tab3)
        self.chart_score.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        self.chart_errors.grid(row=0, column=1, sticky='nsew', padx=(8, 0))
        self.notebook.add(tab3, text='  Model lab  ')

        self._charts = [
            (self.chart_dwell, 'primary'),
            (self.chart_transit, 'success'),
            (self.trend_dwell, 'primary'),
            (self.trend_transit, 'success'),
            (self.chart_score, 'primary'),
            (self.chart_errors, 'success'),
        ]
        for chart, role in self._charts:
            chart.set_palette(self._chart_palette(role))
            chart.placeholder('Waiting for the model…')
        self._right = right

    def _build_statusbar(self):
        c = self.style.colors
        self.status = tk.Label(self, text='Loading historical data…',
                               bg=c.bg, fg=c.secondary, font=(FONT, 9), anchor='w')
        self.status.pack(side='bottom', fill='x', padx=20, pady=(0, 6))

    # ------------------------------------------------------------------ helpers
    def _chart_palette(self, role):
        c = self.style.colors
        return dict(bg=c.bg, border=c.border, fg=c.fg, muted=c.secondary,
                    accent=getattr(c, role))

    @staticmethod
    def _set_dateentry_enabled(de, enabled: bool):
        de.entry.configure(state='normal' if enabled else 'disabled')

    @staticmethod
    def _set_de_date(de, d):
        if d is None:
            return
        dt = d if isinstance(d, datetime) else datetime.combine(d, dtime())
        de.set_date(dt)

    def _get_de_date(self, de):
        return de.get_date().date()

    # ------------------------------------------------------------------ theme
    def _toggle_theme(self):
        self._dark = not self._dark
        self.style.theme_use('bootstrap-dark' if self._dark else 'bootstrap-light')
        self.theme_btn.configure(text='Light mode' if self._dark else 'Dark mode')
        self._restyle()

    def _restyle(self):
        c = self.style.colors
        self.configure(bg=c.bg)
        self._configure_styles()
        for w in (self._topbar, self._topbar_inner, self._titles, self._body, self._right,
                  self.card_ramp.frame.master, self.status):
            w.configure(bg=c.bg)
        strip_bg = _mix(c.bg, c.border, 0.22)
        if hasattr(self, '_strip'):
            self._strip.configure(bg=strip_bg)
            self._strip_row.configure(bg=strip_bg)
            for lab in getattr(self, '_kpi_caps', []):
                lab.configure(bg=strip_bg, fg=c.secondary)
            for lab in getattr(self, '_kpi', {}).values():
                lab.configure(bg=strip_bg, fg=c.fg)
            if self.ml is not None and self.ml.ok:
                self._kpi['model'].configure(fg=c.success)
        self._notes_card.configure(bg=c.inputbg, highlightbackground=c.border)
        self.notes_text.configure(bg=c.inputbg, fg=c.fg)
        for card in (self.card_ramp, self.card_arr, self.card_deramp):
            card.restyle()
        for chart, role in self._charts:
            chart.set_palette(self._chart_palette(role))
        self._refresh_diagnostics()

    # ------------------------------------------------------------------ worker
    def _start_loader(self, refresh=False):
        self._loading = True
        self.predict_btn.configure(state='disabled')
        threading.Thread(target=self._worker, args=(refresh,), daemon=True).start()

    def _worker(self, refresh=False):
        try:
            if refresh:
                self._q.put(('progress', 'Reading the data workbook and rebuilding journeys…'))
            rows = build_master(refresh=refresh)
            self._q.put(('progress', 'Fitting the prediction model…'))
            predictor = ETAPredictor([r for r in rows if r['port_eta']])
            self._q.put(('progress', 'Training the ML assist model…'))
            ml = MLPredictor().fit(rows)
            self._q.put(('progress', 'Scoring holdout diagnostics…'))
            try:
                diag = build_diagnostics(rows)
            except Exception:
                diag = None
            self._q.put(('model', rows, predictor, ml, diag))
            self._q.put(('progress', 'Indexing containers for lookup…'))
            known = all_known_containers()
            self._q.put(('index', known))
            suffix = 'ML assist active.' if ml.ok else f'Statistical model only ({ml.reason or "ML unavailable"}).'
            self._q.put(('done', f'Model ready — {len(rows)} journeys, {len(known)} containers indexed. '
                                f'{suffix} {diag_summary(diag)}'))
        except Exception as exc:  # pragma: no cover
            self._q.put(('error', f'{type(exc).__name__}: {exc}'))

    def _poll(self):
        try:
            while True:
                msg = self._q.get_nowait()
                if msg[0] == 'model':
                    self._on_model_ready(msg[1], msg[2], msg[3], msg[4])
                elif msg[0] == 'index':
                    self._on_index_ready(msg[1])
                elif msg[0] == 'progress':
                    self.status.configure(text=msg[1])
                    self._dialog_progress(msg[1])
                elif msg[0] == 'done':
                    self.status.configure(text=msg[1])
                    self._dialog_progress(msg[1], done=True)
                elif msg[0] == 'error':
                    self._loading = False
                    if 'Data workbook not found' in msg[1]:
                        self.status.configure(text='No data workbook loaded — use Update model data… to choose one.')
                    else:
                        self.status.configure(text=f'Load failed: {msg[1].splitlines()[0]}')
                    self._dialog_progress(msg[1], error=True)
                    self.predict_btn.configure(
                        state='normal' if self.predictor is not None else 'disabled')
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _dialog_progress(self, text, done=False, error=False):
        dlg = self._update_dialog
        if dlg is None:
            return
        try:
            if dlg.winfo_exists():
                dlg.set_progress(text, done=done, error=error)
            else:
                self._update_dialog = None
        except tk.TclError:
            self._update_dialog = None

    def _on_model_ready(self, rows, predictor, ml=None, diag=None):
        preserve = None
        if self.predictor is not None:
            preserve = (self._current_pod(), self._current_ramp(), self._current_dest())
        self.rows = rows
        self.predictor = predictor
        self.ml = ml
        self.diag = diag
        self._loading = False
        with_arr = sum(1 for r in rows if r['at_dest_ts'])
        mode = 'ML assist' if (ml is not None and ml.ok) else 'statistical model'
        self.status.configure(
            text=f'Model ready — {len(rows)} journeys ({with_arr} with arrival), {mode}. '
                 f'{diag_summary(diag)}')
        if self._kpi:
            self._kpi['journeys'].configure(text=f'{len(rows)}')
            self._kpi['arrivals'].configure(text=f'{with_arr}')
            self._kpi['model'].configure(
                text='HYBRID · ML ACTIVE' if (ml is not None and ml.ok) else 'STATISTICAL',
                fg=self.style.colors.success if (ml is not None and ml.ok) else self.style.colors.warning)
            self._kpi['refresh'].configure(text=datetime.now().strftime('%d %b %Y  %H:%M'))
        self.predict_btn.configure(state='normal')
        self._populate_pods(preserve)
        self._refresh_diagnostics()

    def _on_index_ready(self, known):
        self.known = known
        self.status.configure(
            text=f'Model ready — {len(self.rows)} journeys, {len(self.known)} containers indexed. '
                 f'{diag_summary(self.diag)}')
        if self._kpi:
            self._kpi['containers'].configure(text=f'{len(known)}')

    # ------------------------------------------------------------------ form logic
    def _populate_pods(self, preserve=None):
        pods = sorted(((pod, q['n']) for pod, q in self.predictor.stats['dwell_pod'].items()),
                      key=lambda x: -x[1])
        labels = []
        self._pod_opts = {}
        for pod, n in pods:
            label = f'{pod}   ({n} arrivals)'
            labels.append(label)
            self._pod_opts[label] = pod
        self.pod_cb.configure(values=labels)
        if not labels:
            return
        target = preserve[0] if preserve and preserve[0] in self._pod_opts.values() else None
        chosen = next((l for l, v in self._pod_opts.items() if v == target), labels[0])
        self.pod_cb.set(chosen)
        self._on_pod_changed(preserve)

    def _current_pod(self):
        return self._pod_opts.get(self.pod_cb.get()) or canon_pod(self.pod_cb.get())

    def _current_ramp(self):
        return self._ramp_opts.get(self.ramp_cb.get()) or self.ramp_cb.get()

    def _current_dest(self):
        return self._dest_opts.get(self.dest_cb.get()) or canon_dest(self.dest_cb.get())

    def _on_pod_changed(self, preserve=None):
        pod = self._current_pod()
        counts = {}
        for r in self.rows:
            if r['pod'] == pod and r['ramp_station']:
                counts[r['ramp_station']] = counts.get(r['ramp_station'], 0) + 1
        for ramp in PORT_RAMPS.get(pod, []):
            counts.setdefault(ramp, 0)
        labels, self._ramp_opts = [], {}
        for ramp, n in sorted(counts.items(), key=lambda x: -x[1]):
            label = f'{ramp}   ({n} loads)'
            labels.append(label)
            self._ramp_opts[label] = ramp
        self.ramp_cb.configure(values=labels)
        if labels:
            target = preserve[1] if preserve and preserve[1] in self._ramp_opts.values() else None
            chosen = next((l for l, v in self._ramp_opts.items() if v == target), labels[0])
            self.ramp_cb.set(chosen)
        self._on_ramp_changed(preserve)

    def _on_ramp_changed(self, preserve=None):
        if not self.predictor:
            return
        ramp = self._current_ramp()
        labels, self._dest_opts = [], {}
        for r0, dest, n, q in self.predictor.lanes():
            if r0 == ramp and dest:
                label = f'{dest}   ({n} trips)'
                labels.append(label)
                self._dest_opts[label] = dest
        self.dest_cb.configure(values=labels)
        if labels:
            target = preserve[2] if preserve and preserve[2] in self._dest_opts.values() else None
            chosen = next((l for l, v in self._dest_opts.items() if v == target), labels[0])
            self.dest_cb.set(chosen)
        self._predict()

    def _toggle_discharge(self):
        self._set_dateentry_enabled(self.discharge_de, self.discharge_var.get())
        self._predict()

    def _toggle_tracker(self):
        self._set_dateentry_enabled(self.tracker_de, self.tracker_var.get())
        self._predict()

    # ------------------------------------------------------------------ actions
    def _open_update_dialog(self):
        if self._loading:
            messagebox.showinfo(APP_TITLE, 'The model is still building. Try again when it is ready.')
            return
        self._update_dialog = UpdateDataDialog(self, self._rebuild_from)

    def _rebuild_from(self, data_new):
        current = data_path()
        if not data_new and not os.path.exists(current):
            raise FileNotFoundError('No data workbook selected and no current workbook exists.')
        os.makedirs(BACKUP_DIR, exist_ok=True)
        if data_new:
            ext = os.path.splitext(data_new)[1].lower()
            target = DATA_CSV if ext == '.csv' else DATA_XLSX
            other = DATA_XLSX if target == DATA_CSV else DATA_CSV
            if os.path.abspath(data_new) != os.path.abspath(target):
                stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                if os.path.exists(target):
                    shutil.copy2(target, os.path.join(
                        BACKUP_DIR, f'{stamp}_{os.path.basename(target)}'))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(data_new, target)
            if os.path.exists(other) and os.path.abspath(other) != os.path.abspath(data_new):
                stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                shutil.copy2(other, os.path.join(
                    BACKUP_DIR, f'{stamp}_{os.path.basename(other)}'))
                os.remove(other)
            os.environ.pop('CN_RAIL_DATA', None)
        self.status.configure(text='New data workbook stored; rebuilding the model…')
        self._start_loader(refresh=True)

    def _lookup_container(self):
        cid = self.container_e.get().strip().upper()
        if not cid:
            return
        row = next((r for r in self.rows if r['container'] == cid), None)
        known = self.known.get(cid)
        if not row and not known:
            self.lookup_lbl.configure(text=f'{cid}: not found in the data workbook.')
            self.lookup_row = None
            return
        self.lookup_row = row
        if row:
            self.lookup_lbl.configure(
                text=f"{cid} — {row['client'] or 'unknown client'} · job {row['job'] or '—'}")
        else:
            self.lookup_lbl.configure(text=f'{cid} found in the data workbook (no rail events yet).')

        src = row or known
        pod, ramp = src.get('pod'), src.get('ramp_station')
        dest, eta = src.get('rail_dest'), src.get('port_eta')
        if pod:
            for label, val in self._pod_opts.items():
                if val == pod:
                    self.pod_cb.set(label)
                    self._on_pod_changed()
                    break
        if ramp:
            for label, val in self._ramp_opts.items():
                if val == ramp:
                    self.ramp_cb.set(label)
                    self._on_ramp_changed()
                    break
        if dest:
            for label, val in self._dest_opts.items():
                if val == dest:
                    self.dest_cb.set(label)
                    break
        if eta:
            self._set_de_date(self.eta_de, eta)
        if row:
            discharge = row.get('discharge_date')
            if discharge:
                self.discharge_var.set(True)
                self._toggle_discharge()
                self._set_de_date(self.discharge_de, discharge)
            if row.get('tracker_rail_eta'):
                self.tracker_var.set(True)
                self._toggle_tracker()
                self._set_de_date(self.tracker_de, row['tracker_rail_eta'])
        self._predict()

    def _predict(self):
        if not self.predictor or self._loading:
            return
        pod, ramp, dest = self._current_pod(), self._current_ramp(), self._current_dest()
        if not (pod and ramp and dest):
            return
        eta = self._get_de_date(self.eta_de)
        discharge = self._get_de_date(self.discharge_de) if self.discharge_var.get() else None
        tracker = self._get_de_date(self.tracker_de) if self.tracker_var.get() else None
        cid = self.container_e.get().strip().upper()
        sibling_dates = []
        sibling_dwells = []
        if eta:
            for r in self.rows:
                if cid and r['container'] == cid:
                    continue
                if (r['pod'] == pod and r['port_eta'] and r['ramp_ts']
                        and r['port_eta'].date() == eta):
                    sibling_dates.append(r['ramp_ts'])
                    sibling_dwells.append((r['ramp_ts'].date() - r['port_eta'].date()).days)
        ml_days = None
        if self.ml is not None and self.ml.ok and eta:
            try:
                ml_days = self.ml.predict(pod=pod, ramp=ramp, dest=dest, port_eta=eta,
                                          sibling_dwells=sibling_dwells)
                if ml_days and discharge and eta:
                    elapsed = max(0, (discharge - eta).days)
                    if elapsed:
                        ml_days['dwell'] = {k: max(0.5, v - elapsed)
                                            for k, v in ml_days['dwell'].items()}
            except Exception as exc:
                ml_days = None
                self.status.configure(text=f'ML assist unavailable this run ({exc}); using statistical model.')
        try:
            out = self.predictor.predict(pod=pod, ramp=ramp, dest=dest, port_eta=eta,
                                         discharge_date=discharge, tracker_rail_eta=tracker,
                                         sibling_ramp_dates=sibling_dates,
                                         ml_days=ml_days)
        except Exception as exc:
            self.status.configure(text=f'Prediction failed: {exc}')
            return
        self.last_out = out

        self.card_ramp.set_prediction(out['ramp'], f"Port dwell model · {out['ramp'].get('source', '')}",
                                      out['ramp'].get('n'))
        self.card_arr.set_prediction(out['arrival'], f"Transit model · {out['arrival'].get('source', '')}",
                                     out['arrival'].get('n'))
        self.card_deramp.set_prediction(out['deramp'], f'Deramp after arrival · {dest}',
                                        out['deramp'].get('days', {}).get('n'))

        lines = []
        if discharge:
            lines.append(f'Anchor: confirmed discharge {fmt_long(discharge)}.')
        else:
            lines.append(f'Anchor: vessel Port ETA {fmt_long(eta)} (dwell model trained on Port ETA).')
        lines.extend(out['notes'])
        if self.lookup_row:
            r = self.lookup_row
            actual = []
            if r['ramp_ts']:
                actual.append(f"RAMPED {fmt_short(r['ramp_ts'], True)}")
            if r['at_dest_ts']:
                actual.append(f"AT DEST {fmt_short(r['at_dest_ts'], True)}")
            if r['deramp_ts']:
                actual.append(f"DERAMPED {fmt_short(r['deramp_ts'], True)}")
            if actual:
                lines.append(f"Actual events for {r['container']}: " + ', '.join(actual) + '.')
        if not out['notes']:
            lines.append('No seasonal adjustments applied for this month/lane.')
        self.notes_text.configure(state='normal', height=max(3, min(8, len(lines))))
        self.notes_text.delete('1.0', 'end')
        self.notes_text.insert('1.0', '\n'.join('•  ' + ln for ln in lines))
        self.notes_text.configure(state='disabled')

        self._refresh_charts(out, pod, ramp, dest, eta)

    def _refresh_diagnostics(self):
        c = self.style.colors
        d = self.diag
        if not d:
            self.chart_score.placeholder('Holdout diagnostics unavailable')
            self.chart_errors.placeholder('No validation months yet')
            return
        items = [
            ('RAMPED · engine', d.get('ramp_engine'), c.primary),
        ]
        if d.get('ml_ok'):
            items.append(('RAMPED · + ML', d.get('ramp_ml'), c.success))
        items.append(('ARRIVAL · engine', d.get('arr_engine'), c.primary))
        if d.get('ml_ok'):
            items.append(('ARRIVAL · + ML', d.get('arr_ml'), c.success))
        if d.get('arr_carrier') is not None:
            items.append(('ARRIVAL · carrier', d.get('arr_carrier'), c.info))
        note = f"n={d['n_test']} · from {d['cut']:%b %Y}"
        self.chart_score.show_bars(items, 'Holdout accuracy · MAE (days)', note)

        series = [('engine', c.primary,
                   [(lab, e) for lab, e, m, n in d.get('months', [])])]
        if d.get('ml_ok'):
            series.append(('ML assist', c.success,
                           [(lab, m) for lab, e, m, n in d.get('months', [])]))
        cov_e = d.get('cov_arr_engine')
        cov_m = d.get('cov_arr_ml')
        cnote = f'80% band {cov_e:.0%}' if cov_e is not None else ''
        if cov_m is not None:
            cnote += f' · ML {cov_m:.0%}'
        self.chart_errors.show_error_lines(series, 'Arrival error by anchor month', cnote)

    def _official_overlay(self, pod, internal):
        profile = monthly_profile(self.ext_series, canon_pod(pod))
        if not profile:
            return None, None
        pairs = [(internal[m], profile[m]) for m in internal
                 if internal.get(m) and profile.get(m)]
        if pairs:
            scale = (statistics.median(v for v, _ in pairs)
                     / statistics.median(v for _, v in pairs))
        else:
            vals = [v for v in internal.values() if v]
            if not vals:
                return None, None
            scale = statistics.median(vals) / statistics.median(profile.values())
        scale = max(0.25, min(4.0, scale))
        return ({m: v * scale for m, v in profile.items()},
                f'official ×{scale:.2f}')

    def _refresh_charts(self, out, pod, ramp, dest, eta):
        dwell_vals = [r['dwell_days'] for r in self.rows
                      if r['pod'] == pod and r['dwell_days'] is not None]
        transit_vals = [r['transit_days'] for r in self.rows
                        if r['ramp_station'] == ramp and r['rail_dest'] == dest
                        and r['transit_days'] is not None]

        self.chart_dwell.show_hist(dwell_vals, out['ramp'].get('days'),
                                   f'Dwell: vessel ETA → RAMPED · {pod}')
        self.chart_transit.show_hist(transit_vals, out['arrival'].get('days'),
                                     f'Transit: RAMPED → arrival · {ramp} → {dest}')

        dwell_med, dwell_n = self._month_medians(
            [r for r in self.rows if r['pod'] == pod], lambda r: r['port_eta'], 'dwell_days')
        transit_med, transit_n = self._month_medians(
            [r for r in self.rows if r['ramp_station'] == ramp and r['rail_dest'] == dest],
            lambda r: r['ramp_ts'], 'transit_days')

        ramp_p50 = out['ramp']['p50']
        arr_p50 = out['arrival']['p50']
        ext_dwell, ext_label = self._official_overlay(pod, dwell_med)
        self.trend_dwell.show_trend(
            dwell_med, dwell_n, eta.month,
            (out['ramp'].get('days') or {}).get('p50'),
            f'Median dwell by anchor month · {pod}',
            external=ext_dwell, external_label=ext_label)
        self.trend_transit.show_trend(
            transit_med, transit_n, arr_p50.month if arr_p50 else eta.month,
            (out['arrival'].get('days') or {}).get('p50'),
            f'Median transit by ramp month · {ramp} → {dest}')

    @staticmethod
    def _month_medians(rows, month_of, value_key):
        buckets = {m: [] for m in range(1, 13)}
        for r in rows:
            v = r.get(value_key)
            d = month_of(r)
            if v is not None and d is not None:
                buckets[d.month].append(v)
        med = {}
        ns = {}
        for m, vals in buckets.items():
            ns[m] = len(vals)
            if vals:
                vals = sorted(vals)
                med[m] = vals[len(vals) // 2]
        return med, ns


def main():
    app = App()
    app.mainloop()


if __name__ == '__main__':
    main()
