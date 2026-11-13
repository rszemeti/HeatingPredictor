# Heat Load Predictor

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/rszemeti/HeatingPredictor/actions/workflows/validate.yml/badge.svg)](https://github.com/rszemeti/HeatingPredictor/actions/workflows/validate.yml)
[![Tests](https://github.com/rszemeti/HeatingPredictor/actions/workflows/tests.yml/badge.svg)](https://github.com/rszemeti/HeatingPredictor/actions/workflows/tests.yml)

A Home Assistant integration for a house heated by a heat pump. It does three jobs; the first is always on and the other two are optional:

1. **Forecasts the heating.** For each half-hour over the next one to two days it works out how warm the house will be and how much electricity the heat pump will use, from your weather forecast and a daily target temperature curve that you draw on a dashboard card.
2. **Plans a home battery** *(optional)*. On a time-of-use tariff with solar, it decides when to charge from the grid, when to discharge to make room for solar, and whether the inverter should store or export solar, so you never buy at full price what the battery or the sun could have covered. An example automation applies the plan to a Solis inverter.
3. **Sets the heat pump's flow temperature** *(optional)*. It learns your house and radiators from the heat pump's own readings, works out the flow temperature each half-hour needs to follow your schedule, and, if you switch it on, writes it to the heat pump. In effect it replaces the heat pump's own weather compensation curve with one learned from your house, corrected by the room temperature.

![Schedule card](docs/card.png)

### What you need

| For | You need |
|---|---|
| The heating forecast | A `weather` entity with an hourly forecast (Met.no works). An indoor temperature sensor is optional but recommended. |
| The battery planner | A battery SOC sensor, the [Solcast](https://github.com/BJReplay/ha-solcast-solar) solar forecast and, for tariff-based windows, the [Octopus Energy](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy) integration. A whole-house power sensor lets it learn the rest of the house's use. To apply the plan: an inverter you can control from Home Assistant (the example is for Solis). |
| Flow temperature planning | The heat pump's flow and return temperatures, outdoor temperature and a room temperature sensor, ideally also heat output, electricity in and flow rate, all with history (`state_class: measurement`). For example from an ESPHome controller on the heat pump's RS-485 bus. |
| Flow control | A number entity that sets the heat pump's flow temperature, with the heat pump on a fixed flow temperature (its own weather compensation off). |

## Installation

### HACS (recommended)

1. In Home Assistant, open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/rszemeti/HeatingPredictor` with the type **Integration**.
3. Find **Heat Load Predictor** in HACS, download it, and restart Home Assistant.

Updates come through HACS the same way. Each update needs a Home Assistant restart. Nothing is lost by restarting: the schedule, the flow correction, the last weather forecast and the Flow control switch are all kept, and what's learned is relearned from history within a minute.

### Manual

Copy `custom_components/heat_load_predictor` into your `config/custom_components/` folder, then restart Home Assistant.

### Set up

1. **Settings → Devices & services → Add integration → Heat Load Predictor.** Choose your weather entity and, ideally, an indoor temperature sensor. The other figures describe a typical house with an 8 kW heat pump; see [Getting your numbers right](#getting-your-numbers-right). With flow temperature planning set up, the most important of them are learned for you.
2. Add the [schedule card](#the-schedule-card) to a dashboard and draw your target temperatures.
3. *(Optional)* **Configure → Battery planner** to plan a battery. See [Battery planner](#battery-planner-optional).
4. *(Optional)* **Configure → Flow temperature planning** to point it at your heat pump's readings. Watch the plan for a day or two on the [flow plan card](#the-flow-plan-card), then turn on **Flow control** when you're happy. See [Flow temperature planning and control](#flow-temperature-planning-and-control).

## Dashboard cards

Both cards are served by the integration and load automatically, so you don't need to add a dashboard resource.

### The schedule card

```yaml
type: custom:heat-load-schedule-card
title: Heating schedule
# entry_id: ...        # only needed if you set up more than one predictor
# time_step: 15        # minutes; dragging snaps to this (hold Shift for 5)
# temp_step: 0.5       # °C; dragging snaps to this (hold Shift for 0.1)
# y_min: 14            # temperature axis range (it widens to fit your points)
# y_max: 24
# show_battery: true   # battery windows and projected SOC (needs the battery planner)
# show_solar: true     # solar forecast bars, on their own scale
# show_flow: true      # planned flow and forecast outdoor temperature, in a strip under the chart
```

- **Edit points:** drag a point to move it, click empty space to add one, and double-click (or right-click) a point to delete it. The table under the chart gives exact times and temperatures.
- **Other days:** each day of the week has its own curve. The tabs run from today through the next six days. Pick a day with the tabs, then use **Copy to** to apply it to weekdays, the weekend or every day.
- **Curve shape:** **Steps** holds each temperature until the next point, and **Ramps** slopes between points.
- **Save:** saving reruns the forecast straight away. Saving needs an admin user.
- **Reading the chart:** the bars show forecast electricity per period, and the dashed line shows the modelled indoor temperature. Hovering a bar shows the outdoor temperature, the COP, the planned flow and any shortfall when the heat pump can't keep up. With the battery planner on, the chart also shows the charge and discharge windows, the projected SOC, and a strip under the chart with the planned storage mode: blue for Feed-in priority (export first), grey for Self-Use. With flow planning on, a second strip shows the planned flow temperature and the forecast outdoor temperature.
- **Download planning data:** saves a JSON file of everything the planners used. For the battery: the forecasts, solar slots, battery level, settings, cheap windows and what it decided. For the flow: the plan, what's been learned, the correction, the daily COP, and the last 48 hours of the heat pump's and the room's readings on a 5-minute grid. Useful for reporting a plan that looks wrong.

### The flow plan card

```yaml
type: custom:heat-load-flow-card
hours: 24          # how far ahead
```

The next hours of the flow plan: the flow temperature (grey where the heat pump idles) above, and the forecast outdoor temperature, modelled room and target below. Hover for each half-hour's flow, heat, temperatures and COP. The line above the chart gives the flow now, the next change, the range and the idle time. **Download data** saves the same file as the schedule card.

## Entities

All entities belong to one device, named after the integration entry (for example "Heat pump", giving `sensor.heat_pump_…`).

**Heating forecast**

| Entity | Meaning |
|---|---|
| `…_heating_electricity_this_period` | kWh for the current period. Its `forecast` attribute lists every period with `start`, `kwh`, `heat_kwh`, `t_out`, `t_in`, `t_target`, `cop`, `shortfall_k` and, with flow planning, `flow` and `heat_kw`. |
| `…_heating_electricity_next_24h` | kWh over the next 24 hours |
| `…_heating_electricity_rest_of_today` | kWh from now until midnight |
| `…_heating_electricity_tomorrow` | kWh for tomorrow. Its `complete` attribute is `false` if the weather forecast doesn't reach the end of the day. |
| `…_heating_peak_power_next_24h` | Highest average electrical draw in any period over the next 24 hours, in kW |
| `…_room_target` | The room temperature the schedule asks for now |

The `forecast` attribute is kept out of the recorder database because it's large.

**Battery planner**: `…_battery_plan`, `…_battery_lowest_soc_next_24h` and `…_battery_next_inverter_update`; see [Battery planner](#battery-planner-optional).

**Flow temperature**

| Entity | Meaning |
|---|---|
| `…_flow_target` | The planned flow temperature now (unknown while the plan has the heat pump idle). Attributes: the per-period `plan`, what's been `learned` and where each figure comes from, the outdoor-to-flow `water_law` at the current target, `holds_target_down_to`, the room reading and target in use, the `correction` and its parts, `control` (what Flow control last did), `learn_status` and `weather_note`. |
| `…_cop_yesterday` | Yesterday's COP (heat out ÷ electricity in). Attributes: the last 7 days and today so far, each with heat, electricity, COP and hours of data. Needs heat output and electricity in. |
| `switch.…_flow_control` | Off by default. On: the planned flow is written to the heat pump. See [Flow control](#flow-control-off-by-default). |

## Services

`heat_load_predictor.get_forecast` returns the per-period forecast and the totals as a service response. Set `refresh: true` to rerun the model first.

```yaml
action: heat_load_predictor.get_forecast
response_variable: heat
# heat.periods → [{start, kwh, heat_kwh, t_out, t_in, t_target, cop, shortfall_k}, ...]
```

`heat_load_predictor.set_schedule` replaces the schedule, for example to switch to a holiday profile from an automation:

```yaml
action: heat_load_predictor.set_schedule
data:
  schedule:
    interpolation: step        # or linear
    days:
      mon: [{time: "00:00", temp: 18}, {time: "06:30", temp: 20.5}, {time: "22:30", temp: 18}]
      # ... tue to sun, every day needs at least one point
```

### Example: battery charge needed for tomorrow's heating

```yaml
template:
  - sensor:
      - name: "Battery charge for heating"
        unit_of_measurement: kWh
        state: >
          {{ (states('sensor.heat_pump_heating_electricity_tomorrow') | float(0) * 1.1) | round(1) }}
```

The 1.1 allows for about 10% battery round-trip loss. For a period-by-period plan, read the `forecast` attribute or call `get_forecast`.

## Getting your numbers right

All of these can be changed later under **Settings → Devices & services → Heat Load Predictor → Configure**.

With flow temperature planning set up, the integration learns the heat loss, the radiators and the COP from the heat pump's own readings every 6 hours, and the flow plan uses those. The figures below still drive the heating forecast and the battery plan, so it's worth setting them close to what the flow section's `learned` attribute reports.

### Heat loss (W/K)

This is how many watts the house loses for each degree the inside is warmer than the outside. It matters more than any other setting. There are a few ways to estimate it:

1. **From a heat loss survey.** An installer's room-by-room calculation gives a total heat loss in watts at a design outdoor temperature. Divide it by (21 − design temperature). For example, 5,500 W at −3 °C gives 5,500 / 24 = 230 W/K.

2. **From annual fuel use and degree days.** Take your yearly gas or oil use for space heating, leaving out hot water (summer months show roughly what hot water uses). Multiply by boiler efficiency, typically 0.85 for a condensing boiler, 0.75–0.80 for an older one. Look up your area's heating degree days at base 15.5 °C (for example on degreedays.net). Then:

   ```
   heat loss (W/K) = heating kWh × efficiency × 1000 / (degree days × 24)
   ```

   For example: 12,000 kWh × 0.85 × 1000 / (2,100 × 24) = 202 W/K.

3. **From half-hourly smart meter data (most accurate).** Pair the daily gas use (× boiler efficiency) with each day's mean outdoor temperature, using only days heated by gas alone (no wood burner). Fit a straight line through the points. The slope in kWh per day per K, × 1000 / 24, is the heat loss in W/K. Where the line crosses zero is the house's **balance temperature**, which gives the internal gains (see below).

4. **Once your heat pump is running.** Set up flow temperature planning with the heat pump's heat output: the learned heat loss appears in the flow sensor's `learned` attribute and on the flow card. Copy it here.

### Internal gains (W)

This is free heat from people, appliances, cooking and sun, averaged over the day. Typical values are 300–800 W. If you know your balance temperature (method 3 above), use:

```
gains (W) ≈ heat loss (W/K) × (average indoor temperature − balance temperature)
```

### Thermal capacity (kWh/K)

This is how much heat the building stores per degree. It sets how fast the house cools during a setback and how long it takes to recover. To measure it, pick a cold night and switch the heating off for 6–8 hours. Note the indoor temperature at the start and end, and the average outdoor temperature. Then:

```
tau (hours) = hours off / ln( (T_in_start − T_out) / (T_in_end − T_out) )
capacity (kWh/K) = tau × heat loss (W/K) / 1000
```

For example: 8 hours off, indoor 20.5 → 19.0 °C, outdoor 3 °C. That gives tau = 8 / ln(17.5 / 16.0) = 89 hours, so capacity = 89 × 0.2 = 18 kWh/K. Lightweight houses are typically 4–8 kWh/K and heavy masonry 10–20 kWh/K. If you only ever run the heating steadily, with no setbacks, this setting hardly matters.

### COP curve

This is the heat pump's coefficient of performance at each outdoor temperature, written as `outdoor_temp:COP` pairs:

```
-10:2.3, -5:2.6, 0:3.0, 5:3.6, 10:4.3, 15:4.9
```

Take the figures from your heat pump's datasheet, at the flow temperature your weather compensation curve gives at each outdoor temperature. Use "integrated" values (including defrost) where the datasheet has them. Between points the value is interpolated, and beyond the end points it's held flat.

### Maximum heat output curve

This is the heat pump's maximum heat output in kW at each outdoor temperature, in the same format, for example `-10:6, -5:6.6, 2:7, 7:8`. When demand exceeds it, the forecast caps the output and reports a `shortfall_k`.

### Outdoor temperature now

If you set the heat pump's outdoor temperature sensor (under Flow temperature planning), the forecast starts from its last 30 minutes' average instead of the weather service's figure for now, and fades back to the forecast over three hours. So if it's 2 °C colder at the house than the forecast says, the next few hours (and the flow written now) allow for it. A reading more than 6 °C off the forecast is taken as a fault and ignored. The flow sensor's `outdoor_now` attribute shows the reading, the forecast and the difference.

### Other settings

- **Standby power (W):** the controller and circulation pump, added to every period.
- **Period length:** 5, 10, 15, 30 or 60 minutes, aligned to local midnight.
- **Horizon:** how far ahead to forecast (up to 168 hours), limited by how far your weather forecast reaches.
- **Pre-heat:** start heating early so the house reaches each step up in the schedule on time.
- **Indoor temperature sensor (optional):** starts each run from the measured temperature instead of assuming the house is on target.

## Battery planner (optional)

If you have a home battery and a time-of-use tariff, the integration can plan when to charge it so the heat pump and the rest of the house are covered between cheap windows. It can also plan when to discharge overnight to make room for a big solar day. Turn it on under **Configure → Battery planner**.

The planner steps through the forecast period by period: heat pump electricity plus a baseload for the rest of the house, minus the solar forecast, starting from the battery's current state of charge. For each window:

- **Cheap windows (charge):** by default, fill as far as the next day's solar allows, but never buy what the solar will supply: if the solar will fill the battery anyway, a window only buys what's needed to carry the house until the solar takes over, and a night window buys only the part the next day's solar can't top up (the next cheap window covers a shortfall). Cheap power is the cheapest there is, so the planner doesn't gamble on just scraping through to the next window; the only reason to stop short is to leave room for solar. If you prefer, **Cheap window charging → Only what the forecast needs** charges to the lowest SOC that still reaches the next cheap window at your chosen arrival SOC.
- **Discharge windows:** make room for solar the house can neither use nor export (beyond your export limit), plus a safety headroom. What the house and heat pump use overnight and before the solar arrives counts towards that room, so the planner exports only what the house won't use: the export stops at a level from which the house's own use brings the battery to the limit by the end of the window. If the house gets there on its own, or the target is above a threshold (80 % by default), the window is skipped. Making room never means buying at full price: the battery must still carry the house until the solar takes over (judged on the pessimistic forecast), so it reaches its minimum no earlier than sunup. On a huge day that can mean clipping some solar, which costs less than importing.
- **Solar room, but never at the cost of buying at full price:** a charge leaves space the solar will need. On a day the solar will fill the battery and spill anyway, holding the reserve would only buy power the solar replaces for free, so a cheap window then charges just enough for the battery to reach its minimum no earlier than the solar takes over the house (judged on the pessimistic forecast), or not at all if the battery already gets there.
- **Solar first in charge windows:** like a Solis timed charge, a charge window uses spare solar before the grid. A window the solar would fill on its own is switched off, and during any active charge window the storage mode is Self-Use, so the inverter never exports solar while charging from the grid. Feed-in priority is only recommended when there's spare solar to export. A charge window that overlaps an active discharge is switched off.
- **Storage mode:** the plan stores as much spare solar as it can, as early as it can, without clipping. Solar is only lost when the battery is full *and* the surplus is above the export limit, so the battery only needs room for the surplus above the limit still to come that day. Working backwards from the end of the day, the plan finds that room for each half-hour; working forwards from the battery's SOC now, it chooses Self-Use (store everything) unless that would leave less than that room, and Feed-in priority (export up to the limit, store only the excess) when it would. On ordinary days that means Self-Use all day; on a big day it's Self-Use in the morning, Feed-in priority through the peak and Self-Use again after. By default the room is sized on the expected forecast plus the solar headroom; the plan reruns every 15 minutes from the battery's real level and Solcast's latest forecast, so a sunnier day than expected switches to Feed-in priority in time. **Room for the solar peak sized on → Optimistic** sizes it on Solcast's `pv_estimate90` instead, which keeps more room but exports more on an ordinary day (and may buy it back in a cheap window). If a peak is too big for even an empty battery, `clipping_today_kwh` says how much will be lost. It assumes the overnight discharge window makes room for the next day; without one, two big days in a row can leave the battery full on the second morning.
- **House load learned from history:** set your whole-house power sensor (e.g. the inverter's house load) and the planner learns a half-hourly baseload for the rest of the house: measured load minus the heat model's heat pump use, averaged by time of day over the last 10 days and relearned every 6 hours. So it knows teatime cooking is heavier than 3 am. If the heat pump started recently, set **Heat pump in use since**: before that date the house load is taken as the baseload as it is, after it the modelled heating is subtracted. Until there's enough history, the fixed baseload setting is used. The plan sensor's `baseload_from` says which.
- **Sized cautiously:** charges are sized against Solcast's pessimistic forecast (`pv_estimate10`, which it expects to beat on about 9 days in 10) and with the house's demand raised by a margin (10 % by default). The projected SOC on the card uses the expected forecast, so on a typical day you'll see the battery reach each window above the reserve. That gap is the buffer. Each window is re-planned about 10 minutes before it opens, using the battery's actual level, so a disappointing morning is made up in the afternoon window. The example automation also rechecks a running charge window every 5 minutes and rewrites it if the plan has changed. For a bigger cushion through the evening peak, raise the arrival SOC.

**Cheap windows from your tariff:** if you use the [Octopus Energy](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy) integration, set its current-day and next-day rates events (`event.octopus_energy_electricity_…_current_day_rates` / `…_next_day_rates`), and the charge windows come from your actual rates. Each day's cheapest band (half-hours within 10 % of that day's lowest price, adjustable) becomes the charge windows, so a change to your tariff is followed automatically. Tomorrow's rates are published in the afternoon; until then, and if no rate entities are set, the fixed windows are used. Those default to Cosy Octopus (cheap 04:00-07:00, 13:00-16:00, 22:00-00:00). Solar comes from the [Solcast](https://github.com/BJReplay/ha-solcast-solar) integration's `detailedForecast` attribute.

### Sensors

| Entity | Meaning |
|---|---|
| `…_battery_plan` | State is a summary such as "Charge to 45% at 04:00". `storage_mode` is the recommended mode now and `self_use_from` the time it switches to Self-Use for the rest of today. `clipping_today_kwh` and `clipping_24h_kwh` are the solar expected to be lost above the export limit with the battery full: zero unless it can't be avoided. The `slots` attribute holds the next occurrence of each window, keyed `charge_1`, `charge_2`, … and `discharge_1`, each with `start`, `end`, `enabled`, `target_soc`, `projected_soc` and a plain-English `reason`. `windows` lists every planned window, and `soc_forecast` the projected SOC per period. |
| `…_battery_lowest_soc_next_24h` | Lowest projected SOC over the next 24 hours, which is useful for alerts. |
| `…_battery_next_inverter_update` | When the inverter next needs updating (10 minutes before the next window), as a timestamp. Use it as an automation's time trigger so inverter writes follow the windows wherever the tariff puts them. |

### On the card

With the planner on, the schedule card also shows the plan for each day. Charge windows are shaded green and discharge windows amber, labelled with their target (for example ▲ 45 %) or "off". The projected battery SOC is drawn on its own 0–100 % axis, and the solar forecast appears as faint bars on their own scale. Hover a window label to see why the plan chose it, or a bar to see that period's solar and projected battery level.

![Battery plan on the card](docs/card-battery.png)

### Applying the plan to your inverter

The integration only plans. One automation, [`examples/solis/battery_control.yaml`](examples/solis/battery_control.yaml), drives a Solis inverter from the plan using the Solis Modbus integration's timed charge and discharge slots (`number.solis8_timed_…`; change the names to match yours). It does three jobs:

- **Apply the plan.** At the plan's next-inverter-update time it writes each window due in the next 20 minutes into its slot: times, target and on/off. If the plan is unavailable, fixed Cosy-time triggers keep discharge off and charge to 80 %.
- **Stop discharging at the target.** During the discharge window it switches the slot off once the battery reaches its target, so the inverter doesn't import from the grid to hold that level.
- **Set the storage mode.** Every 30 minutes it sets the mode the plan recommends (`storage_mode` on the plan sensor), with grid charging allowed in both. If the plan is unavailable, it falls back to comparing the remaining solar forecast with the battery's free space. Check that the inverter's force-charge SOC is below your minimum SOC, or Self-Use will top the battery up from the grid whenever it reaches the floor.

By default (`use_single_slot: true`) every cheap window goes into charge slot 1, rewritten about 10 minutes before each window, and the on/off bits of charge slots 2–6 are cleared (`clear_unused_slots: true`) so whatever times those slots hold can't charge. Slots 2–6 themselves are never read or written, which avoids integrations whose mapping of the other slots is unreliable. Set `use_single_slot: false` to give each window its own slot. The automation keeps its last 30 traces, and every write is logged with the slot and the trigger that caused it.

Window times are written in the inverter's clock. If your inverter stays on GMT/UTC all year, set `inverter_uses_local_time: false` in the automation's variables, and summer windows are written an hour earlier to match.

The planner works in UTC throughout, so clock changes don't move anything. Times are shown in local time on the card and sensors.

For other inverters, read the same `slots` attribute and write the equivalent settings.

## Flow temperature planning and control

Under **Configure → Flow temperature planning**, point the integration at your heat pump's own readings (flow, return, flow rate, heat out, electricity in, room and outdoor temperatures). It then:

- **Plans a flow temperature for every period** from the heat model: the heat each period needs to follow your schedule, turned into a flow temperature with the radiator equation, output = K × (mean water temperature − room)^1.3, plus half the flow-to-return drop at your flow rate. Limited to your lowest and highest flow temperatures, in 0.5 °C steps. Periods where the heat pump can idle show as idle.
- **Learns your house** from the heat pump's history (5-minute statistics, hourly for older days), every 6 hours: the radiator constant K from steady running, the heat loss from a heat balance over whole days (including losses on the way to the house), and the COP as a function of outdoor and flow temperature.
- **Learns the curve straight from history** (needs only the flow, room and outdoor temperatures): over each few hours the room's rise or fall is fitted as heating from the flow, minus losses to outside, plus free gains. That gives the flow that holds any room temperature at any outdoor temperature, using what the house actually did, so it takes in the radiators, the pipework and the draughts without needing K or the heat loss right. Once there's about a day of history, this curve is used for the plan (`curve_from: history`), at each period's target temperature; until then the radiator equation above is (`curve_from: settings`). It runs as a weather curve, not on/off: a room a little above or below target is left to the correction, so the heat pump runs steadily. Only a real setback (the room 0.5 °C or more above a lowered target) idles it, and only a real warm-up (0.5 °C or more below) gets extra flow. The slope is the weak part: with the flow nearly fixed for days, and a room sensor that only reports in small, infrequent steps, it comes out far too flat. So the history's slope is used only if it's within ×0.67–×1.5 of the slope from the radiator and heat loss figures (learned from the heat meter, or the settings); otherwise that slope is kept and only the level comes from history. `learn_status` says which. The measured flow includes the pump's off-cycles, so the learned flow is the one that, run steadily, delivers the same heat.
- **Corrects towards the target:** while Flow control is on, the flow is raised 2 °C per °C the room is below target, and an integral adds 3 °C of flow per °C per hour while it stays there (limited to ±5 °C, ±6 °C in all). Both gains are settings under Flow temperature planning; changing them only reloads the integration, and the built-up correction is kept. There's a 0.05 °C dead band, so one step of a 0.1 °C room sensor counts. It only acts once the target has been steady for two hours, not during scheduled warm-ups or cool-downs (the plan allows for those), and it's zero while the plan is advisory. The flow sensor's `correction_parts` attribute shows the two parts.

`learn_status` says what was learned, or what's missing (for example a sensor without `state_class: measurement` has no history).

`sensor.heat_pump_flow_target` holds the flow temperature for now; see [Entities](#entities) for its attributes. The [flow plan card](#the-flow-plan-card) shows the next 24 hours.

### Daily COP

With heat output and electricity in set, `sensor.heat_pump_cop_yesterday` gives yesterday's COP (heat out ÷ electricity in, from the hourly statistics), with the last 7 days (heat, electricity, COP, hours of data) and today so far in its attributes. Updated every 30 minutes.

### Flow control (off by default)

Nothing is written to the heat pump until you turn on **Flow control** (`switch.heat_pump_flow_control`) and set **Heat pump flow target** in Flow temperature planning: the number entity that sets the flow temperature (for an ESPHome Samsung controller, `number.…_heat_pump_flow_target`). The heat pump must be on a fixed flow temperature, with its own water law off, for the target to take effect.

While it's on, every run (every 15 minutes):

- The flow planned for the current period is written, within your lowest and highest flow temperatures and the entity's own limits, to the entity's step.
- When the plan has the heat pump idle, the lowest flow is written, so it stops on its own.
- It writes only when the plan differs from the current target by at least 0.5 °C, and at most once per run.
- It writes nothing if the target entity is unavailable or there's no room temperature reading (the correction depends on it). The pump keeps its last target.
- Turning it off writes back the flow target the pump had when you turned it on.

The switch's `status` attribute (and the flow sensor's `control` attribute) says what it last did and why.

## When things go missing

- **Weather forecast unavailable** (Met.no has outages): the last good forecast is used for up to 12 hours, so the heating, battery and flow plans carry on. It's saved, so this works across a restart too. Beyond that, or with nothing saved, the heat pump's outdoor sensor (if set) is held flat for the whole forecast: crude, but the plans keep running. The flow sensor's `weather_note` says which is in use.
- **Restarts and pauses:** the flow correction's built-up part is saved and picked up again after a restart (if it's under 6 hours old). Switching Flow control off holds it; switching back on within 6 hours carries on from where it was, and after longer it starts afresh. The Flow control switch remembers its state; what's learned is relearned from history within a minute.

## How the model works

The house is treated as one thermal mass:

```
C · dT_in/dt = P_heat + G − H · (T_in − T_out)
```

Within each period the outdoor temperature and heat output are held constant, which gives an exact exponential solution. For each period the model picks the heat output that brings the indoor temperature to the target by the end of the period. That output is clamped between zero, when the house coasts down after a setback, and the heat pump's maximum at that outdoor temperature. Electricity is heat ÷ COP, plus standby. With pre-heat on, a backward pass first raises earlier targets wherever the heat pump couldn't otherwise reach a later step up in time.

### Limits

- **The heating forecast's COP depends only on outdoor temperature.** It doesn't change with part load or with the higher flow temperatures used during recovery, so recovery periods are slightly optimistic. (Flow planning learns COP against flow temperature too, and shows it in the plan, but the electricity forecast and battery plan still use the COP curve.)
- **The flow curve doesn't vary by time of day.** Mornings, with doors opening and no sun, can need more than the curve gives; the room-temperature correction makes that up while Flow control is on.
- **Solar gain** is averaged into the internal gains rather than taken from the forecast. Sunny winter days will come in a little under the forecast.
- **Other heat sources** such as wood burners and electric heaters aren't modelled.
- **Hot water** isn't included. This forecasts space heating only.

## Development

```
pip install -r requirements_test.txt
pytest
```

`model.py` (the heating forecast), `planner.py` (the battery plan) and `flowlaw.py` (flow temperatures and learning) have no Home Assistant imports and are tested on their own, including replays of real planning-data downloads in `tests/data`. The integration tests run against a real Home Assistant core and recorder and cover the sensors, switch, services, websocket API, config and options flows, learning from statistics, flow control, persistence across restarts and weather outages.

Issues and pull requests are welcome.

## Licence

MIT
