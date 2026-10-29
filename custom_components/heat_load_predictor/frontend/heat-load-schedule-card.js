/*
 * Heat Load Schedule Card
 * Editor for the Heat Load Predictor's daily target temperature curve, with
 * the forecast heating electricity per period drawn underneath.
 *
 *   type: custom:heat-load-schedule-card
 *   entry_id: optional, if you have more than one predictor
 *   title: optional
 *   time_step: 15        # minutes; drag snaps to this (Shift: 5)
 *   temp_step: 0.5       # °C; drag snaps to this (Shift: 0.1)
 *   y_min / y_max        # temperature axis, default 14–24 (widens to fit)
 *   show_battery: true   # battery windows and projected SOC, if the planner is on
 *   show_solar: true     # solar forecast bars (own scale), if the planner is on
 *
 * Interaction: drag a point to move it; click empty space to add a point;
 * double-click or right-click a point to delete it.
 */

const DOMAIN = "heat_load_predictor";
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const W = 760, H = 332;
const M = { l: 46, r: 50, t: 26, b: 42 };   // bottom: mode strip + time labels
let PW = W - M.l - M.r;
const PH = H - M.t - M.b;

const toMin = (s) => { const [h, m] = s.split(":").map(Number); return h * 60 + m; };
const toHHMM = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const round = (v, step) => Math.round(v / step) * step;
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
// Monday-first weekday index (0..6) of a YYYY-MM-DD string, timezone-free.
const weekdayOf = (ymd) => { const [y, m, d] = ymd.split("-").map(Number); return (new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7; };

/** Target temperature at `minute` on day index `di`, mirroring model.schedule_temp. */
function tempAt(schedule, di, minute) {
  const days = schedule.days;
  const today = days[DAYS[di]], prevDay = days[DAYS[(di + 6) % 7]], nextDay = days[DAYS[(di + 1) % 7]];
  const line = [[toMin(prevDay[prevDay.length - 1].time) - 1440, prevDay[prevDay.length - 1].temp]];
  for (const p of today) line.push([toMin(p.time), p.temp]);
  line.push([toMin(nextDay[0].time) + 1440, nextDay[0].temp]);
  let prev = line[0];
  for (const pt of line.slice(1)) {
    if (pt[0] > minute) {
      if (schedule.interpolation === "linear") return prev[1] + (pt[1] - prev[1]) * (minute - prev[0]) / (pt[0] - prev[0]);
      return prev[1];
    }
    prev = pt;
  }
  return prev[1];
}

class HeatLoadScheduleCard extends HTMLElement {
  setConfig(config) {
    this._config = { time_step: 15, temp_step: 0.5, y_min: 14, y_max: 24, show_battery: true, show_solar: true, ...config };
    this._day = (new Date().getDay() + 6) % 7;
    if (!this.shadowRoot) this._build();
  }

  static getStubConfig() { return {}; }
  getCardSize() { return 9; }
  getGridOptions() { return { columns: 12, min_columns: 6, rows: "auto" }; }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._load();
  }

  disconnectedCallback() { clearInterval(this._timer); this._timer = null; }
  connectedCallback() {
    if (this._hass && !this._timer) this._timer = setInterval(() => this._loadForecast(), 5 * 60 * 1000);
  }

  // ------------------------------------------------------------------ data

  async _ws(type, extra = {}) {
    const msg = { type: `${DOMAIN}/${type}`, ...extra };
    if (this._config.entry_id) msg.entry_id = this._config.entry_id;
    return this._hass.callWS(msg);
  }

  async _load() {
    try {
      const res = await this._ws("schedule/get");
      this._entryId = res.entry_id;
      this._schedule = JSON.parse(JSON.stringify(res.schedule));
      this._saved = JSON.stringify(res.schedule);
      this._error = null;
    } catch (e) {
      this._error = e.message || "Heat Load Predictor isn't set up";
    }
    await this._loadForecast();
    if (!this._timer) this._timer = setInterval(() => this._loadForecast(), 5 * 60 * 1000);
  }

  async _loadForecast() {
    try {
      const f = await this._ws("forecast");
      this._forecast = f.periods || [];
      this._battery = f.battery && !f.battery.error ? f.battery : null;
    } catch (e) {
      this._forecast = [];
      this._battery = null;
    }
    this._render();
  }

  get _dirty() { return this._schedule && JSON.stringify(this._schedule) !== this._saved; }
  get _points() { return this._schedule.days[DAYS[this._day]]; }

  /** Forecast periods for the selected weekday's next date, plus that date. */
  _dayForecast() {
    const byDate = new Map();
    for (const p of this._forecast || []) {
      const d = p.start.slice(0, 10);
      if (!byDate.has(d)) byDate.set(d, []);
      byDate.get(d).push(p);
    }
    for (const [d, rows] of byDate) if (weekdayOf(d) === this._day) return { date: d, rows };
    return { date: null, rows: [] };
  }

  _datesByWeekday() {
    const map = {};
    for (const p of this._forecast || []) {
      const d = p.start.slice(0, 10), wd = weekdayOf(d);
      if (!(wd in map)) map[wd] = d;
    }
    return map;
  }

  // ------------------------------------------------------------------ edits

  _commit(points) {
    points.sort((a, b) => toMin(a.time) - toMin(b.time));
    this._schedule.days[DAYS[this._day]] = points;
    this._render();
  }

  _deletePoint(i) {
    const pts = this._points;
    if (pts.length <= 1) return;
    pts.splice(i, 1);
    this._commit(pts);
  }

  _copyTo(target) {
    const src = JSON.stringify(this._points);
    const idx = target === "all" ? [0, 1, 2, 3, 4, 5, 6] : target === "weekdays" ? [0, 1, 2, 3, 4] : [5, 6];
    for (const i of idx) this._schedule.days[DAYS[i]] = JSON.parse(src);
    this._flash(`Copied ${DAY_NAMES[this._day]} to ${target === "all" ? "every day" : target}`);
    this._render();
  }

  async _save() {
    this._busy = true; this._render();
    try {
      const res = await this._ws("schedule/set", { schedule: this._schedule });
      this._schedule = JSON.parse(JSON.stringify(res.schedule));
      this._saved = JSON.stringify(res.schedule);
      this._flash("Saved; forecast updated");
      await this._loadForecast();
    } catch (e) {
      this._flash(`Couldn't save: ${e.message || e.code}`, true);
    }
    this._busy = false; this._render();
  }

  _revert() {
    this._schedule = JSON.parse(this._saved);
    this._render();
  }

  _flash(text, bad = false) {
    this._msg = { text, bad };
    clearTimeout(this._msgTimer);
    this._msgTimer = setTimeout(() => { this._msg = null; this._renderFooter(); }, 4000);
  }

  // ------------------------------------------------------------------ scales

  _yRange() {
    let lo = this._config.y_min, hi = this._config.y_max;
    const all = [];
    for (const d of DAYS) for (const p of this._schedule.days[d]) all.push(p.temp);
    for (const r of this._dayForecast().rows) all.push(r.t_in);
    if (all.length) { lo = Math.min(lo, Math.floor(Math.min(...all) - 0.5)); hi = Math.max(hi, Math.ceil(Math.max(...all) + 0.5)); }
    return [lo, hi];
  }
  _x(min) { return M.l + (min / 1440) * PW; }
  _y(t) { const [lo, hi] = this._yRange(); return M.t + (1 - (t - lo) / (hi - lo)) * PH; }
  _kwhMax() {
    const max = Math.max(0, ...(this._forecast || []).map((r) => r.kwh));
    return Math.max(0.5, Math.ceil(max * 4) / 4);
  }
  _fromSvg(evt) {
    const svg = this._svg, pt = svg.createSVGPoint();
    pt.x = evt.clientX; pt.y = evt.clientY;
    const p = pt.matrixTransform(svg.getScreenCTM().inverse());
    const [lo, hi] = this._yRange();
    return { min: ((p.x - M.l) / PW) * 1440, temp: lo + (1 - (p.y - M.t) / PH) * (hi - lo) };
  }

  // ------------------------------------------------------------------ render

  _build() {
    const root = this.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { padding: 16px; }
        .head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
        .title { font-size: 1.2em; font-weight: 500; flex: 1 1 auto; color: var(--primary-text-color); }
        .tabs { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 8px; }
        button { font: inherit; cursor: pointer; border-radius: 8px; border: 1px solid var(--divider-color, #ccc);
                 background: var(--card-background-color, #fff); color: var(--primary-text-color); padding: 6px 10px; }
        button:hover { border-color: var(--primary-color); }
        button.on { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
        button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
        button:disabled { opacity: .5; cursor: default; }
        .tab small { opacity: .7; margin-left: 3px; }
        svg { width: 100%; height: auto; display: block; touch-action: none; user-select: none; }
        .grid line { stroke: var(--divider-color, #ddd); stroke-width: 1; }
        .axis text { fill: var(--secondary-text-color, #666); font-size: 11px; }
        .bar { fill: var(--accent-color, #ff9800); opacity: .45; }
        .bar:hover { opacity: .8; }
        .curve { fill: none; stroke: var(--primary-color, #03a9f4); stroke-width: 2.5; }
        .tin { fill: none; stroke: var(--primary-text-color); stroke-width: 1.5; stroke-dasharray: 4 4; opacity: .6; }
        .sun { fill: #f4c430; opacity: .28; }
        .mode { cursor: default; }
        .mode.feedin { fill: #1e88e5; }
        .mode.selfuse { fill: var(--secondary-text-color); opacity: .3; }
        .win.charge { fill: var(--success-color, #43a047); }
        .win.discharge { fill: #ff8f00; }
        .win { opacity: .13; }
        .win.off { opacity: .04; stroke: var(--secondary-text-color); stroke-dasharray: 3 3; stroke-opacity: .6; }
        .winlbl { font-size: 11px; font-weight: 500; cursor: help; }
        .winlbl.charge { fill: var(--success-color, #43a047); }
        .winlbl.discharge { fill: #ff8f00; }
        .winlbl.off { fill: var(--secondary-text-color); font-weight: 400; }
        .soc { fill: none; stroke: var(--success-color, #43a047); stroke-width: 2; }
        .socax text { fill: var(--success-color, #43a047); font-size: 11px; }
        .now { stroke: var(--error-color, #db4437); stroke-width: 1.5; opacity: .7; }
        .pt { fill: var(--card-background-color, #fff); stroke: var(--primary-color, #03a9f4); stroke-width: 2.5; cursor: grab; }
        .pt.drag { fill: var(--primary-color); cursor: grabbing; }
        .hit { fill: transparent; cursor: crosshair; }
        .pthit { fill: transparent; cursor: grab; }
        .legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: .85em; color: var(--secondary-text-color); margin: 4px 0 10px; }
        .sw { display: inline-block; width: 18px; height: 0; vertical-align: middle; margin-right: 5px; }
        .tip { position: absolute; pointer-events: none; background: var(--card-background-color, #fff); color: var(--primary-text-color);
               border: 1px solid var(--divider-color); border-radius: 6px; padding: 4px 8px; font-size: .8em; white-space: nowrap; display: none;
               box-shadow: 0 2px 6px rgba(0,0,0,.15); }
        .wrap { position: relative; }
        table { width: auto; border-collapse: collapse; margin-top: 4px; }
        td, th { padding: 3px 4px; text-align: left; font-size: .9em; color: var(--primary-text-color); }
        th { font-weight: 500; color: var(--secondary-text-color); }
        input { font: inherit; padding: 4px 6px; border-radius: 6px; border: 1px solid var(--divider-color, #ccc);
                background: var(--card-background-color, #fff); color: var(--primary-text-color); width: 7.5em; box-sizing: border-box; }
        .row { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin-top: 10px; }
        .spacer { flex: 1; }
        .msg { font-size: .9em; color: var(--success-color, #43a047); }
        .msg.bad { color: var(--error-color, #db4437); }
        .total { font-size: .95em; color: var(--primary-text-color); }
        .err { color: var(--error-color, #db4437); }
        .hint { font-size: .8em; color: var(--secondary-text-color); }
      </style>
      <ha-card>
        <div class="head"><div class="title"></div><div class="interp"></div></div>
        <div class="tabs"></div>
        <div class="wrap"><svg viewBox="0 0 ${W} ${H}"></svg><div class="tip"></div></div>
        <div class="legend">
          <span><span class="sw" style="border-top:3px solid var(--primary-color,#03a9f4)"></span>Target</span>
          <span><span class="sw" style="border-top:2px dashed var(--primary-text-color)"></span>Modelled indoor</span>
          <span><span class="sw" style="height:10px;background:var(--accent-color,#ff9800);opacity:.5"></span>Heating electricity (kWh per period)</span>
          <span class="legend-batt"></span>
        </div>
        <div class="hint">Drag points to move them, click empty space to add one, and double-click or right-click a point to delete it. Hold Shift for finer steps.</div>
        <div class="table"></div>
        <div class="row copy"></div>
        <div class="row footer"></div>
      </ha-card>`;
    this._svg = root.querySelector("svg");
    this._tip = root.querySelector(".tip");
    this._svg.addEventListener("pointerdown", (e) => this._onDown(e));
    this._svg.addEventListener("pointermove", (e) => this._onMove(e));
    this._svg.addEventListener("pointerup", (e) => this._onUp(e));
    this._svg.addEventListener("pointercancel", (e) => this._onUp(e));
    this._svg.addEventListener("contextmenu", (e) => {
      const i = e.target.dataset?.i; if (i !== undefined) { e.preventDefault(); this._deletePoint(Number(i)); }
    });
    this._svg.addEventListener("mouseleave", () => { this._tip.style.display = "none"; });
  }

  _render() {
    if (!this.shadowRoot) return;
    const r = this.shadowRoot;
    r.querySelector(".title").textContent = this._config.title || "Heating schedule";
    if (this._error) {
      r.querySelector(".tabs").innerHTML = `<div class="err">${esc(this._error)}</div>`;
      return;
    }
    if (!this._schedule) return;
    this._renderHead();
    this._renderSvg();
    this._renderTable();
    this._renderFooter();
  }

  _renderHead() {
    const r = this.shadowRoot;
    const interp = this._schedule.interpolation;
    r.querySelector(".interp").innerHTML = `
      <button data-interp="step" class="${interp === "step" ? "on" : ""}" title="Hold each temperature until the next point">Steps</button>
      <button data-interp="linear" class="${interp === "linear" ? "on" : ""}" title="Ramp between points">Ramps</button>`;
    r.querySelectorAll("[data-interp]").forEach((b) => (b.onclick = () => { this._schedule.interpolation = b.dataset.interp; this._render(); }));

    const dates = this._datesByWeekday();
    // Tabs run from today: today, tomorrow, ... six days on.
    const first = (new Date().getDay() + 6) % 7;
    r.querySelector(".tabs").innerHTML = [0, 1, 2, 3, 4, 5, 6].map((k) => {
      const i = (first + k) % 7, n = DAY_NAMES[i];
      return `<button class="tab ${i === this._day ? "on" : ""}" data-day="${i}">${n}${dates[i] ? `<small>${Number(dates[i].slice(8))}</small>` : ""}</button>`;
    }).join("");
    r.querySelectorAll("[data-day]").forEach((b) => (b.onclick = () => { this._day = Number(b.dataset.day); this._render(); }));

    r.querySelector(".copy").innerHTML = `<span class="hint">Copy ${DAY_NAMES[this._day]} to:</span>
      <button data-copy="weekdays">Weekdays</button><button data-copy="weekend">Weekend</button><button data-copy="all">Every day</button>`;
    r.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = () => this._copyTo(b.dataset.copy)));
  }

  get _showBattery() { return !!(this._battery && this._config.show_battery); }

  /** Minutes [start, end) of an ISO local time range clipped to `date`, or null. */
  _clip(startIso, endIso, date) {
    const sd = startIso.slice(0, 10), ed = endIso.slice(0, 10);
    if (sd > date || ed < date) return null;
    const s = sd < date ? 0 : toMin(startIso.slice(11, 16));
    const e = ed > date ? 1440 : toMin(endIso.slice(11, 16));
    return e > s ? [s, e] : null;
  }

  _renderSvg() {
    M.r = this._showBattery ? 92 : 50;
    PW = W - M.l - M.r;
    const [lo, hi] = this._yRange();
    const kmax = this._kwhMax();
    const { rows } = this._dayForecast();
    const parts = [];

    // grid and axes
    parts.push(`<g class="grid">`);
    for (let h = 0; h <= 24; h += 3) parts.push(`<line x1="${this._x(h * 60)}" x2="${this._x(h * 60)}" y1="${M.t}" y2="${M.t + PH}"/>`);
    for (let t = Math.ceil(lo); t <= hi; t += 1) parts.push(`<line x1="${M.l}" x2="${M.l + PW}" y1="${this._y(t)}" y2="${this._y(t)}" opacity="${t % 2 ? 0.4 : 1}"/>`);
    parts.push(`</g><g class="axis">`);
    for (let h = 0; h <= 24; h += 3) parts.push(`<text x="${this._x(h * 60)}" y="${M.t + PH + 28}" text-anchor="middle">${String(h).padStart(2, "0")}:00</text>`);
    for (let t = Math.ceil(lo); t <= hi; t += 2) parts.push(`<text x="${M.l - 6}" y="${this._y(t) + 4}" text-anchor="end">${t}°</text>`);
    for (let k = 0; k <= 4; k++) {
      const v = (kmax * k) / 4;
      parts.push(`<text x="${M.l + PW + 6}" y="${M.t + PH - (k / 4) * PH + 4}">${v.toFixed(2)}</text>`);
    }
    parts.push(`<text x="${M.l + PW + 6}" y="12">kWh</text><text x="${M.l - 6}" y="12" text-anchor="end">°C</text></g>`);
    const date = this._dayForecast().date;
    const bat = this._showBattery ? this._battery : null;
    if (bat) {
      parts.push(`<g class="socax">`);
      for (let k = 0; k <= 4; k++) parts.push(`<text x="${M.l + PW + 50}" y="${M.t + PH - (k / 4) * PH + 4}">${k * 25}%</text>`);
      parts.push(`<text x="${M.l + PW + 50}" y="12">SOC</text></g>`);
    }

    parts.push(`<rect class="hit" x="${M.l}" y="${M.t}" width="${PW}" height="${PH}"/>`);

    // battery windows (behind everything else)
    this._winShown = [];
    if (bat && date) {
      (bat.windows || []).forEach((w) => {
        const c = this._clip(w.start, w.end, date);
        if (!c) return;
        const cls = `${w.kind}${w.enabled ? "" : " off"}`;
        const x0 = this._x(c[0]), x1 = this._x(c[1]);
        parts.push(`<rect class="win ${cls}" pointer-events="none" x="${x0}" y="${M.t}" width="${Math.max(1, x1 - x0)}" height="${PH}"/>`);
        const lbl = w.enabled ? `${w.kind === "charge" ? "▲" : "▼"} ${w.target_soc}%` : "off";
        const i = this._winShown.push(w) - 1;
        parts.push(`<text class="winlbl ${cls}" data-win="${i}" x="${(x0 + x1) / 2}" y="${M.t + 12}" text-anchor="middle">${lbl}</text>`);
      });
    }

    // solar forecast, scaled to its own peak
    if (bat && date && this._config.show_solar && (bat.solar || []).length) {
      const smax = Math.max(0.01, ...bat.solar.map((r) => r.kwh));
      const per = this._guessPeriod();
      for (const r of bat.solar) {
        if (r.start.slice(0, 10) !== date || r.kwh <= 0) continue;
        const s = toMin(r.start.slice(11, 16));
        const h = (r.kwh / smax) * PH * 0.9;
        parts.push(`<rect class="sun" pointer-events="none" x="${this._x(s)}" y="${M.t + PH - h}" width="${Math.max(1, this._x(s + per) - this._x(s))}" height="${h}"/>`);
      }
    }

    // forecast bars
    for (const row of rows) {
      const s = toMin(row.start.slice(11, 16));
      const e = s + this._guessPeriod();
      const h = (row.kwh / kmax) * PH;
      parts.push(`<rect class="bar" data-bar="${esc(row.start)}" x="${this._x(s) + 0.5}" y="${M.t + PH - h}" width="${Math.max(1, this._x(e) - this._x(s) - 1)}" height="${h}"/>`);
    }

    // modelled indoor temperature (period end values)
    if (rows.length) {
      const per = this._guessPeriod();
      const pts = rows.map((row) => `${this._x(Math.min(1440, toMin(row.start.slice(11, 16)) + per))},${this._y(row.t_in)}`);
      const first = rows[0];
      pts.unshift(`${this._x(toMin(first.start.slice(11, 16)))},${this._y(first.t_in)}`);
      parts.push(`<polyline class="tin" points="${pts.join(" ")}" pointer-events="none"/>`);
    }

    // projected battery SOC (values at period ends)
    if (bat && date) {
      const pts = [];
      for (const r of bat.soc_forecast || []) {
        const d = r.time.slice(0, 10), m = toMin(r.time.slice(11, 16));
        let x = null;
        if (d === date) x = m;
        else if (d > date && m === 0 && pts.length) x = 1440;
        if (x === null) continue;
        pts.push(`${this._x(x)},${M.t + PH * (1 - r.soc / 100)}`);
        if (x === 1440) break;
      }
      if (pts.length > 1) parts.push(`<polyline class="soc" pointer-events="none" points="${pts.join(" ")}"/>`);
    }

    // storage mode strip, just under the plot
    this._modeShown = [];
    if (bat && date) {
      for (const r of bat.mode_runs || []) {
        const c = this._clip(r.start, r.end, date);
        if (!c) continue;
        const i = this._modeShown.push(r) - 1;
        const x0 = this._x(c[0]), x1 = this._x(c[1]);
        const cls = r.mode === "Self-Use" ? "selfuse" : "feedin";
        parts.push(`<rect class="mode ${cls}" data-mode="${i}" x="${x0}" y="${M.t + PH + 2}" width="${Math.max(1, x1 - x0)}" height="6"/>`);
      }
    }

    // now marker
    if (date && this._forecast?.length && this._forecast[0].start.slice(0, 10) === date) {
      const nowMin = toMin(this._forecast[0].start.slice(11, 16));
      parts.push(`<line class="now" pointer-events="none" x1="${this._x(nowMin)}" x2="${this._x(nowMin)}" y1="${M.t}" y2="${M.t + PH}"/>`);
    }

    // target curve
    const di = this._day, sched = this._schedule;
    let d = "";
    if (sched.interpolation === "linear") {
      const xs = [0, ...this._points.map((p) => toMin(p.time)), 1440];
      d = xs.map((m, i) => `${i ? "L" : "M"}${this._x(m)},${this._y(tempAt(sched, di, Math.min(m, 1439.999)))}`).join(" ");
    } else {
      let t = tempAt(sched, di, 0);
      d = `M${this._x(0)},${this._y(t)}`;
      for (const p of this._points) {
        const m = toMin(p.time);
        d += ` L${this._x(m)},${this._y(t)} L${this._x(m)},${this._y(p.temp)}`;
        t = p.temp;
      }
      d += ` L${this._x(1440)},${this._y(t)}`;
    }
    parts.push(`<path class="curve" d="${d}" pointer-events="none"/>`);
    this._points.forEach((p, i) => {
      parts.push(`<circle class="pthit" data-i="${i}" cx="${this._x(toMin(p.time))}" cy="${this._y(p.temp)}" r="18"/>`);
      parts.push(`<circle class="pt${this._drag?.i === i ? " drag" : ""}" data-i="${i}" cx="${this._x(toMin(p.time))}" cy="${this._y(p.temp)}" r="7"><title>${p.time} · ${p.temp}°C</title></circle>`);
    });
    this._svg.innerHTML = parts.join("");

    this._svg.querySelectorAll("[data-bar]").forEach((el) => {
      el.addEventListener("mousemove", (e) => this._showTip(e, el.dataset.bar));
      el.addEventListener("mouseleave", () => (this._tip.style.display = "none"));
    });
    this._svg.querySelectorAll("[data-mode]").forEach((el) => {
      el.addEventListener("mousemove", (e) => {
        const r = this._modeShown[Number(el.dataset.mode)];
        const hm = (iso) => iso.slice(11, 16);
        const what = r.mode === "Self-Use"
          ? "Solar goes to the house, then the battery"
          : "Solar exports first (up to the limit), the battery takes the rest";
        const end = hm(r.end) === "00:00" ? "24:00" : hm(r.end);
        this._tip.innerHTML = `<b>${esc(r.mode)}</b> ${hm(r.start)}–${end}<br>${what}`;
        this._placeTip(e);
      });
      el.addEventListener("mouseleave", () => (this._tip.style.display = "none"));
    });
    this._svg.querySelectorAll("[data-win]").forEach((el) => {
      el.addEventListener("mousemove", (e) => this._showWinTip(e, this._winShown[Number(el.dataset.win)]));
      el.addEventListener("mouseleave", () => (this._tip.style.display = "none"));
    });
    const lb = this.shadowRoot.querySelector(".legend-batt");
    lb.innerHTML = bat ? `<span><span class="sw" style="border-top:2px solid var(--success-color,#43a047)"></span>Battery SOC</span>
      <span><span class="sw" style="height:10px;background:var(--success-color,#43a047);opacity:.25"></span>Charge window</span>
      <span><span class="sw" style="height:10px;background:#ff8f00;opacity:.25"></span>Discharge window</span>
      ${this._config.show_solar && (bat.solar || []).length ? `<span><span class="sw" style="height:10px;background:#f4c430;opacity:.45"></span>Solar (own scale)</span>` : ""}
      ${(bat.mode_runs || []).length ? `<span><span class="sw" style="height:6px;background:#1e88e5"></span>Feed-in priority</span>
      <span><span class="sw" style="height:6px;background:var(--secondary-text-color);opacity:.3"></span>Self-Use</span>` : ""}` : "";
    lb.style.display = bat ? "contents" : "none";
  }

  _guessPeriod() {
    const f = this._forecast || [];
    if (f.length >= 2) return Math.round((Date.parse(f[1].start) - Date.parse(f[0].start)) / 60000);
    return 30;
  }

  _batteryAt(start) {
    const bat = this._showBattery ? this._battery : null;
    if (!bat) return "";
    const t = Date.parse(start), per = this._guessPeriod() * 60000;
    const sun = (bat.solar || []).find((r) => Date.parse(r.start) === t);
    const soc = (bat.soc_forecast || []).find((r) => Date.parse(r.time) === t + per);
    const parts = [];
    if (sun && sun.kwh > 0) parts.push(`Solar ${sun.kwh.toFixed(2)} kWh`);
    if (soc) parts.push(`Battery ${Math.round(soc.soc)}%`);
    return parts.length ? `<br>${parts.join(" · ")}` : "";
  }

  _placeTip(e) {
    const box = this.shadowRoot.querySelector(".wrap").getBoundingClientRect();
    this._tip.style.display = "block";
    const x = e.clientX - box.left + 12;
    this._tip.style.left = `${Math.max(0, Math.min(x, box.width - this._tip.offsetWidth - 4))}px`;
    this._tip.style.top = `${e.clientY - box.top + 12}px`;
  }

  _showWinTip(e, w) {
    if (!w) return;
    const hm = (iso) => iso.slice(11, 16);
    const what = w.kind === "charge" ? `Charge slot ${w.slot}` : `Discharge slot ${w.slot}`;
    this._tip.innerHTML = `<b>${what}</b> · ${hm(w.start)}–${hm(w.end)}<br>
      ${w.enabled ? `${w.kind === "charge" ? "Charge" : "Discharge"} to <b>${w.target_soc}%</b>` : "Off"}
      · expected ${Math.round(w.projected_soc)}% at start<br>${esc(w.reason)}`;
    this._placeTip(e);
  }

  _showTip(e, start) {
    const row = (this._forecast || []).find((r) => r.start === start);
    if (!row) return;
    const s = toMin(start.slice(11, 16)), per = this._guessPeriod();
    this._tip.innerHTML = `<b>${toHHMM(s)}–${toHHMM((s + per) % 1440)}</b> · ${row.kwh.toFixed(2)} kWh<br>
      Outdoor ${row.t_out}°C · COP ${row.cop}<br>Indoor ${row.t_in}°C (target ${row.t_target}°C)${"flow" in row ? `<br>Flow ${row.flow == null ? "idle" : row.flow + "°C"} (planned)` : ""}${row.shortfall_k > 0.05 ? `<br><span class="err">Short by ${row.shortfall_k} K</span>` : ""}${this._batteryAt(start)}`;
    this._placeTip(e);
  }

  _renderTable() {
    const r = this.shadowRoot;
    const rows = this._points.map((p, i) => `<tr>
        <td><input type="time" step="300" value="${p.time}" data-time="${i}"></td>
        <td><input type="number" step="0.1" min="5" max="30" value="${p.temp}" data-temp="${i}"> °C</td>
        <td><button data-del="${i}" ${this._points.length <= 1 ? "disabled" : ""} title="Delete">✕</button></td></tr>`).join("");
    r.querySelector(".table").innerHTML = `<table><tr><th>From</th><th>Target</th><th></th></tr>${rows}</table>
      <div class="row"><button data-add>+ Add point</button></div>`;
    r.querySelectorAll("[data-time]").forEach((el) => (el.onchange = () => {
      const i = Number(el.dataset.time), m = toMin(el.value || "00:00");
      const pts = this._points;
      if (pts.some((p, j) => j !== i && toMin(p.time) === m)) { this._flash("There's already a point at that time", true); this._render(); return; }
      pts[i].time = toHHMM(m); this._commit(pts);
    }));
    r.querySelectorAll("[data-temp]").forEach((el) => (el.onchange = () => {
      const v = parseFloat(el.value);
      if (Number.isNaN(v)) return this._render();
      this._points[Number(el.dataset.temp)].temp = clamp(Math.round(v * 10) / 10, 5, 30);
      this._commit(this._points);
    }));
    r.querySelectorAll("[data-del]").forEach((el) => (el.onclick = () => this._deletePoint(Number(el.dataset.del))));
    r.querySelector("[data-add]").onclick = () => {
      const pts = this._points, used = new Set(pts.map((p) => toMin(p.time)));
      let m = 12 * 60;
      while (used.has(m) && m < 1435) m += this._config.time_step;
      if (used.has(m)) return;
      pts.push({ time: toHHMM(m), temp: Math.round(tempAt(this._schedule, this._day, m) * 10) / 10 });
      this._commit(pts);
    };
  }

  _renderFooter() {
    const r = this.shadowRoot;
    if (!this._schedule) return;
    const { date, rows } = this._dayForecast();
    const total = rows.reduce((a, b) => a + b.kwh, 0);
    const per = this._guessPeriod();
    const full = rows.length >= Math.floor(1440 / per) - 1;
    const totalTxt = date
      ? `${full ? "Forecast" : "Rest of day"}: <b>${total.toFixed(1)} kWh</b>${this._dirty ? " (save to update)" : ""}`
      : "No forecast for this day yet";
    const msg = this._msg ? `<span class="msg ${this._msg.bad ? "bad" : ""}">${esc(this._msg.text)}</span>` : "";
    r.querySelector(".footer").innerHTML = `<span class="total">${totalTxt}</span>${msg}<span class="spacer"></span>
      ${this._battery ? `<button data-plandata title="Save everything the battery planner used for its last plan, as a JSON file">Download planning data</button>` : ""}
      <button data-revert ${this._dirty && !this._busy ? "" : "disabled"}>Revert</button>
      <button class="primary" data-save ${this._dirty && !this._busy ? "" : "disabled"}>${this._busy ? "Saving…" : "Save"}</button>`;
    r.querySelector("[data-save]").onclick = () => this._save();
    r.querySelector("[data-revert]").onclick = () => this._revert();
    const pd = r.querySelector("[data-plandata]");
    if (pd) pd.onclick = () => this._downloadPlanData();
  }

  async _downloadPlanData() {
    try {
      const data = await this._ws("plan_data");
      const text = JSON.stringify(data, null, 1);
      const stamp = (data.made_at || new Date().toISOString()).slice(0, 16).replace(/[:]/g, "");
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([text], { type: "application/json" }));
      a.download = `battery-plan-${stamp}.json`;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
      this._flash("Planning data downloaded");
      this._renderFooter();
    } catch (err) {
      this._flash(`Couldn't get the planning data: ${err.message || err}`, true);
      this._renderFooter();
    }
  }

  // ------------------------------------------------------------------ drag

  _onDown(e) {
    if (e.button !== 0 || !this._schedule) return;
    const pts = this._points;
    let i = e.target.dataset?.i !== undefined ? Number(e.target.dataset.i) : -1;
    // Double tap/click on a point deletes it. Detected here because the first
    // click redraws the chart, so the browser's own dblclick never arrives.
    const now = Date.now();
    if (i >= 0 && this._lastDown && this._lastDown.i === i && now - this._lastDown.t < 400) {
      this._lastDown = null;
      this._deletePoint(i);
      e.preventDefault();
      return;
    }
    this._lastDown = i >= 0 ? { i, t: now } : null;
    if (i < 0) {
      if (!e.target.classList?.contains("hit") && !e.target.classList?.contains("bar")) return;
      const { min, temp } = this._fromSvg(e);
      const m = clamp(round(min, this._config.time_step), 0, 1435);
      if (pts.some((p) => toMin(p.time) === m)) return;
      const p = { time: toHHMM(m), temp: clamp(round(temp, this._config.temp_step), 5, 30) };
      pts.push(p);
      pts.sort((a, b) => toMin(a.time) - toMin(b.time));
      i = pts.indexOf(p);
    }
    this._drag = { i, id: e.pointerId };
    this._svg.setPointerCapture(e.pointerId);
    e.preventDefault();
    this._renderSvg();
  }

  _onMove(e) {
    if (!this._drag || e.pointerId !== this._drag.id) return;
    const pts = this._points, i = this._drag.i;
    this._drag.moved = true;
    const { min, temp } = this._fromSvg(e);
    const tStep = e.shiftKey ? 5 : this._config.time_step;
    const vStep = e.shiftKey ? 0.1 : this._config.temp_step;
    const loM = i > 0 ? toMin(pts[i - 1].time) + 5 : 0;
    const hiM = i < pts.length - 1 ? toMin(pts[i + 1].time) - 5 : 1435;
    const m = clamp(round(min, tStep), loM, hiM);
    pts[i].time = toHHMM(m);
    pts[i].temp = Math.round(clamp(round(temp, vStep), 5, 30) * 10) / 10;
    this._renderSvg();
  }

  _onUp(e) {
    if (!this._drag || e.pointerId !== this._drag.id) return;
    const moved = this._drag.moved;
    this._drag = null;
    if (moved) this._lastDown = null;
    this._render();
  }
}

// Home Assistant's app script installs a scoped custom element registry
// polyfill that replaces window.customElements with a new registry. This file
// is injected alongside the app script and can finish loading first; a card
// defined before the swap lands in the old registry, which dashboards no
// longer read ("Custom element doesn't exist"). So wait for Home Assistant's
// root element to be defined, which happens after any swap, then register in
// whatever registry is current. Outside Home Assistant, register at once.
const CARD_TAG = "heat-load-schedule-card";
function registerCard() {
  if (!window.customElements.get(CARD_TAG)) window.customElements.define(CARD_TAG, HeatLoadScheduleCard);
}
if (window.customElements.get("home-assistant") || !document.querySelector("home-assistant")) {
  registerCard();
} else {
  window.customElements.whenDefined("home-assistant").then(registerCard);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "heat-load-schedule-card",
  name: "Heat Load Schedule",
  description: "Edit the Heat Load Predictor's target temperature curve and see forecast heating electricity.",
});
