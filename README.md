# JURA Wi-Fi Connect for Home Assistant

Local integration for JURA coffee machines with a **Wi-Fi Connect** dongle (the
one the J.O.E. app uses). It talks directly to the dongle on TCP port 51515. No
cloud, no JURA account.

It is built on the reverse-engineered
[`jura-connect`](https://github.com/makefu/jura-connect) library (MIT), which
bundles the machine profiles of the J.O.E. app. This project is not affiliated
with JURA.

## Status

| Area | State |
| --- | --- |
| JURA E8 (SD) with Wi-Fi Connect V2 | pairing, status, counters and maintenance values verified on a real machine (read-only) |
| Home Assistant side (config flow, entities, offline handling) | 127 automated tests pass on Home Assistant 2025.10 and 2026.10; the integration was also loaded in a Home Assistant test instance against the real E8 (read-only) |
| Busy machine (menu, brewing, maintenance program) | the machine then pushes progress frames instead of status frames; decoded with the library, covered by tests using one frame captured from a real E8 in its menu |
| Model detection | article number announced by UDP broadcast (same network as the dongle only), article number or model list as fallback; the UDP part is **not yet verified against a real machine** |
| Brewing | opt-in, **not yet tried on an E8 (SD)** through this integration |
| Other models | profiles for ~330 variants are bundled, untested |

Requires Home Assistant **2025.10 or newer**.

## Installation

### HACS (recommended)

1. In HACS open the three-dot menu → **Custom repositories**.
2. Add `https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration`
   with the category **Integration**.
3. Search for **JURA Wi-Fi Connect** in HACS, press **Download** and restart
   Home Assistant.
4. Continue with [Setup](#setup).

### Manual

Copy `custom_components/jura_wifi` into the `custom_components` folder of your
Home Assistant configuration and restart Home Assistant.

### Setup

1. The Wi-Fi Connect dongle has to be connected to your Wi-Fi already (set it up
   once with the J.O.E. app). Close the **J.O.E. app** afterwards: the dongle
   serves one connection at a time.
2. Switch the machine on and leave its display on the **start screen** (no menu
   open).
3. *Settings → Devices & services → Add integration → JURA Wi-Fi Connect*.
4. Enter the **IP address of the dongle** (give it a fixed lease in your router).
5. Confirm the **connection request with OK** on the machine within 60 seconds.
   Home Assistant stores the credentials issued by the machine; the request only
   appears once.
6. The **exact model is read from the machine** and kept in the
   [Model sensor](#entities). The machine announces its article number by UDP
   broadcast, which does not leave the local network. If Home Assistant is in
   another network than the dongle (VLAN, routed subnet) the setup asks for the
   **article number** instead: it is shown in the J.O.E. app next to the machine
   name and on the type plate on the underside of the machine (for example
   `15833` = E8 (SDS)). Leave it empty to pick the model from a list as a last
   resort.

## Entities

| Entity | Notes |
| --- | --- |
| Model (sensor) | diagnostic; the exact model, e.g. `E8 (SDS)`. Attributes: article number, machine profile (e.g. `EF1120`), firmware if known, and how the model was found (`discovery`, `article_number` or `manual`). The device page shows the same as model, model ID and firmware. |
| Status (sensor) | `offline`, `attention`, `rinsing`, `heating_up`, `brewing`, `maintenance`, `programming`, `busy`, `energy_saving`, `ready`. The raw alerts are available as attributes; while the machine is brewing, running a maintenance program or showing its programming menu the attributes `activity` and `activity_detail` (drink or program) are set instead. |
| Connection (binary sensor) | diagnostic; off while the machine does not answer |
| Problem sensors | water tank empty, grounds container full/missing, drip tray full/missing, beans empty, cleaning / descaling / filter / milk system rinse / milk system cleaning due |
| Total brews, one counter per drink | `total_increasing`, keep their last value while the machine is off |
| Cleaning need, descaling need, filter wear | percent of the interval used (filter wear is disabled by default) |
| Maintenance cycle counters | diagnostic, disabled by default |
| Brew buttons | only if enabled in the options, see below |

Entities are created from the machine profile, so only what your model supports
shows up.

## Options

*Settings → Devices & services → JURA Wi-Fi Connect → Configure*

- **Update interval** (30 to 900 s, default 60). One session with the dongle
  takes about 1.5 s.
- **Brew buttons** (off by default). Adds a button per drink that brews it with
  the *factory-default* recipe of the profile (not your personal settings). There
  is no remote abort: put a cup under the spout first. Drinks that are blocked
  by an active alert are refused.

## Things to know

- The dongle accepts **one connection at a time**. While Home Assistant polls
  (about 1.5 s per minute) the J.O.E. app may fail to connect; just retry. A
  session opened right after another one is rejected by the dongle, so the
  integration waits at least 10 s between sessions.
- With the machine **switched off** the dongle is unreachable: the status shows
  `offline`. **Energy-saving mode** is fine, the dongle stays reachable and a
  brew wakes the machine up.
- While the machine **brews, runs a maintenance program or shows its
  programming menu** it does not send its status, only what it is doing. The
  status then shows `brewing`, `maintenance` or `programming`, all other
  entities keep the values of the last full poll, and the brew buttons refuse to
  start another drink.
- If the dongle gets a new IP address, use *Reconfigure* on the integration.
- If the machine forgets the pairing (for example after a dongle reset) Home
  Assistant asks to pair again.
- Debug logging: `logger: logs: custom_components.jura_wifi: debug` and
  `jura_connect: debug`. Diagnostics downloads redact the address and
  credentials.

## Development

```
pip install -r requirements_test.txt   # current Home Assistant needs Python 3.14
pytest
ruff check . && ruff format --check .
```

The Home Assistant test plugin needs a POSIX system (Linux, macOS, WSL or a dev
container).

## Credits

Protocol and machine profiles by the `jura-connect` project, which in turn
derives them from the J.O.E. app. JURA and J.O.E. are trademarks of JURA
Elektroapparate AG.
