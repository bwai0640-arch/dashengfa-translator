# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

import uiautomation
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs


hiddenimports = [
    "pystray._win32",
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "uiautomation.uiautomation",
]
hiddenimports += ["kokoro_onnx"]
hiddenimports += ["piper", "piper.voice", "piper.phonemize_espeak", "piper.espeakbridge"]

uia_bin = Path(uiautomation.__file__).resolve().parent / "bin"
binaries = [
    (str(uia_bin / "UIAutomationClient_VC140_X64.dll"), "uiautomation/bin"),
    (str(uia_bin / "UIAutomationClient_VC140_X86.dll"), "uiautomation/bin"),
]
binaries += collect_dynamic_libs("espeakng_loader")
binaries += collect_dynamic_libs("piper")
kokoro_runtime_data = collect_data_files("espeakng_loader")
kokoro_package_data = collect_data_files("kokoro_onnx")
phonemizer_data = collect_data_files("language_tags")
piper_package_data = collect_data_files("piper")

a = Analysis(
    ["desktop_app.py"],
    pathex=[],
    binaries=binaries,
    datas=[
        ("resources\\ecdict.db", "resources"),
        ("resources\\app_icon.png", "resources"),
        ("resources\\app_icon.ico", "resources"),
        ("resources\\models\\translate-en_zh-1_9\\model", "resources\\models\\translate-en_zh-1_9\\model"),
        ("resources\\models\\translate-en_zh-1_9\\sentencepiece.model", "resources\\models\\translate-en_zh-1_9"),
        ("resources\\models\\translate-en_zh-1_9\\metadata.json", "resources\\models\\translate-en_zh-1_9"),
        ("resources\\models\\translate-en_zh-1_9\\README.md", "resources\\models\\translate-en_zh-1_9"),
        ("resources\\models\\translate-zh_en-1_9\\model", "resources\\models\\translate-zh_en-1_9\\model"),
        ("resources\\models\\translate-zh_en-1_9\\sentencepiece.model", "resources\\models\\translate-zh_en-1_9"),
        ("resources\\models\\translate-zh_en-1_9\\metadata.json", "resources\\models\\translate-zh_en-1_9"),
        ("resources\\models\\translate-zh_en-1_9\\README.md", "resources\\models\\translate-zh_en-1_9"),
        ("resources\\models\\kokoro\\kokoro-v1.0.int8.onnx", "resources\\models\\kokoro"),
        ("resources\\models\\kokoro\\voices-v1.0.bin", "resources\\models\\kokoro"),
        ("resources\\models\\piper\\en_US-lessac-high.onnx", "resources\\models\\piper"),
        ("resources\\models\\piper\\en_US-lessac-high.onnx.json", "resources\\models\\piper"),
        ("resources\\models\\piper\\en_US-lessac-high.MODEL_CARD.md", "resources\\models\\piper"),
        ("resources\\models\\piper\\en_GB-cori-high.onnx", "resources\\models\\piper"),
        ("resources\\models\\piper\\en_GB-cori-high.onnx.json", "resources\\models\\piper"),
        ("resources\\models\\piper\\en_GB-cori-high.MODEL_CARD.md", "resources\\models\\piper"),
        ("resources\\models\\piper\\PIPER_GPL-3.0.txt", "resources\\models\\piper"),
        ("resources\\models\\piper\\README.md", "resources\\models\\piper"),
    ] + kokoro_runtime_data + kokoro_package_data + phonemizer_data + piper_package_data,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch",
        "tensorflow",
        "transformers",
        "fairseq",
        "sentence_transformers",
        "scipy",
        "sklearn",
        "spacy",
        "stanza",
        # CTranslate2 exposes optional conversion helpers.  They pull in the
        # following unrelated data-science and model-download stacks when
        # present in a developer's global Python environment, but the desktop
        # translator only uses CTranslate2's local runtime API.
        "gradio",
        "huggingface_hub",
        "hf_xet",
        "llvmlite",
        "lxml",
        "matplotlib",
        "numba",
        "pandas",
        "pyarrow",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DaShengFaTranslator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=["espeakbridge.pyd"],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["resources\\app_icon.ico"],
    version="DaShengFaTranslator.version.txt",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=["espeakbridge.pyd"],
    name="DaShengFaTranslator",
)
