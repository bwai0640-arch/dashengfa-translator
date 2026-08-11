# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

import uiautomation


hiddenimports = [
    "pystray._win32",
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "uiautomation.uiautomation",
]

uia_bin = Path(uiautomation.__file__).resolve().parent / "bin"
binaries = [
    (str(uia_bin / "UIAutomationClient_VC140_X64.dll"), "uiautomation/bin"),
    (str(uia_bin / "UIAutomationClient_VC140_X86.dll"), "uiautomation/bin"),
]

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
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch",
        "tensorflow",
        "transformers",
        "onnx",
        "onnxruntime",
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
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["resources\\app_icon.ico"],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="DaShengFaTranslator",
)
