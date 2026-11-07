# Third-party components

| Component | Source revision | License |
| --- | --- | --- |
| WheelPad | [0xNOY/letsnote-wheelpad](https://github.com/0xNOY/letsnote-wheelpad), `5b52b8d58ea3990a793428533bdc179e217cd2ce` | [MIT](letsnote-wheelpad/LICENSE) |
| Facelock pose helper and optional GPU backend | [tyvsmith/facelock](https://github.com/tyvsmith/facelock), `352bbbcb0f8f61cc21e36a1f038442e94fa6e26b` | [MIT](Facelock-LICENSE-MIT), selected from the upstream dual license |

WheelPad includes local changes for a two-second systemd watchdog heartbeat and multi-contact pass-through to libinput. Its upstream tests and packaging fixtures are retained for regression coverage. The upstream installer is not used by Oh My Panasonic. Run its tests with umask 077.

Facelock source is fetched separately and compiled from the pinned revision. No model weights or enrollment data are included. Original third-party copyright notices are retained. Local modifications are maintained by AkaseToshiyuki.

The main deb includes license texts and source references for its Rust dependencies in `/usr/share/doc/panasonic-on-ubuntu/rust-licenses/`. OpenVINO, its runtime dependencies, and face model weights have separate terms; they are not part of the main source or binary release.
