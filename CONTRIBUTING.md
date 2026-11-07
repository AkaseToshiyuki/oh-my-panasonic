# Contributing

Project author and maintainer: [AkaseToshiyuki](https://github.com/AkaseToshiyuki).

Please include the model family, BIOS version, kernel version and the affected component. Remove hostnames, IP/MAC addresses, serial numbers, Wi-Fi names, credentials and biometric data from reports.

Run `PYTHONPATH=src python3 -m unittest discover -s tests -v`. WheelPad changes also require `(umask 077; cd vendor/letsnote-wheelpad; cargo test --locked)`. UI changes should include renders at laptop and wider window sizes.

Keep privileged operations narrowly scoped and reversible. Do not enable EC writes on a new model based solely on a similar port layout. Require documented commands, bounded operations, readback and restoration. Preserve stricter external limits, user settings and authentication fallbacks.

Record genuine hardware limitations in documentation; do not substitute percentages, negotiated limits or model-based estimates for unavailable sensors. UI text should focus on values, controls and actionable errors.

The default for hardware validators is help; actual actions require `--run`. Never include local credentials, firmware dumps, compiled models or enrollment data in a contribution.
