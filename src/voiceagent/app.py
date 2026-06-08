from __future__ import annotations

import sys
import logging
import os
import signal
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from voiceagent import __version__
from voiceagent.backends import SpeechToTextBackend, TextToSpeechBackend
from voiceagent.config import AppConfig
from voiceagent.controller import VoiceController
from voiceagent.logging_utils import configure_logging
from voiceagent.model_loader import WhisperModelLoader
from voiceagent.services.audio import MicrophoneRecorder
from voiceagent.services.chat import LmStudioClient
from voiceagent.services.playback import AudioPlayer
from voiceagent.single_instance import acquire_or_activate
from voiceagent.tts_loader import TtsVoiceLoader
from voiceagent.window import MainWindow

if TYPE_CHECKING:
    pass


def _chatterbox_extras_available() -> bool:
    """Return True if every optional dependency the Chatterbox engine
    needs is importable. Used as the gate before instantiating
    `ChatterboxTtsService` so the user gets a deterministic fall-back
    to Piper instead of an `ImportError` on first synth. `huggingface_hub`
    is a hard dep of the project so it's not in the probe list.
    """
    import importlib.util

    for name in ("onnxruntime", "transformers", "librosa", "soundfile"):
        if importlib.util.find_spec(name) is None:
            return False
    return True


def _kokoro_extras_available() -> bool:
    """Return True if the optional Kokoro engine deps are importable.

    Mirrors the Chatterbox probe: check every module that
    `from kokoro_onnx import Kokoro` + a first synth transitively imports
    (`KOKORO_EXTRA_MODULES`: the wrapper, ONNX Runtime, numpy, and the
    phonemizer side — `phonemizer` + `espeakng_loader`), not just the
    top-level wrapper. A wrapper-only `find_spec` is a false positive when
    `kokoro_onnx` is discoverable but a transitive dep is missing (a
    `--no-deps`/partial install), which would bypass the deterministic
    Piper fallback and defer the crash to first synth. `find_spec` (not a
    real import) keeps the heavy `onnxruntime` load off the startup path
    while still catching an absent dep. Install via
    `pip install --ignore-requires-python voiceagent[kokoro]` (the flag
    works around kokoro-onnx's conservative `<3.14` cap; cp314 wheels
    exist).
    """
    import importlib.util

    from voiceagent.services.kokoro_tts import KOKORO_EXTRA_MODULES

    for name in KOKORO_EXTRA_MODULES:
        if importlib.util.find_spec(name) is None:
            return False
    return True


def build_shared_services(
    config: AppConfig,
) -> tuple[SpeechToTextBackend, TextToSpeechBackend, WhisperModelLoader, TtsVoiceLoader]:
    # Backend imports stay local so future optional engines can be added
    # without forcing every provider dependency to be importable at startup.
    from voiceagent.services.stt import WhisperTranscriber

    logger = logging.getLogger(__name__)

    transcriber = WhisperTranscriber(
        model_name=config.whisper_model,
        device=config.whisper_device,
        compute_type=config.whisper_compute_type,
    )
    transcriber.model_root = config.stt_model_root
    if config.tts_engine == "chatterbox" and _chatterbox_extras_available():
        from voiceagent.paths import default_chatterbox_model_root
        from voiceagent.services.chatterbox_tts import ChatterboxTtsService

        # `tts_model_root` is now Piper-specific (defaults to
        # `<data>/tts/piper/`), so appending `/chatterbox` to it would
        # land inside Piper's voices directory. The Chatterbox model
        # cache has its own engine-scoped root under `<data>/tts/chatterbox/model/`.
        tts_service: TextToSpeechBackend = ChatterboxTtsService(
            model_root=default_chatterbox_model_root(),
            references_root=config.chatterbox_references_root,
            selected_item=config.tts_model,
        )
    elif config.tts_engine == "kokoro" and _kokoro_extras_available():
        from voiceagent.paths import default_kokoro_model_root
        from voiceagent.services.kokoro_tts import KokoroTtsService

        # Like Chatterbox, Kokoro gets its own engine-scoped model root
        # (`<data>/tts/kokoro/model/`) so the single bundle can't collide
        # with Piper voices under `<data>/tts/piper/`.
        tts_service = KokoroTtsService(
            model_root=default_kokoro_model_root(),
            selected_item=config.tts_model,
        )
    else:
        if config.tts_engine == "chatterbox":
            logger.warning(
                "Chatterbox engine selected but extras not installed; "
                "falling back to Piper"
            )
        elif config.tts_engine == "kokoro":
            logger.warning(
                "Kokoro engine selected but extras not installed; "
                "falling back to Piper"
            )
        from voiceagent.services.tts import PiperTtsService

        tts_service = PiperTtsService(
            command=config.tts_command,
            model_path=config.tts_model,
            extra_args=config.tts_extra_args,
        )
        tts_service.model_root = config.tts_model_root
    model_loader = WhisperModelLoader(transcriber)
    tts_loader = TtsVoiceLoader(tts_service)
    return transcriber, tts_service, model_loader, tts_loader


def configure_model_environment(stt_model_root: Path, tts_model_root: Path) -> None:
    stt_model_root.mkdir(parents=True, exist_ok=True)
    tts_model_root.mkdir(parents=True, exist_ok=True)
    hf_home = stt_model_root / "huggingface"
    hf_home.mkdir(parents=True, exist_ok=True)

    os.environ["HF_HOME"] = str(hf_home)
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(hf_home / "hub")
    os.environ["HF_HUB_CACHE"] = str(hf_home / "hub")
    os.environ["TRANSFORMERS_CACHE"] = str(hf_home / "transformers")


def build_controller(
    config: AppConfig,
    transcriber: SpeechToTextBackend,
    tts_service: TextToSpeechBackend,
) -> VoiceController:
    recorder = MicrophoneRecorder(sample_rate=config.sample_rate)
    chat_client = LmStudioClient(
        base_url=config.lm_studio_base_url,
        model=config.lm_studio_model,
        system_prompt=config.lm_studio_system_prompt,
        timeout_seconds=config.lm_studio_timeout_seconds,
        load_timeout_seconds=config.lm_studio_load_timeout_seconds,
    )
    player = AudioPlayer()
    controller = VoiceController(
        recorder=recorder,
        transcriber=transcriber,
        chat_client=chat_client,
        tts_service=tts_service,
        player=player,
    )
    controller.max_history_turns = config.max_history_turns
    return controller


def _prewarm_sounddevice(logger: logging.Logger) -> None:
    """Force sounddevice + PortAudio to load off the first-paint path.

    The first import of sounddevice loads the PortAudio C library and
    can take 100-500 ms. If that cost is paid lazily inside
    MicrophoneRecorder.start(), it stretches the gap between mic
    button click and visible UI response. We run this on a daemon
    thread (see main()) so the import cost is genuinely off the main
    Qt thread; the GUI keeps painting while PortAudio loads.
    """
    import time as _time
    started = _time.monotonic()
    try:
        import sounddevice as _sd  # noqa: F401
    except Exception as exc:  # pragma: no cover - depends on host audio stack
        logger.warning("sounddevice pre-warm failed: %s", exc)
        return
    logger.info(
        "sounddevice pre-warm ok ms=%.1f",
        (_time.monotonic() - started) * 1000.0,
    )


def _silence_upstream_qml_chatter() -> None:
    """Filter the noisy Kirigami `ToolBarLayout` incubation warning.

    Kirigami 6.x's `ActionToolBar` (used by every `Kirigami.ApplicationWindow`
    page header) logs `kf.kirigami.layouts: Could not create delegate for
    ToolBarLayout / Object or context destroyed during incubation` repeatedly
    during initial layout and at every responsive-mode transition. The
    warning is upstream chatter — the delegates do get created, the
    toolbar renders correctly, and every Kirigami app emits these.

    Filter the specific category so real warnings still surface. Respect
    any user-supplied `QT_LOGGING_RULES` so a developer setting them for
    diagnostics isn't clobbered.

    Must run before `QApplication()` is constructed because Qt reads
    `QT_LOGGING_RULES` at logging init.
    """
    rule = "kf.kirigami.layouts.warning=false"
    existing = os.environ.get("QT_LOGGING_RULES", "")
    if rule in existing:
        return
    os.environ["QT_LOGGING_RULES"] = f"{existing};{rule}" if existing else rule


def _ensure_qml_import_path() -> None:
    """Prepend system Qt 6 QML directories to QML_IMPORT_PATH if absent.

    A bare `voiceagent` entry-point launch doesn't get QML_IMPORT_PATH
    from a wrapper the way `voiceagent-buildtest.sh` does, so QML
    modules like `org.kde.kirigami` (installed under `/usr/lib/qt6/qml`
    on Manjaro/Arch/Fedora-derived distros) wouldn't resolve. Auto-
    detect and prepend the standard locations so both launch paths
    work; respect any existing value in the env.
    """
    candidates = ["/usr/lib/qt6/qml", "/usr/lib/qt/qml"]
    found = [p for p in candidates if os.path.isdir(p)]
    if not found:
        return
    for var in ("QML_IMPORT_PATH", "QML2_IMPORT_PATH"):
        existing = os.environ.get(var, "")
        merged_parts = found + ([existing] if existing else [])
        os.environ[var] = os.pathsep.join(merged_parts)


def _install_sigint_handler(
    app: QApplication,
) -> tuple[QTimer, dict[str, bool], object]:
    """Let Ctrl+C leave through Qt's normal quit path.

    Qt can sit in the C++ event loop long enough that Python's default
    SIGINT handling lands during an arbitrary Python callback, including
    QObject teardown. A tiny timer keeps Python signal checks alive while
    the handler itself only requests a normal QApplication quit.
    """

    interrupted = {"value": False}
    previous_handler = signal.getsignal(signal.SIGINT)

    def _handle_sigint(_signum, _frame) -> None:
        interrupted["value"] = True
        app.quit()

    signal.signal(signal.SIGINT, _handle_sigint)

    timer = QTimer(app)
    timer.setInterval(250)
    timer.timeout.connect(lambda: None)
    timer.start()
    return timer, interrupted, previous_handler


def main() -> int:
    log_path = configure_logging()
    logger = logging.getLogger(__name__)
    console = logging.getLogger("voiceagent.console")
    logger.info("Starting voiceagent")
    console.info("Voice Agent %s", __version__)
    console.info("Starting services...")
    _silence_upstream_qml_chatter()
    _ensure_qml_import_path()
    app = QApplication(sys.argv)
    app.setApplicationName("voiceagent")
    app.setApplicationDisplayName("Voice Agent")
    app.setDesktopFileName("voiceagent")
    app.setOrganizationName("voiceagent")
    app.setWindowIcon(QIcon.fromTheme("audio-input-microphone"))
    instance = acquire_or_activate()
    if instance is None:
        console.info("Another Voice Agent instance is already running; activating it.")
        return 0
    config = AppConfig.from_env()
    configure_model_environment(config.stt_model_root, config.tts_model_root)
    logger.info("Configured log file path=%s", log_path)
    logger.info("Configured STT model root path=%s", config.stt_model_root)
    logger.info("Configured TTS model root path=%s", config.tts_model_root)
    transcriber, tts_service, model_loader, tts_loader = build_shared_services(config)
    controller = build_controller(config, transcriber=transcriber, tts_service=tts_service)
    window = MainWindow(controller, model_loader, tts_loader)
    instance.activated.connect(window.show)
    # Release the lock file + QLocalServer deterministically on graceful shutdown,
    # not via Python GC of `instance` (which SystemExit or hard interrupt can bypass).
    app.aboutToQuit.connect(instance.release)
    window.show()
    # Run the sounddevice/PortAudio pre-warm in a daemon thread so the
    # import cost is genuinely off the main Qt thread. A previous
    # iteration used QTimer.singleShot(0, ...) but a 0 ms timer can
    # fire on the next event-loop tick before the first frame swap
    # completes, defeating the point. The import does no Qt work and
    # PortAudio releases the GIL during its C library load, so the
    # GUI thread keeps painting while this runs. If the user clicks
    # the mic before this finishes, MicrophoneRecorder.start()'s
    # `import sounddevice` blocks on Python's import lock until this
    # thread completes — same worst case as the pre-fix behavior.
    import threading
    threading.Thread(
        target=_prewarm_sounddevice,
        args=(logger,),
        daemon=True,
        name="sounddevice-prewarm",
    ).start()
    console.info("Ready.")
    sigint_timer, sigint_state, previous_sigint_handler = _install_sigint_handler(app)
    try:
        exit_code = app.exec()
    finally:
        sigint_timer.stop()
        signal.signal(signal.SIGINT, previous_sigint_handler)
    return 130 if sigint_state["value"] else exit_code
