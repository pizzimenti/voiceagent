"""Lightweight i18n shim registered as a QML context property.

This module provides a tiny translator wrapper used in place of
`PyKF6.KI18n.KLocalizedContext`, which is not currently shipped in
the PyKF6 bindings on Manjaro/Arch (no `PyKF6` package is importable
from this venv at all). The wrapper is registered as the `i18nCtx`
context property on the QML engine in `MainWindow.__init__` so
user-facing QML strings can be authored as
`i18nCtx.i18n("Voice Agent")` and stay translation-ready without
touching the Python-to-QML bridge.

Format-string call sites use Qt's standard QString.arg() chain at the
QML layer, e.g. `i18nCtx.i18n("Sent %1").arg(timestamp)` — so this
shim only needs the single identity-pass slot.

When `KLocalizedContext` becomes available (or another translator
backend is wired), swap the implementation here without touching any
QML call site.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Slot


class TranslatorContext(QObject):
    """Identity-passing translator exposed to QML as `i18nCtx`.

    Mirrors the simplest KI18n call shape: `i18nCtx.i18n("Voice
    Agent")` returns the literal string. Format-string call sites use
    QString.arg() at the QML layer, so we don't need to thread
    variadic args through PySide6 slot signatures.
    """

    @Slot(str, result=str)
    def i18n(self, source: str) -> str:  # noqa: D401 — QML-facing name
        return source
