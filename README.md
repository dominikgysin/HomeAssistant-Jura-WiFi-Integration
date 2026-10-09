<p align="center">
  <img src="https://raw.githubusercontent.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/main/custom_components/jura_wifi/brand/icon@2x.png" alt="JURA Wi-Fi Connect logo" width="128">
</p>

# JURA Wi-Fi Connect for Home Assistant

Local integration for JURA coffee machines with a **Wi-Fi Connect** dongle (the
one the J.O.E. app uses). It talks directly to the dongle on TCP port 51515. No
cloud, no JURA account.

It reads the state, the counters and the maintenance needs of the machine, and
it can brew your drinks and start the maintenance programs.

It is built on the reverse-engineered
[`jura-connect`](https://github.com/makefu/jura-connect) library (MIT), which
bundles the machine profiles of the J.O.E. app. This project is not affiliated
with JURA. The logo is an original drawing and not related to any JURA logo.

## Status

| Area | State |
| --- | --- |
| JURA E8 (SDS) with Wi-Fi Connect V2 | pairing, status, counters, maintenance values and the stored drink recipes verified on a real machine (read-only), also while the machine is in energy-saving mode |
| Home Assistant side (config flow, entities, offline handling) | automated tests pass on Home Assistant 2025.10 and 2026.10; the integration was also loaded in a Home Assistant test instance against the real E8 (read-only) |
| Brewing, maintenance programs, cancel | the commands are tested end to end against the dongle simulator of the library (real protocol over TCP). The **coffee system rinse** was also run on a real E8 (SDS) with the client code of this integration: the machine acknowledges the command, rinses for about a minute and counts the rinse. The other commands are **not yet run on a real machine through this integration** |
| Busy machine (menu, brewing, maintenance program) | the machine then pushes progress frames instead of status frames; covered by tests using one frame captured from a real E8 in its menu |
| Model detection | article number announced by UDP broadcast (same network as the dongle only), article number or model list as fallback; the UDP part is **not yet verified against a real machine** |
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
| Brew buttons | one per drink, only if enabled in the options, see [Controls](#controls) |
| Maintenance buttons | one per maintenance program of the machine and one for the coffee system rinse, only if enabled in the options; in the configuration section of the device |
| Cancel button | only together with one of the two sets above |

Entities are created from the machine profile, so only what your model supports
shows up.

## Controls

The buttons are off by default, because pressing one makes the machine do
something. Switch them on under *Settings → Devices & services → JURA Wi-Fi
Connect → Configure*:

- **Brew buttons** adds a button per drink.
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
- Use `button.press` in scripts and automations.

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

## Options

*Settings → Devices & services → JURA Wi-Fi Connect → Configure*

- **Update interval** (30 to 900 s, default 60). One session with the dongle
  takes about 1.5 s.
- **Brew buttons** and **Maintenance buttons** (both off by default), see
  [Controls](#controls). Switching one off removes its buttons again.

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
  entities keep the values of the last full poll, and the buttons refuse to
  start something else (the cancel button still works).
- **Pairing is refused?** Leave the settings menu on the machine, close the
  J.O.E. app and try again. Home Assistant tells you when it sees that the
  machine is in its menu or busy; the reason of a refusal is also in the log.
- If the dongle gets a new IP address, use *Reconfigure* on the integration.
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
  `jura_connect: debug`. Diagnostics downloads redact the address and
  credentials.

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
