# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), the versions follow
[Semantic Versioning](https://semver.org/). While the major version is 0, a minor
version may change behaviour.

## [Unreleased]

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

[Unreleased]: https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dominikgysin/HomeAssistant-Jura-WiFi-Integration/releases/tag/v0.1.0
