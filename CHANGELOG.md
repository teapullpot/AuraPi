# Changelog

## v0.1.25

### Added
- External JSON locale system in `locales/`.
- English master/default/fallback locale (`locales/en.json`).
- German locale (`locales/de.json`).
- Live `EN` / `DE` language dropdown in the top-right of the header.
- Automatic discovery of additional locale JSON files.
- English fallback for missing translation keys.
- Translation placeholders for dynamic paths, device data, sizes, hashes and errors.
- Locale-aware tooltips, file dialogs, confirmations, validation messages, progress/status messages and operation buttons.
- Locale-aware embedded sudo Askpass/password dialog via `AURAPI_LANG` and `AURAPI_LOCALES_DIR`.
- `verify_locales.py` consistency checker for future translations.
- Optional "Show more devices" checkbox on the Backup tab (opt-in,
  defaults to off). Reveals drives without a serial number or a stable
  by-id path (e.g. exotic USB adapters), clearly marked with a warning
  tag in the device list.
- Hover tooltip explaining the tradeoff, available in both English and
  German.
- New locale keys: `devices.show_more`, `devices.show_more_tooltip`,
  `system.device.model_changed`.

### Changed
- English is now the primary UI language at application start.
- Internal operation identifiers remain language-neutral; visible labels are resolved only at presentation time.
- Device menu labels are rebuilt on language change while preserving the selected device by stable device ID.
- Runtime button states are no longer coupled to translated text.
- PiShrink installation guidance and verification status are locale-aware.
- User-facing AuraPi log messages are translated; raw output from external system tools remains unchanged where applicable.
- `list_removable_devices()` now accepts an `include_unstable` parameter.
- `reverify_device()` falls back to raw device path + model + size
  comparison for devices without a stable by-id path, since the usual
  by-id re-check isn't possible for them.
- Backup source and restore target device lists are now tracked
  separately internally, so the new filter can never affect the restore
  target — only the backup source.
- Device display strings (including the warning marker) are rebuilt
  correctly on a live language switch.

### Compatibility / safety
- Backup and restore modes and their internal values (`rpi`, `other`, `full`) are unchanged.
- Existing device verification, by-id handling, sudo session management, cancellation cleanup, atomic backup writing and restore safeguards remain in place.
- One language-dependent internal timeout inference was made language-neutral so program logic does not depend on translated error text.
- Restore target device list remains fail-closed by default, unchanged.
- Behavior for normal (stable, by-id-identified) devices is unchanged.
- The new "Show more devices" filter is strictly opt-in; unless enabled,
  behavior is identical to before this release.
