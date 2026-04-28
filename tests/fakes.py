"""Shared test doubles + a real-MainWindow builder for compiletest.

The fake backends in this module are the same ones used by
`tests/test_mainwindow_integration.py`. They live here (rather than
private to that file) so `voiceagent-compiletest.sh` can import them
to drive a real `MainWindow` against a real `QQmlApplicationEngine`,
which means compiletest exercises the actual property/slot surface
the QML binds against. The previous compiletest mirrored that surface
via a hand-maintained `StubVoiceAgent`; the stub silently drifted
when MainWindow grew new slots/properties. With this module, drift
becomes structurally impossible — the live class is the only surface.

Naming: the doubles drop the leading underscore the test-private
versions carried, since they are now part of a shared helper module.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# Resolve the worktree's `src/` before any editable install so the
# real `voiceagent` package imports work both from `pytest` and from
# `voiceagent-compiletest.sh` (which sets PYTHONPATH but still benefits
# from a defensive insert here, since this module is imported either way).
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Headless Qt for any environment that didn't already set this. The
# compiletest script also exports QT_QPA_PLATFORM=offscreen; this
# setdefault is a belt-and-suspenders for direct imports.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal

if TYPE_CHECKING:
    from voiceagent.window import MainWindow


# --- test doubles --------------------------------------------------------


class FakeRecorder:
    """Minimal MicrophoneRecorder stand-in."""

    def __init__(self) -> None:
        self.sample_rate = 16000
        self.is_recording = False

    def start(self, *, segment_ready_callback=None) -> None:
        self.is_recording = True

    def stop(self, *, discard: bool = False) -> None:
        self.is_recording = False

    def take_pending_segment(self):
        return None

    def snapshot_active_segment(self):
        return None

    def force_finalize_active_segment(self, reason: str) -> bool:
        return False

    def suspend_input(self) -> None:
        pass

    def resume_input(self, warmup_seconds: float = 0.0, reason: str = "") -> None:
        pass


class FakeChatClient:
    """LmStudioClient stand-in covering the surface LlmController reads."""

    def __init__(self) -> None:
        self.base_url = ""
        self.model = ""

    def complete(self, text: str) -> str:
        return ""

    def set_base_url(self, url: str) -> None:
        self.base_url = self.normalize_base_url(url)

    def set_model(self, model: str) -> None:
        self.model = model

    @staticmethod
    def normalize_base_url(value: str) -> str:
        return (value or "").strip().rstrip("/")


class FakePlayer(QObject):
    """AudioPlayer signal-shape stand-in."""

    playback_started = Signal(str)
    playback_finished = Signal(str)
    playback_failed = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.played_paths: list[Path] = []

    def stop(self) -> None:
        pass

    def play_file(self, path) -> bool:
        self.played_paths.append(path)
        return True

    def set_muted(self, muted: bool) -> None:
        self._muted = muted


class FakeTranscriber:
    """`SpeechToTextBackend` stand-in covering both protocol surfaces:
    the basic backend interface AND the WhisperTranscriber-specific
    methods MainWindow's catalog adapter calls.
    """

    backend_name = "Fake-Whisper"
    selection_label = "Model"
    is_loaded = True

    MODEL_REPOSITORIES: dict[str, str] = {
        "tiny.en": "Systran/faster-whisper-tiny.en",
        "base.en": "Systran/faster-whisper-base.en",
    }

    def __init__(self, model_root: Path, model_name: str = "tiny.en") -> None:
        self.model_root = model_root
        self.model_name = model_name
        self._installed: set[str] = set()
        self._custom_path: str | None = (
            model_name if self._is_custom_path(model_name) else None
        )

    @classmethod
    def _is_custom_path(cls, name: str | None) -> bool:
        if not name:
            return False
        if name in cls.MODEL_REPOSITORIES:
            return False
        return "/" in name or name.startswith("~") or Path(name).is_absolute()

    @classmethod
    def available_model_names(cls) -> list[str]:
        return list(cls.MODEL_REPOSITORIES.keys())

    def available_items(self) -> list[str]:
        names = self.available_model_names()
        if self._custom_path:
            names.append(self._custom_path)
        return names

    @property
    def selected_item(self) -> str:
        return self.model_name

    @property
    def is_available(self) -> bool:
        return self.is_item_available(self.model_name)

    def set_model_name(self, model_name: str) -> None:
        if self.model_name == model_name:
            return
        self.model_name = model_name
        self._custom_path = (
            model_name if self._is_custom_path(model_name) else None
        )

    def set_selected_item(self, item_name: str) -> None:
        self.set_model_name(item_name)

    def is_item_available(self, name: str) -> bool:
        return name in self._installed

    def is_item_managed(self, name: str) -> bool:
        return name in self.MODEL_REPOSITORIES

    def is_item_downloadable(self, name: str) -> bool:
        return self.is_item_managed(name)

    def download_item(self, name: str, progress_callback=None) -> None:
        self._installed.add(name)

    def remove_item(self, name: str) -> None:
        self._installed.discard(name)

    def artifact_paths(self, name: str) -> list[Path]:
        return []

    def ensure_loaded(self) -> None:
        pass

    def transcribe(self, path: Path) -> str:
        return ""


class FakeTts:
    """`TextToSpeechBackend` stand-in covering the surface MainWindow
    + TtsVoiceLoader expect.
    """

    backend_name = "Fake-Piper"
    selection_label = "Voice"

    def __init__(self, model_root: Path) -> None:
        self.model_root = model_root
        self.command = ["piper"]
        self.model_path: str | None = None
        self._installed: set[str] = set()
        self._available_flag = False
        # Counters for replay-path tests.
        self.synthesize_calls: list[str] = []
        # If set, synthesize raises with this exception.
        self.synthesize_raises: Exception | None = None

    # -- properties ----------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.command and self.model_path)

    @property
    def is_available(self) -> bool:
        return self._available_flag

    def set_available(self, value: bool) -> None:
        self._available_flag = value

    @property
    def selected_item(self) -> str | None:
        return self.model_path

    def set_selected_item(self, item_name: str | None) -> None:
        self.model_path = item_name

    # -- catalog protocol ---------------------------------------------

    def available_items(self) -> list[str]:
        return sorted(self._installed)

    def is_item_available(self, name: str) -> bool:
        return name in self._installed

    def is_item_managed(self, name: str) -> bool:
        return True

    def is_item_downloadable(self, name: str) -> bool:
        return True

    def download_item(self, name: str, progress_callback=None) -> None:
        self._installed.add(name)

    def remove_item(self, name: str) -> None:
        self._installed.discard(name)

    def artifact_paths(self, name: str) -> list[Path]:
        return []

    def refresh_catalog(self) -> list[str]:
        return self.available_items()

    def describe_selection_state(self) -> dict:
        return {
            "selected_model": self.model_path or "",
            "available": self._available_flag,
            "can_download": False,
            "resolved_model_path": "",
            "direct_candidate": "",
            "local_candidate": "",
            "onnx_candidate": "",
            "json_candidate": "",
        }

    # -- synthesis ----------------------------------------------------

    def synthesize(self, text: str, progress_callback=None) -> Path | None:
        self.synthesize_calls.append(text)
        if self.synthesize_raises is not None:
            raise self.synthesize_raises
        out = self.model_root / f"replay-{len(self.synthesize_calls)}.wav"
        out.write_bytes(b"fake-wav")
        return out


# --- compiletest builder -------------------------------------------------


def build_compiletest_window() -> "MainWindow":
    """Construct a real MainWindow wired to fake backends, suitable
    for compiletest's QQmlApplicationEngine load. Caller owns
    QApplication lifecycle.

    The real `QQmlApplicationEngine` is *not* monkeypatched here —
    that's the entire point of using this builder from compiletest.
    The engine loads `MainWindow.qml` against the real `MainWindow`
    instance's real property/slot surface, so any drift between QML
    bindings and Python surface fails compiletest immediately.

    Mirrors the production constructor arg order from
    `voiceagent.app.main()`:
        MainWindow(controller, model_loader, tts_loader)
    """
    import tempfile

    from voiceagent.controller import VoiceController
    from voiceagent.model_loader import WhisperModelLoader
    from voiceagent.tts_loader import TtsVoiceLoader
    from voiceagent.window import MainWindow

    # tempfile for the fakes' model_root. The fakes only touch
    # `model_root` as a Path container for `replay-*.wav` writes that
    # only happen in `synthesize()`, which compiletest never calls.
    # A throwaway temp dir keeps it hermetic.
    model_root = Path(tempfile.mkdtemp(prefix="voiceagent-compiletest-"))

    transcriber = FakeTranscriber(model_root=model_root)
    tts_service = FakeTts(model_root=model_root)
    controller = VoiceController(
        recorder=FakeRecorder(),
        transcriber=transcriber,
        chat_client=FakeChatClient(),
        tts_service=tts_service,
        player=FakePlayer(),
    )
    model_loader = WhisperModelLoader(transcriber)
    tts_loader = TtsVoiceLoader(tts_service)
    return MainWindow(controller, model_loader, tts_loader)
