#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENV_PYTHON="${ROOT_DIR}/.venv/bin/python"

if [ ! -x "${VENV_PYTHON}" ]; then
  echo "Missing virtualenv python at ${VENV_PYTHON}" >&2
  exit 1
fi

if [ -n "${PYTHONPATH:-}" ]; then
  export PYTHONPATH="${ROOT_DIR}/src:${PYTHONPATH}"
else
  export PYTHONPATH="${ROOT_DIR}/src"
fi

QML_PATHS=""
for candidate in /usr/lib/qt6/qml /usr/lib/qt/qml; do
  if [ -d "${candidate}" ]; then
    if [ -n "${QML_PATHS}" ]; then
      QML_PATHS="${QML_PATHS}:${candidate}"
    else
      QML_PATHS="${candidate}"
    fi
  fi
done

if [ -n "${QML_PATHS}" ]; then
  if [ -n "${QML_IMPORT_PATH:-}" ]; then
    export QML_IMPORT_PATH="${QML_PATHS}:${QML_IMPORT_PATH}"
  else
    export QML_IMPORT_PATH="${QML_PATHS}"
  fi
  if [ -n "${QML2_IMPORT_PATH:-}" ]; then
    export QML2_IMPORT_PATH="${QML_PATHS}:${QML2_IMPORT_PATH}"
  else
    export QML2_IMPORT_PATH="${QML_PATHS}"
  fi
fi

export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"

"${VENV_PYTHON}" -m py_compile \
  "${ROOT_DIR}/src/voiceagent/app.py" \
  "${ROOT_DIR}/src/voiceagent/window.py" \
  "${ROOT_DIR}/src/voiceagent/controller.py"

# Lint MainWindow.qml in full (with the stub `voiceAgent` defined
# below) AND each extracted component standalone. The standalone
# invocations catch errors local to a component (missing import,
# malformed binding) without the stub having to be kept perfectly
# in sync — the stub only resolves `voiceAgent` references inside
# MainWindow.qml's nested usage.
qmllint \
  "${ROOT_DIR}/src/voiceagent/qml/MainWindow.qml" \
  "${ROOT_DIR}/src/voiceagent/qml/MicButton.qml" \
  "${ROOT_DIR}/src/voiceagent/qml/ConversationPane.qml"

"${VENV_PYTHON}" -c '
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QByteArray, QModelIndex, Property, QObject, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import QApplication

class StubConversationModel(QAbstractListModel):
    MessageRole = Qt.ItemDataRole.UserRole + 1
    LevelRole = Qt.ItemDataRole.UserRole + 2
    TextRole = Qt.ItemDataRole.UserRole + 3
    ReplayableRole = Qt.ItemDataRole.UserRole + 4
    BubbleStateRole = Qt.ItemDataRole.UserRole + 5
    TurnPendingRole = Qt.ItemDataRole.UserRole + 6
    TimestampLabelRole = Qt.ItemDataRole.UserRole + 7
    StateNameRole = Qt.ItemDataRole.UserRole + 8
    _roles = {
        MessageRole: QByteArray(b"messageRole"),
        LevelRole: QByteArray(b"level"),
        TextRole: QByteArray(b"text"),
        ReplayableRole: QByteArray(b"replayable"),
        BubbleStateRole: QByteArray(b"bubbleState"),
        TurnPendingRole: QByteArray(b"turnPending"),
        TimestampLabelRole: QByteArray(b"timestampLabel"),
        StateNameRole: QByteArray(b"stateName"),
    }
    _keys = {
        MessageRole: "role",
        LevelRole: "level",
        TextRole: "text",
        ReplayableRole: "replayable",
        BubbleStateRole: "bubbleState",
        TurnPendingRole: "turnPending",
        TimestampLabelRole: "timestampLabel",
    }

    def __init__(self):
        super().__init__()
        self._messages = [
            {"role": "user", "text": "Test input", "replayable": False, "bubbleState": "sent", "timestampLabel": "Sent 10:00"},
            {"role": "assistant", "text": "Test response", "replayable": True, "bubbleState": "sent", "timestampLabel": "Received 10:00"},
        ]

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._messages)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() < 0 or index.row() >= len(self._messages):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            role = self.TextRole
        key = self._keys.get(role)
        return self._messages[index.row()].get(key) if key else None

    def roleNames(self):
        return self._roles

class StubCatalogModel(QAbstractListModel):
    NameRole = Qt.ItemDataRole.UserRole + 1
    InstalledRole = Qt.ItemDataRole.UserRole + 2
    LoadingRole = Qt.ItemDataRole.UserRole + 3
    ProgressRole = Qt.ItemDataRole.UserRole + 4
    DownloadableRole = Qt.ItemDataRole.UserRole + 5
    ManagedRole = Qt.ItemDataRole.UserRole + 6
    _roles = {
        NameRole: QByteArray(b"name"),
        InstalledRole: QByteArray(b"installed"),
        LoadingRole: QByteArray(b"loading"),
        ProgressRole: QByteArray(b"progress"),
        DownloadableRole: QByteArray(b"downloadable"),
        ManagedRole: QByteArray(b"managed"),
    }

    def __init__(self, entries):
        super().__init__()
        self._entries = list(entries)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._entries)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() < 0 or index.row() >= len(self._entries):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            role = self.NameRole
        entry = self._entries[index.row()]
        if role == self.NameRole:
            return entry["name"]
        if role == self.InstalledRole:
            return bool(entry.get("installed", False))
        if role == self.LoadingRole:
            return bool(entry.get("loading", False))
        if role == self.ProgressRole:
            return float(entry.get("progress", 0.0))
        if role == self.DownloadableRole:
            return bool(entry.get("downloadable", True))
        if role == self.ManagedRole:
            return bool(entry.get("managed", True))
        return None

    def roleNames(self):
        return self._roles

# StubVoiceAgent mirrors the property / slot surface that QML reads
# off voiceAgent. Drift between this stub and the real MainWindow is
# silent: qmllint accepts any property that exists on the stub even
# if MainWindow no longer exposes it, and vice versa. When a
# property/slot is added or removed on MainWindow, update this stub
# in the same commit. The standalone qmllint runs above
# (MicButton.qml, ConversationPane.qml) catch component-local errors
# without depending on this stub.
class StubVoiceAgent(QObject):
    ui_changed = Signal()
    conversation_changed = Signal()

    @Property("QVariantList", notify=ui_changed)
    def sttOptions(self):
        return ["large-v3"]

    @Property("QVariantList", notify=ui_changed)
    def ttsOptions(self):
        return ["en_US-lessac-medium"]

    @Property(int, notify=ui_changed)
    def sttInstalledCount(self):
        return 1

    @Property(int, notify=ui_changed)
    def ttsInstalledCount(self):
        return 1

    @Property(str, notify=ui_changed)
    def selectedSttModel(self):
        return "large-v3"

    @Property(str, notify=ui_changed)
    def selectedTtsModel(self):
        return "en_US-lessac-medium"

    @Property(str, notify=ui_changed)
    def modelStatus(self):
        return "Whisper model ready"

    @Property(bool, notify=ui_changed)
    def modelLoading(self):
        return False

    @Property(float, notify=ui_changed)
    def modelProgressValue(self):
        return 1.0

    @Property(bool, notify=ui_changed)
    def modelProgressIndeterminate(self):
        return False

    @Property(str, notify=ui_changed)
    def modelProgressText(self):
        return ""

    @Property(str, notify=ui_changed)
    def ttsStatus(self):
        return "Piper voice ready"

    @Property(bool, notify=ui_changed)
    def ttsLoading(self):
        return False

    @Property(float, notify=ui_changed)
    def ttsProgressValue(self):
        return 1.0

    @Property(bool, notify=ui_changed)
    def ttsProgressIndeterminate(self):
        return False

    @Property(str, notify=ui_changed)
    def ttsProgressText(self):
        return ""

    @Property("QVariantList", notify=ui_changed)
    def llmUrls(self):
        return ["http://127.0.0.1:1234/v1"]

    @Property(str, notify=ui_changed)
    def currentLlmUrl(self):
        return "http://127.0.0.1:1234/v1"

    @Property("QVariantList", notify=ui_changed)
    def llmModelOptions(self):
        return ["", "local-model"]

    @Property(str, notify=ui_changed)
    def selectedLlmModel(self):
        return "local-model"

    @Property(bool, notify=ui_changed)
    def llmServerConnected(self):
        return True

    @Property(bool, notify=ui_changed)
    def llmConnectionBusy(self):
        return False

    @Property(bool, notify=ui_changed)
    def llmModelBusy(self):
        return False

    @Property(str, notify=ui_changed)
    def llmConnectionButtonText(self):
        return "Disconnect"

    @Property(bool, notify=ui_changed)
    def talkReady(self):
        return True

    @Property(str, notify=ui_changed)
    def micStatusLabel(self):
        return "Connected"

    @Property(bool, notify=ui_changed)
    def voiceConnectionEnabled(self):
        return False

    @Property(str, notify=ui_changed)
    def voiceConnectionLabel(self):
        return "Voice Connection Off"

    @Property(bool, notify=ui_changed)
    def audioMuted(self):
        return False

    @Property(str, notify=ui_changed)
    def themeMode(self):
        return "auto"

    @Property(str, notify=ui_changed)
    def themeModeLabel(self):
        return "Auto"

    @Property(bool, notify=ui_changed)
    def logVerboseMode(self):
        return False

    def __init__(self):
        super().__init__()
        self._conversation_model = StubConversationModel()
        self._stt_catalog_model = StubCatalogModel([
            {"name": "large-v3", "installed": True},
            {"name": "small", "installed": False},
        ])
        self._tts_catalog_model = StubCatalogModel([
            {"name": "en_US-lessac-medium", "installed": True},
            {"name": "en_US-amy-medium", "installed": False},
        ])

    @Property(QObject, constant=True)
    def conversationModel(self):
        return self._conversation_model

    @Property(QObject, constant=True)
    def sttCatalogModel(self):
        return self._stt_catalog_model

    @Property(QObject, constant=True)
    def ttsCatalogModel(self):
        return self._tts_catalog_model

    @Property(int, notify=conversation_changed)
    def conversationMessageCount(self):
        return self._conversation_model.rowCount()

    @Property(str, notify=ui_changed)
    def errorMessage(self):
        return ""

    @Property(str, notify=ui_changed)
    def statusMessage(self):
        return "Compile test"

    @Property(str, notify=ui_changed)
    def state(self):
        return "idle"

    @Slot(str)
    def selectSttModel(self, _value):
        pass

    @Slot(str)
    def installSttModel(self, _value):
        pass

    @Slot(str)
    def deleteSttModel(self, _value):
        pass

    @Slot(str)
    def selectTtsModel(self, _value):
        pass

    @Slot(str)
    def installTtsModel(self, _value):
        pass

    @Slot(str)
    def deleteTtsModel(self, _value):
        pass

    @Slot(str)
    def setCurrentLlmUrl(self, _value):
        pass

    @Slot(bool)
    def refreshLlmModels(self, _show_error):
        pass

    @Slot(str)
    def toggleLlmServerConnection(self, _value):
        pass

    @Slot(str)
    def selectLlmModel(self, _value):
        pass

    @Slot(bool)
    def setVoiceConnectionEnabled(self, _enabled):
        pass

    @Slot(bool)
    def setAudioMuted(self, _enabled):
        pass

    @Slot(bool)
    def setLogVerboseMode(self, _enabled):
        pass

    @Slot(str)
    def setThemeMode(self, _mode):
        pass

    @Slot(int)
    def replayMessage(self, _index):
        pass

app = QApplication([])
engine = QQmlApplicationEngine()
stub = StubVoiceAgent()
engine.setInitialProperties({"voiceAgent": stub})
qml_path = Path("src/voiceagent/qml/MainWindow.qml").resolve()
engine.load(QUrl.fromLocalFile(str(qml_path)))
if not engine.rootObjects():
    raise SystemExit(f"Failed to load QML interface: {qml_path}")

root = engine.rootObjects()[0]

# Let deferred Loader/Window creation settle before teardown.
QTimer.singleShot(0, app.quit)
app.exec()

if hasattr(root, "close"):
    root.close()
root.deleteLater()
engine.collectGarbage()
app.sendPostedEvents()
app.processEvents()
QTimer.singleShot(0, app.quit)
app.exec()

print(f"compile ok: {qml_path}")
'
