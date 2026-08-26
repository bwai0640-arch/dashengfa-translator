# Bundled Piper word voices

The single-word AI pronunciation path uses Piper TTS 1.6.0 on the CPU only:

- US: `en_US-lessac-high.onnx`
- UK: `en_GB-cori-high.onnx`

The Piper runtime is GPL-3.0-or-later. Its licence is bundled as
`PIPER_GPL-3.0.txt`; corresponding source is available at
https://github.com/OHF-Voice/piper1-gpl/tree/v1.6.0 . The PyInstaller build
also carries Piper's eSpeak-ng data and `espeakbridge` binary required for
offline phonemization. The application never downloads a voice or enables a
GPU provider at runtime.

Each model's upstream card is included beside its ONNX/config pair. The
maintainer has confirmed internal authorization for the free, non-commercial,
open-source use of the Lessac voice. Public materials retain the project
notice: if any rightsholder considers content infringing, contact author
"眼泪斷了线" for review and removal where appropriate.

All files in this folder are SHA256-verified before a Piper voice is loaded;
the pinned digests live in `app.py` and `desktop_app.py`.
