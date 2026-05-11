from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "voiceagent"


def _xdg_home(env_var: str, default_relative: str) -> Path:
    value = os.environ.get(env_var, "").strip()
    if value:
        return Path(value).expanduser()
    return Path.home() / default_relative


def app_data_dir() -> Path:
    return _xdg_home("XDG_DATA_HOME", ".local/share") / APP_NAME


def app_state_dir() -> Path:
    return _xdg_home("XDG_STATE_HOME", ".local/state") / APP_NAME


# ---------------------------------------------------------------------------
# Hierarchical, per-engine layout (v0.12.1+).
#
# `default_data_root()` is the top-level voiceagent data directory; every
# engine-specific helper resolves under it. The structure is:
#
#   ~/.local/share/voiceagent/
#     stt/
#       whisper/                      # default_whisper_root()
#         huggingface/                # HF cache used by faster-whisper
#         tiny.en/                    # downloaded model dirs
#         ...
#     tts/
#       piper/                        # default_piper_voices_root()
#         <voice>.onnx
#         <voice>.onnx.json
#         voices.json                 # Piper's catalog cache
#       chatterbox/                   # default_chatterbox_root()
#         model/                      # default_chatterbox_model_root()
#         references/                 # default_chatterbox_references_root()
#           default.wav
#           my-voice.wav
#
# The legacy `default_stt_model_root` / `default_tts_model_root` helpers
# remain as thin aliases — they now point at the new engine-scoped roots
# (`stt/whisper/` and `tts/piper/` respectively) so any direct caller that
# slipped through the audit lands in the right place rather than at the
# old `stt-models/` / `tts-models/` flat dirs.
# ---------------------------------------------------------------------------


def default_data_root() -> Path:
    """Top-level voiceagent data directory (`~/.local/share/voiceagent/`)."""
    return app_data_dir()


def default_stt_root() -> Path:
    """STT engines parent (`<data>/stt/`)."""
    return default_data_root() / "stt"


def default_tts_root() -> Path:
    """TTS engines parent (`<data>/tts/`)."""
    return default_data_root() / "tts"


def default_whisper_root() -> Path:
    """Whisper models (`<data>/stt/whisper/`)."""
    return default_stt_root() / "whisper"


def default_piper_voices_root() -> Path:
    """Piper voices + `voices.json` (`<data>/tts/piper/`)."""
    return default_tts_root() / "piper"


def default_chatterbox_root() -> Path:
    """Chatterbox engine root (`<data>/tts/chatterbox/`)."""
    return default_tts_root() / "chatterbox"


def default_chatterbox_model_root() -> Path:
    """Chatterbox downloaded model artifacts (`<chatterbox>/model/`)."""
    return default_chatterbox_root() / "model"


def default_chatterbox_references_root() -> Path:
    """Chatterbox user-supplied reference clips (`<chatterbox>/references/`)."""
    return default_chatterbox_root() / "references"


# ---------------------------------------------------------------------------
# Legacy aliases — retained so that any direct caller (or test patch
# target) that wasn't migrated still resolves to a sensible path. Both
# now return the engine-scoped equivalents, NOT the pre-v0.12.1 flat
# `stt-models/` / `tts-models/` directories.
# ---------------------------------------------------------------------------


def default_stt_model_root() -> Path:
    """Deprecated alias for `default_whisper_root()`.

    Pre-v0.12.1 this returned `<data>/stt-models/`. The new layout puts
    Whisper artifacts under `<data>/stt/whisper/`; this alias is kept
    so any straggling import does not break, but new code should call
    `default_whisper_root()` directly.
    """
    return default_whisper_root()


def default_tts_model_root() -> Path:
    """Deprecated alias for `default_piper_voices_root()`.

    Pre-v0.12.1 this returned `<data>/tts-models/`. The new layout puts
    Piper voices under `<data>/tts/piper/`; this alias is kept so any
    straggling import does not break, but new code should call
    `default_piper_voices_root()` directly.
    """
    return default_piper_voices_root()


def default_log_dir() -> Path:
    return app_state_dir() / "logs"


# ---------------------------------------------------------------------------
# Legacy → engine-scoped one-time migration (v0.12.1).
#
# v0.12.0 introduced the engine-scoped data tree but redirected the legacy
# `default_stt_model_root()` / `default_tts_model_root()` aliases to the
# NEW paths rather than the old flat dirs. So an upgrading v0.11.x user
# who didn't manually `mv` their dirs would see empty STT/TTS catalogs
# (the app reads the new empty dirs, the old data sits orphaned). No
# error — just a silent "your config evaporated" experience that
# triggers a multi-GB re-download.
#
# The migration here detects the legacy flat dirs at startup and moves
# their contents into the engine-scoped tree. Idempotent (no-op once
# migrated). Skipped when the user has set a `VOICEAGENT_*_ROOT` env
# var (their explicit choice overrides the default migration target).
# ---------------------------------------------------------------------------


# Legacy → new path pairs. Each entry: (legacy_relative_to_data_root,
# new_default_path_fn, env_override_var). The env override means
# "user has explicitly chosen a path, don't auto-migrate to the
# default — let them handle it."
_LEGACY_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("stt-models", "default_whisper_root", "VOICEAGENT_STT_MODEL_ROOT"),
    ("tts-models", "default_piper_voices_root", "VOICEAGENT_TTS_MODEL_ROOT"),
    (
        "chatterbox-references",
        "default_chatterbox_references_root",
        "VOICEAGENT_CHATTERBOX_REFERENCES_ROOT",
    ),
)


def migrate_legacy_data_dirs(logger=None) -> list[tuple[Path, Path]]:
    """Move v0.11.x flat data dirs into the v0.12+ engine-scoped tree.

    Run early at startup (before any service reads its model root).
    Returns the list of (legacy, new) pairs that were actually moved
    so callers can log / surface the migration. Empty list means
    nothing to do — already migrated, no legacy data, or all moves
    were skipped by env-override / conflict.

    Skip rules:
    - Legacy dir doesn't exist → nothing to move.
    - User has set the matching `VOICEAGENT_*_ROOT` env var to a
      non-blank value → don't auto-migrate; the user has an explicit
      path that would be ignored by a default-to-default move.
    - New dir already exists with content → don't clobber. Logs a
      warning so the user sees the conflict; manual merge needed.
    """
    moved: list[tuple[Path, Path]] = []
    data_root = default_data_root()
    if not data_root.exists():
        return moved
    # Late lookup so the names map to the live module-level functions
    # — keeps the table data-only and avoids a circular eval at import.
    here = globals()
    for legacy_name, new_fn_name, env_var in _LEGACY_MIGRATIONS:
        env_value = os.environ.get(env_var, "").strip()
        if env_value:
            # Explicit user override — skip silently.
            continue
        legacy = data_root / legacy_name
        if not legacy.exists() or not legacy.is_dir():
            continue
        new_fn = here.get(new_fn_name)
        if not callable(new_fn):
            continue
        new = new_fn()
        # If the new dir already has content, don't overwrite — let
        # the user resolve the conflict (they may have started fresh
        # post-upgrade and accumulated new state alongside the old).
        try:
            new_has_content = new.exists() and any(new.iterdir())
        except OSError:
            new_has_content = True  # fail closed
        if new_has_content:
            if logger is not None:
                logger.warning(
                    "Skipping legacy data migration: both %s and %s "
                    "have content. Merge manually if the old data "
                    "should take precedence.",
                    legacy, new,
                )
            continue
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            # Replace an empty new dir so `legacy.rename(new)` works
            # — rename refuses to overwrite a non-empty target on
            # POSIX, but empty-target replacement is portable.
            if new.exists():
                new.rmdir()
            legacy.rename(new)
        except OSError as exc:
            if logger is not None:
                logger.warning(
                    "Could not migrate legacy data dir %s -> %s (%s); "
                    "the app will treat the new dir as empty. Manual "
                    "`mv` recovers prior state.",
                    legacy, new, exc,
                )
            continue
        if logger is not None:
            logger.info(
                "Migrated legacy data dir %s -> %s", legacy, new,
            )
        moved.append((legacy, new))
    return moved
