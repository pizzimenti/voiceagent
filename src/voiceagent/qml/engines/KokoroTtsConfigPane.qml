import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.kde.kirigami 2.20 as Kirigami
import ".."

// Kokoro TTS config pane — filter field + CatalogList over the Kokoro
// voice catalog. Structurally identical to the Piper pane: Kokoro is a
// single-bundle engine, so the catalog lists all 54 bundled voices and
// installing any one of them fetches the shared `kokoro-v1.0.onnx` +
// `voices-v1.0.bin` pair (~350 MB total, once). After that every voice
// resolves as installed. Decoupled from `voiceAgent` directly: the
// parent (MainWindow's Loader) injects the backend controller as
// `voiceAgent` and a two-way `filterText` binding so this pane stays
// unit-testable against stub data.
ColumnLayout {
    id: kokoroPane

    // Backend controller (Python `MainWindow`). The parent Loader sets
    // this in onLoaded; defaults to null so the pane fails-soft if a
    // future test mounts it bare.
    property var voiceAgent: null

    // Live filter string. The parent owns the canonical value (so STT
    // and TTS tabs can each persist their own filter across pane swaps);
    // we mirror it here and write back through the binding when the
    // TextField changes.
    property string filterText: ""

    Layout.leftMargin: Kirigami.Units.largeSpacing
    Layout.rightMargin: Kirigami.Units.largeSpacing
    Layout.topMargin: Kirigami.Units.mediumSpacing
    Layout.bottomMargin: Kirigami.Units.largeSpacing
    spacing: Kirigami.Units.smallSpacing

    function tr(text) {
        if (typeof i18nCtx !== "undefined" && i18nCtx) {
            return i18nCtx.i18n(text);
        }
        return text;
    }

    readonly property bool hasVoiceAgent: kokoroPane.voiceAgent !== null && kokoroPane.voiceAgent !== undefined

    Kirigami.InlineMessage {
        Layout.fillWidth: true
        visible: true
        type: Kirigami.MessageType.Information
        text: kokoroPane.tr("Installing any voice downloads the shared Kokoro model (~350 MB once); all voices become available after that.")
    }

    TextField {
        Layout.fillWidth: true
        placeholderText: kokoroPane.tr("Filter TTS voices")
        text: kokoroPane.filterText
        onTextChanged: kokoroPane.filterText = text
    }

    CatalogList {
        id: ttsCatalogView
        catalogModel: kokoroPane.hasVoiceAgent ? kokoroPane.voiceAgent.ttsCatalogModel : null
        filterText: kokoroPane.filterText
        selectedName: kokoroPane.hasVoiceAgent ? kokoroPane.voiceAgent.selectedTtsModel : ""
        onSelect: function(name) { if (kokoroPane.hasVoiceAgent) kokoroPane.voiceAgent.selectTtsModel(name); }
        onInstall: function(name) { if (kokoroPane.hasVoiceAgent) kokoroPane.voiceAgent.installTtsModel(name); }
        onRemove: function(name) { if (kokoroPane.hasVoiceAgent) kokoroPane.voiceAgent.deleteTtsModel(name); }
    }
}
