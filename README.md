<p align="center">
  <img src="https://raw.githubusercontent.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/main/custom_components/jura_wifi/brand/icon@2x.png" alt="JURA Wi-Fi Connect logo" width="128">
</p>

# JURA Wi-Fi Connect for Home Assistant

Local integration for JURA coffee machines with a **Wi-Fi Connect** dongle (the
one the J.O.E. app uses). It talks directly to the dongle on TCP port 51515. No
cloud, no JURA account.

It reads the state, the counters and the maintenance needs of the machine, and
it can brew your drinks and start the maintenance programs. Optionally it also
shows and changes the settings of the machine and locks its front panel.

It is built on the reverse-engineered
[`jura-connect`](https://github.com/makefu/jura-connect) library (MIT), which
bundles the machine profiles of the J.O.E. app. This project is not affiliated
with JURA. The logo is an original drawing and not related to any JURA logo.

## Status

| Area | State |
| --- | --- |
| JURA E8 (SDS) with Wi-Fi Connect V2 | pairing, status, counters, maintenance values and the stored drink recipes verified on a real machine (read-only), also while the machine is in energy-saving mode |
| Home Assistant side (config flow, entities, offline handling, cache of the last values) | automated tests pass on Home Assistant 2025.10 and 2026.10; the integration was also loaded in a Home Assistant against the real E8 |
| Brew buttons, maintenance programs, cancel | the commands are tested end to end against the dongle simulator of the library (real protocol over TCP). On a real E8 (SDS) the **brew button (Coffee)**, the **milk system rinse** and the **coffee system rinse** were run through this integration on 2026-10-09: each press was followed by machine activity and counter changes. The other drinks use the same command. Cleaning, descaling, filter change, milk system cleaning and cancel were **not yet run on a real machine** |
| Machine settings (read and write), front panel lock, brew with parameters, polling every 15 s while the machine is active (all new in 0.5.0) | covered by the library simulator only (real protocol over TCP) and **not yet run on a real machine**. The settings and the lock are off by default |
| Busy machine (menu, brewing, maintenance program) | the machine then pushes progress frames instead of status frames; covered by tests using one frame captured from a real E8 in its menu |
| Model detection, and filling in entries of older versions | article number, firmware and serial number announced by UDP broadcast (same network as the dongle only), article number or model list as fallback; the UDP part is **not yet verified against a real machine** |
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

> [!IMPORTANT]
> **Leave the settings menu on the machine before you pair.** The display has to
> show the start screen. While a menu is open the machine cannot show the
> connection request and refuses the connection, which Home Assistant reports as
> "The machine refused the connection request" (or "…is in its settings menu").

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
   [Model sensor](#entities). The machine announces its article number, its
   firmware and its serial number by UDP broadcast, which does not leave the
   local network. If Home Assistant is in another network than the dongle (VLAN,
   routed subnet) the setup asks for the **article number** instead: it is shown
   in the J.O.E. app next to the machine name and on the type plate on the
   underside of the machine (for example `15833` = E8 (SDS)). Leave it empty to
   pick the model from a list as a last resort.
7. A last step asks for the **area** of the machine (optional) and for the
   [options](#options): update interval, brew buttons, maintenance buttons and
   machine settings. Everything except the area can be changed later. The
   area is applied once, when the device of the machine is created, so that all
   of its entities get the same prefix in their entity IDs (Home Assistant 2026
   puts the name of the area in front). It is never applied again: whatever you
   change on the device afterwards stays.

## Entities

| Entity | Notes |
| --- | --- |
| Model (sensor) | diagnostic; the exact model, e.g. `E8 (SDS)`. Attributes: article number, machine profile (e.g. `EF1120`), firmware if known, and how the model was found (`discovery`, `article_number` or `manual`). The device page shows the same as model, model ID, firmware and serial number. |
| Status (sensor) | `offline`, `attention`, `rinsing`, `heating_up`, `brewing`, `maintenance`, `programming`, `busy`, `switching_off`, `energy_saving`, `ready`. The raw alerts are available as attributes; while the machine is brewing, running a maintenance program or showing its programming menu the attributes `activity` and `activity_detail` (drink or program) are set instead. |
| Last seen (sensor) | diagnostic; the time of the last poll in which the machine answered (online or busy). It is kept over restarts, so it also tells when a machine that is switched off was last on. |
| Connection (binary sensor) | diagnostic; off while the machine does not answer |
| Problem sensors | water tank empty, grounds container full/missing, drip tray full/missing, beans empty, cleaning / descaling / filter / milk system rinse / milk system cleaning due, and the alerts that block the machine: system fill needed, tap open, front cover open, machine error |
| Hardware state sensors | disabled by default: outlet missing, rear cover missing, water tank removal requested, ventilation closed, powder cover open. Diagnostic and disabled by default: filter detected, keys locked, remote screen active |
| Maintenance recommended (binary sensors) | cleaning, descaling and filter change: on once the maintenance percent reaches the threshold of the machine profile (80 % on the E8), as the J.O.E. app recommends it. The machine asks for the maintenance itself later, which the *due* sensors show. Only for the maintenance the profile gives a threshold for |
| Total brews, one counter per product | `total_increasing`, keep their last value while the machine is off. A counter exists for every product the machine reports, including the double drinks (*2x Espresso*, *2x Coffee*) that some profiles do not offer as a drink; the counter of the powder product is disabled by default. The values are read from the machine, nothing is counted in Home Assistant. The machine counts a double drink twice in its total, so the single counters add up to less than the total |
| Cleaning need, descaling need, filter wear | percent of the interval used (filter wear is disabled by default). An indicator that the machine does not report, such as the filter on an E8 without one, is *unavailable* |
| Maintenance cycle counters | diagnostic, disabled by default |
| Brew buttons | one per drink, only if enabled in the options, see [Controls](#controls) |
| Maintenance buttons | one per maintenance program of the machine and one for the coffee system rinse, only if enabled in the options; in the configuration section of the device |
| Cancel button | only together with one of the two sets above |
| Machine settings (number, select, switch) | one per setting the machine profile declares, only if *Machine settings* is enabled in the options; in the configuration section of the device, see [Machine settings](#machine-settings) |
| Front panel lock (switch) | only if *Machine settings* is enabled and the profile declares the commands, see [Front panel lock](#front-panel-lock) |

Entities are created from the machine profile, so only what your model supports
shows up.

## Controls

The buttons are off by default, because pressing one makes the machine do
something. Switch them on under *Settings → Devices & services → JURA Wi-Fi
Connect → Configure* (or at the setup):

- **Brew buttons** adds a button per drink, and with them the action
  [Brew with options](#brew-with-options).
- **Maintenance buttons** adds a button per maintenance program.

Both also add a **cancel button**.

### Brewing

A press brews the drink **with the recipe that is stored on the machine**, the
one you get when you select the drink at its display, including what you have
adjusted there (strength, amount, foam time and so on). The recipe is read from
the machine at the moment of the press. A machine that does not hand out its
recipes brews the factory recipe of its profile instead.

- Put a **cup under the spout** first. The machine cannot tell whether there is
  one. For milk drinks the milk system has to be connected.
- Drinks that are blocked by an active alert (for example an empty water tank)
  are refused, and so is a second drink while the machine is busy.
- A machine in energy-saving mode is woken up by the command. If it ignores the
  first one while it wakes, the command is sent a second time; if it still does
  not start, press the button again.
- Use `button.press` in scripts and automations, or the action below to change
  single parameters of the recipe.

### Brew with options

The action **Brew with options** (`jura_wifi.brew`) brews a drink with single
parameters of its recipe changed, for this drink only. Its target is a **brew
button** of this integration; the drink is the one of the button. It needs the
*Brew buttons* option.

| Field | Meaning |
| --- | --- |
| `strength` | coffee strength, one of the levels the machine offers for the drink |
| `water_amount` | amount of water in ml |
| `temperature` | `low`, `normal` or `high` |
| `milk_foam_time` | seconds of milk foam |
| `milk_break` | seconds of pause between the milk and the coffee |
| `bypass` | ml of water that bypass the coffee, as for an Americano |

All fields are optional. **Whatever is not given stays as stored on the
machine.** Only the parameters that the profile of the machine defines for the
drink are accepted, and only within the range, the step or the values it
declares (an espresso takes 15 to 80 ml in steps of 5, for example); anything
else is refused with a message that says what is allowed, before anything is
sent to the machine. The guards are those of the buttons: the machine has to be
online and idle and the drink must not be blocked by an alert. Double drinks and
preselections are not supported.

```yaml
action: jura_wifi.brew
target:
  entity_id: button.jura_e8_sds_brew_cappuccino
data:
  strength: 6
  water_amount: 60
  temperature: high
  milk_foam_time: 25
```

The entity ID depends on the name of your device. The action has not been run on
a real machine yet, see [Status](#status).

### Maintenance programs

A press **starts** the program (cleaning, descaling, filter change, milk system
rinse, milk system cleaning, as far as your model has them). The machine then
leads you through it **on its display** and waits there for what it needs: empty
the drip tray, insert the tablet, confirm. Home Assistant does not confirm
anything on its own.

Cleaning and descaling take a long time and need the tablet or the descaler to
be at hand, and a program should be run to its end. They only make sense when
you are at the machine. The status sensor shows `maintenance` while one runs.

The **coffee system rinse** (*Rinse coffee system*) is the simple one: it needs
no tablet and no confirmation. It starts at once and runs for about a minute with
water coming out of the coffee spout, so put a cup or the drip tray under it
first. The machine counts it in *Coffee system rinses performed*. The machine
profile of the E8 does not list this program (only the GIGA 6 profiles do), but a
real E8 (SDS) runs it when it gets the command of the J.O.E. app; the button is
therefore offered for every machine. A machine that does not know the command
reports a failed start.

### Cancel

The cancel button sends the cancel command of the J.O.E. app: it stops the drink
that is running or the current step of a maintenance program. It is available
while the machine is busy, which is when it is needed.

## Machine settings

The option **Machine settings** (off by default) adds entities for the settings
that the profile of your machine declares. The E8 (SDS) declares six: water
hardness (1 to 30), switch-off time (15 minutes to 9 hours), units (ml or oz),
language (11 languages), brewing mode (ask, classic, light, sweet) and quality
assistant (on or off). They are numbers, selects and a switch in the
configuration section of the device; a machine gets only the settings its
profile declares.

- The values are **read from the machine** at the first poll, then about every
  10 minutes while the machine is on, and after every change. They are not read
  with every poll. A change made at the display of the machine or in the J.O.E.
  app shows up at the next read.
- A change is **written** to the machine with its checksummed setting command,
  and the value that the machine stores is read back and shown. It needs the
  machine to be **on and idle**: while it is brewing, running a program or
  showing its menu Home Assistant refuses the change and says why.
- The values are not kept over a restart. While the machine is off the entities
  are unavailable.
- Reading and writing the settings has only been run against the simulator of
  the library, not yet on a real machine, see [Status](#status). Try a change
  that is easy to undo first and look at the display of the machine.

### Front panel lock

With **Machine settings** a switch **Front panel lock** locks the keys of the
machine and releases them again. It is only offered if the profile of the
machine declares the commands for it (the E8 does, as *Remote Screen* and
*Release Keys*) and the alerts that report the state. The state is what the
machine reports with its alerts *keys locked* and *remote screen*; right after a
command the switch shows what was asked for, until the check that follows has
the answer of the machine. It needs the machine to be on and idle as well, and
has not been run on a real machine yet.

## Examples

### Brews per day

The total of the machine (and every counter of a drink) is a
`total_increasing` sensor, which the
[Utility Meter](https://www.home-assistant.io/integrations/utility_meter/) takes
as its source. In `configuration.yaml`, or as a helper in the user interface:

```yaml
utility_meter:
  jura_brews_per_day:
    name: Brews per day
    source: sensor.jura_e8_sds_total_brews
    cycle: daily
```

The machine counts a double drink twice in its total. The counters of single
drinks work the same way, for example `sensor.jura_e8_sds_espresso_count`.

### Notifications

Replace `notify.mobile_app_your_phone` by your notification service and the
entity IDs by those of your device.

```yaml
automation:
  - alias: Coffee machine - water tank empty
    triggers:
      - trigger: state
        entity_id: binary_sensor.jura_e8_sds_water_tank_empty
        to: "on"
        for: "00:01:00"
    actions:
      - action: notify.mobile_app_your_phone
        data:
          title: Coffee machine
          message: The water tank is empty.

  - alias: Coffee machine - grounds container full
    triggers:
      - trigger: state
        entity_id: binary_sensor.jura_e8_sds_grounds_container_full
        to: "on"
        for: "00:01:00"
    actions:
      - action: notify.mobile_app_your_phone
        data:
          title: Coffee machine
          message: The grounds container is full.

  - alias: Coffee machine - cleaning due
    triggers:
      - trigger: state
        entity_id: binary_sensor.jura_e8_sds_cleaning_recommended
        to: "on"
    actions:
      - action: notify.mobile_app_your_phone
        data:
          title: Coffee machine
          message: The machine should be cleaned soon.
```

The *recommended* sensor turns on first (at 80 % on the E8); the *due* sensor
(`binary_sensor.jura_e8_sds_cleaning_due`) follows when the machine itself asks
for the cleaning. The same works for descaling and the filter change.

## Options

*Settings → Devices & services → JURA Wi-Fi Connect → Configure*

- **Update interval** (30 to 900 s, default 60). One session with the dongle
  takes about 1.5 s. While the machine reports an activity (brewing, a
  maintenance program, its menu, busy) it is polled every 15 s instead, so that
  the status does not linger after the drink is done; the pause between two
  sessions still applies. A shorter interval than 15 s is kept as it is.
- **Brew buttons**, **Maintenance buttons** and **Machine settings** (all off by
  default), see [Controls](#controls) and [Machine settings](#machine-settings).
  Switching one off removes its entities again. The same options are asked once
  at the setup.

## Things to know

- The dongle accepts **one connection at a time**. While Home Assistant polls
  (about 1.5 s per minute, more often while the machine is active) the J.O.E.
  app may fail to connect; just retry. A session opened right after another one
  is rejected by the dongle, so the integration waits at least 10 s between
  sessions.
- With the machine **switched off** the dongle is unreachable: the status shows
  `offline`. **Energy-saving mode** is fine, the dongle stays reachable and a
  brew wakes the machine up. When the machine **switches itself off**, the E8
  first counts down for about a quarter of an hour (the status is `switching_off`)
  before the dongle disappears.
- Home Assistant **remembers the last counters and maintenance values** that the
  machine reported and shows them after a restart while the machine is switched
  off, instead of *unknown*. The machine stays the only source: nothing is
  counted in Home Assistant, and the entities still show the machine as offline
  (status `offline`, connection off, alerts unavailable). The values are written
  a few minutes after a poll and when the integration is unloaded, and are
  removed with the integration.
- Only a wrong hash or a wrong PIN, which means the dongle does not know the
  pairing any more, asks you to pair again. A machine that aborts or rejects a
  connection for another reason is treated like an unreachable machine and
  polled again.
- The log (level info) says once when the machine becomes unreachable and once
  when it is reachable again.
- While the machine **brews, runs a maintenance program or shows its
  programming menu** it does not send its status, only what it is doing. The
  status then shows `brewing`, `maintenance` or `programming`, all other
  entities keep the values of the last full poll, and the buttons refuse to
  start something else (the cancel button still works).
- **Pairing is refused?** Leave the settings menu on the machine, close the
  J.O.E. app and try again. Home Assistant tells you when it sees that the
  machine is in its menu or busy; the reason of a refusal is also in the log.
- If the dongle gets a new IP address, use *Reconfigure* on the integration.
- **Entries from older versions** (set up with 0.1.0) lack the article number,
  the firmware and the serial number. Where the UDP discovery reaches the dongle
  they are filled in as soon as the machine is reachable, which also shows
  firmware, model ID and serial number on the device. Where it does not,
  *Reconfigure* has an optional **article number** field. It has to belong to the
  model that is set up: the machine type decides which profile is used and is
  never changed silently. To change it, set the machine up again.
- The **serial number** of the machine identifies the entry where it is known
  (instead of the IP address) and is shown on the device. The identifiers of the
  device and of all entities stay as they were, so nothing is lost on an update.
- If the machine forgets the pairing (for example after a dongle reset) Home
  Assistant asks to pair again.
- The **logo** needs Home Assistant 2026.3 or newer; older versions show the
  grey "icon not available" placeholder for every custom integration. The
  browser keeps brand images for up to a day: if the placeholder is still there
  after an update, reload the page once or twice (the image is refreshed in the
  background) or clear the site data of Home Assistant in the browser.
- **HACS itself shows the grey placeholder** instead of the logo, in its list and
  in the update entry (Settings → Updates). HACS 2.0.5 and older still ask the old
  brands server for the icon, which no longer takes custom integrations since Home
  Assistant 2026.3 ([hacs/integration#5171](https://github.com/hacs/integration/issues/5171)).
  The logo is shipped correctly: the pages of Home Assistant (integration, device)
  show it.
- Debug logging: `logger: logs: custom_components.jura_wifi: debug` and
  `jura_connect: debug`. Diagnostics downloads contain the state of the
  coordinator, the versions of the integration and the library and the profile
  of the machine, and redact the address, the credentials, the serial number and
  the area.

## Development

```
pip install -r requirements_test.txt   # current Home Assistant needs Python 3.14
pytest
ruff check . && ruff format --check .
```

The Home Assistant test plugin needs a POSIX system (Linux, macOS, WSL or a dev
container). The tests include end-to-end runs against the dongle simulator of
the `jura-connect` library: real protocol over TCP on the loopback address.

The brand images in `custom_components/jura_wifi/brand` are drawn by
`scripts/make_brand_images.py` (needs Pillow). The README points to the icon by
its absolute address and avoids `<picture>`, because HACS shows relative images
broken and `<picture>` as text.

The workflows in `.github/workflows` check the repository with
[hassfest](https://developers.home-assistant.io/blog/2020/04/16/hassfest/) and the
[HACS action](https://hacs.xyz/docs/publish/action) on every push and every
night. HACS requires both to pass, without ignored checks, before it takes an
integration into its default list, so keep them green.

## Releases

The versions follow [Semantic Versioning](https://semver.org/) and every change
is listed in the [changelog](https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/blob/main/CHANGELOG.md). HACS offers the latest
[GitHub release](https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/releases),
so a change reaches the users when it is released, not when it is pushed.

To make a release:

1. Move the entries under *Unreleased* in `CHANGELOG.md` into a new section
   `## [x.y.z] - date` and add its link at the bottom.
2. Set the same version in `custom_components/jura_wifi/manifest.json` and in
   `pyproject.toml`. A test fails if these and the changelog differ.
3. Commit and push to `main`.
4. Create the release from the text of the changelog:

   ```
   python scripts/release_notes.py x.y.z | gh release create vx.y.z --target main --title vx.y.z --notes-file -
   ```

   Create the release only after the workflows of that commit have passed.

## License

[MIT](https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/blob/main/LICENSE).

## Credits

Protocol and machine profiles by the `jura-connect` project, which in turn
derives them from the J.O.E. app. JURA and J.O.E. are trademarks of JURA
Elektroapparate AG.
