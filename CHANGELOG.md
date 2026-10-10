# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), the versions follow
[Semantic Versioning](https://semver.org/). While the major version is 0, a minor
version may change behaviour.

## [Unreleased]

## [0.5.2] - 2026-10-10

### Fixed

- The **serial number** is now the one on the type plate of the machine: the
  production date (YYYYMMDD) followed by the machine number with six digits, both
  from the UDP discovery reply. 0.5.0 and 0.5.1 took another 16-bit field of the reply
  for it, which the library calls `serial_number` but which is not the number on the
  type plate. The composition was checked against the type plate of one E8.
- Entries of 0.5.0 and 0.5.1 are corrected without anything to do. The update drops
  the wrong number from the entry and from the device, and the entry is known by its
  IP address again, as before 0.5.0. As soon as the dongle answers the discovery
  (while the machine is on), the entry, its ID and the device get the serial number of
  the type plate. The identifiers of the device and the IDs of all entities do not
  depend on it and stay as they were, so no entity and no history is lost. The config
  entry moves to version 1.2, which 0.5.1 can still load.
- When the reply of the machine with the counters cannot be read, the total and the
  product counters keep their last values instead of becoming unknown, and the cache
  keeps them as well. The reason is logged at debug level. This was never seen on the
  real E8.

### Changed

- The **front panel lock** can be released whenever the machine answers, also while it
  brews or runs a program, for example a drink started in the J.O.E. app while the keys
  are locked. Locking still needs an idle machine. A busy machine does not report its
  alerts, so the switch shows the release until the machine is idle again. Like the
  lock itself, this has only been run against the simulator of the library.
- README: the status table tells what was seen working on the real E8 with 0.5.0 and
  0.5.1 and what has only been run against the simulator. The note on switching off
  no longer says that the E8 always counts down first: it was also seen going from
  energy saving straight to offline. New notes recommend automations on the counters
  rather than on the status `brewing` (a short drink can start and end between two
  polls), and explain that the last pressed time of a button is lost when Home
  Assistant restarts while the machine is off.

## [0.5.1] - 2026-10-10

### Removed

- The sensor **Last seen** that 0.5.0 added. It wrote a new state at every poll: on a
  real E8 that were 62 state changes an hour, about 1,500 a day, one per poll. It was
  the only entity that wrote continuously (every other entity changes only when
  something about the machine changes), and it filled the logbook and the activity
  card of the device. The entity is removed from the entity registry when the entry is
  set up, whether it is enabled or disabled, and nothing else is removed. A dashboard
  card or an automation that used it has to use the attribute below.

### Added

- The status sensor has the attribute `last_seen` while the machine is offline: the
  time of the last poll in which the machine answered (ISO 8601, UTC). It does not
  change while the machine stays offline, so it only changes together with the status.
  It is restored from the cache when Home Assistant restarts while the machine is
  switched off. While the machine is online or busy the attribute is not there. The
  time is still part of the diagnostics.
- README: a note that enabling or disabling an entity (Home Assistant reloads the
  integration about 30 seconds later, core behaviour) and saving the options reload
  the integration, and that all entities are unavailable for a few seconds meanwhile
  (2 to 14 s measured). The entity table describes the attribute.

## [0.5.0] - 2026-10-09

### Added

- **Machine settings** (option *Machine settings*, off by default, also asked in the
  setup). The settings that the machine profile declares become entities in the
  configuration section of the device. On the E8 these are the water hardness
  (number), the switch-off time, the units, the language and the brewing mode
  (selects) and the quality assistant (switch). The values are read from the machine
  at the first poll, every ten minutes and after each change, and written with the
  checksummed setting write of the library. A change needs the machine to be on and
  idle, and the value is read back from the machine. A machine that knows steps
  instead of 1 to 30 for the water hardness (ENA 4, E4, D4) gets a select for it, and
  a setting without a translation is named the way its profile names it.
- **Front panel lock** (switch, same option): locks the keys and the display of the
  machine with the lock and release commands of the J.O.E. app (`@TS:01`, `@TS:00`).
  The state follows the alerts `LockedKeys` and `RemoteScreen`. Only machines whose
  profile declares both commands get it.
- **Action `jura_wifi.brew`** (needs the brew buttons): brews the drink of a brew
  button with single parameters of its recipe changed for this drink only (strength,
  water amount, temperature, milk foam time, milk break, bypass). Only the parameters
  that the profile defines for the drink are accepted, within its range, step or
  items; anything else is refused with a translated message. What is not given stays
  as stored on the machine. The same guards as for the buttons apply: the machine has
  to be online and idle and the drink must not be blocked.
- The setup asks for the **area** (optional), the update interval, the brew buttons,
  the maintenance buttons and the machine settings before it creates the entry. The
  area is applied when the device is created, so that all of its entities get the
  same entity ID prefix, and is never applied again, so a later change of the area
  stays.
- Sensor **Last seen** (diagnostic): the time of the last poll that the machine
  answered. It survives a restart of Home Assistant.
- The values that the machine reported last (counters and maintenance values) are
  kept and shown after a restart of Home Assistant while the machine is switched off,
  instead of `unknown`. The entities still show the machine as offline, and nothing
  is counted by the integration: the machine stays the only source of every value.
- More binary sensors, for the alerts that the profile declares: system fill needed,
  tap open, front cover open and machine error (the alerts that block the machine);
  outlet missing, rear cover missing, water tank removal requested, ventilation
  closed, powder cover open, filter detected, keys locked and remote screen active
  (disabled by default). Plus *Cleaning*, *Descaling* and *Filter change recommended*,
  which turn on when the maintenance percent reaches the threshold that the profile
  declares (80 on the E8), like the J.O.E. app recommends it.
- Status **switching off**. The E8 counts down for about a quarter of an hour before
  it switches off and the status showed `busy` meanwhile (alert
  `switch_off_delay_active`).
- The reconfiguration offers an optional article number, for a machine whose dongle
  cannot be reached by UDP. It has to belong to the model that is set up: the machine
  type is never changed.
- Diagnostics: the state of the coordinator (last update, interval, failures in a
  row, online or offline since), the versions of the integration and of the library,
  the profile code and the settings read from the machine. The address, the
  credentials and now the serial number stay redacted.
- The serial number of the machine, as the discovery reports it, is shown on the
  device.

### Changed

- The poll interval is 15 seconds while the machine reports an activity (brewing, a
  maintenance program, its menu), and the configured interval otherwise. `brewing`
  and `maintenance` used to linger up to about 80 seconds after the drink was done.
  The pause of 10 seconds between two sessions with the dongle still applies.
- A config entry is identified by the serial number of the machine instead of the
  address of the dongle when the serial number is known. Entries of earlier versions
  get it when the machine answers the discovery. Device identifiers and entity IDs are
  unchanged, so nothing is lost on an update.
- Entries created by 0.1.0 to 0.4.1 get the article number, the firmware, the serial
  number and the source of the model from the discovery when it works, in the
  background once the machine answers. The device then shows the firmware and the
  article number as model ID. The machine type is never changed.
- Home Assistant logs at INFO, once per change, when the machine becomes unreachable
  and when it is reachable again. It used to be only visible at DEBUG.
- *Filter wear* is unavailable instead of unknown when the machine does not report
  the indicator, as the E8 does without a filter.

### Fixed

- A machine that refused a connection with a state other than a wrong credential or
  PIN (`ABORTED`, `REJECTED:<code>`) was treated like rejected credentials: Home
  Assistant stopped polling until the machine was paired again. Only `WRONG_HASH`
  and `WRONG_PIN` count as rejected credentials now; any other refusal is handled like
  an unreachable machine.
- The brew counters missed the products that the profile does not offer as a drink
  but that the machine counts: 2x Espresso, 2x Coffee and the powder product on the
  E8 (disabled by default). The counters of the drinks added up to less than the
  total.

## [0.4.1] - 2026-10-09

### Added

- MIT license.
- GitHub workflows that check the repository with hassfest and the HACS action
  on every push and every night. HACS requires both to pass before it takes an
  integration into its default list.

### Changed

- The manifest no longer lists `ifaddr`. Home Assistant itself requires it (the
  same version is installed with every Home Assistant), and hassfest rejects it in
  the manifest of a custom integration.

The behaviour of the integration is unchanged.

## [0.4.0] - 2026-10-09

### Added

- Button **Rinse coffee system** (option **Maintenance buttons**). The machine
  profile of the E8 does not list this program, only the GIGA 6 profiles do, but a
  real E8 (SDS) starts it with the command of the J.O.E. app (`@TG:22`): it
  acknowledges the command, rinses for about a minute while it reports its
  progress, and counts the rinse. The rinse asks for no confirmation and starts at
  once, so put a cup under the spout first. The status shows `maintenance` with
  the detail `coffee_rinse` meanwhile. The button is offered for every machine that
  has the maintenance buttons; a machine that does not know the command reports a
  failed start.

### Fixed

- The README shows its logo in HACS: the image has an absolute address now and
  the `<picture>` element, which HACS displays as text, is gone. The link to the
  changelog works in HACS as well.

## [0.3.0] - 2026-10-09

### Added

- Maintenance buttons (option **Maintenance buttons**): one button per
  maintenance program that the machine profile declares (cleaning, descaling,
  filter change, milk system rinse, milk system cleaning). Only the start is
  sent; the machine asks for the confirmations on its display. The buttons are in
  the configuration section of the device.
- Cancel button (the cancel command of the J.O.E. app) next to the brew or
  maintenance buttons. It also works while the machine is busy.
- Pairing recognizes a machine that is in its settings menu or busy, because it
  cannot show the connect prompt then, and says so. Every text that asks to pair
  tells to leave the settings menu first.
- Icon and logo (light and dark) in `custom_components/jura_wifi/brand`, shown by
  Home Assistant 2026.3 and newer, and the script that draws them.
- End-to-end tests against the dongle simulator of the `jura-connect` library.
- This changelog and tagged GitHub releases, so that HACS shows a version number
  instead of a commit hash.

### Changed

- Brew buttons use the recipe that is stored on the machine, as set at its
  display, instead of the factory recipe. It is read in the same session; a
  machine that does not hand out its recipes gets the factory recipe.
- The status shows what a button has started right away, and a poll follows a few
  seconds later.
- Buttons of an option that is switched off again are removed instead of staying
  behind as unavailable entities.
- The settings menu is recognized whatever the byte after its state looks like.

## [0.2.1] - 2026-10-09

### Added

- Status states `brewing`, `maintenance`, `programming` and `busy`, with the
  activity as attribute. The values of the last full poll stay, and the brew
  buttons refuse to start another drink meanwhile.
- Checks that the English and German translations and the icons stay in sync.

### Fixed

- A machine that brews, runs a maintenance program or sits in its menu is
  reported online instead of offline. It pushes progress frames instead of status
  frames, which used to count as failed polls.

## [0.2.0] - 2026-10-09

### Added

- The exact model is read from the machine after pairing: article number and
  firmware from the UDP discovery reply, like the J.O.E. app does. If the machine
  does not answer, the article number is asked for, and the list of all models is
  the last resort.
- Model sensor (diagnostic); the device shows model, model ID and firmware.
- The reason of a refused pairing is logged and shown in the retry form.

### Changed

- The setup form only asks for the IP address of the dongle.

## [0.1.0] - 2026-10-09

### Added

- First release: config flow with pairing, reauthentication and reconfiguration;
  status, problem and maintenance sensors and brew counters; opt-in brew buttons
  with the factory recipes; diagnostics; English and German translations.

[Unreleased]: https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/compare/v0.5.2...HEAD
[0.5.2]: https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/releases/tag/v0.1.0
