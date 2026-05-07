import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.kde.kirigami 2.20 as Kirigami
import ".."

// Whisper STT config pane — hosts the filter field and CatalogList for
// the faster-whisper STT model catalog. Same shape as the Piper TTS
// pane: parent Loader injects `voiceAgent` and a two-way `filterText`
// binding.
ColumnLayout {
    id: whisperPane

    // Backend controller (Python `MainWindow`). The parent Loader sets
    // this in onLoaded; defaults to null so the pane fails-soft if a
    // future test mounts it bare.
    property var voiceAgent: null

    // Live filter string mirrored from the parent so the STT tab keeps
    // its filter independent of the TTS tab.
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

    readonly property bool hasVoiceAgent: whisperPane.voiceAgent !== null && whisperPane.voiceAgent !== undefined

    TextField {
        Layout.fillWidth: true
        placeholderText: whisperPane.tr("Filter STT models")
        text: whisperPane.filterText
        onTextChanged: whisperPane.filterText = text
    }

    CatalogList {
        id: sttCatalogView
        catalogModel: whisperPane.hasVoiceAgent ? whisperPane.voiceAgent.sttCatalogModel : null
        filterText: whisperPane.filterText
        selectedName: whisperPane.hasVoiceAgent ? whisperPane.voiceAgent.selectedSttModel : ""
        onSelect: function(name) { if (whisperPane.hasVoiceAgent) whisperPane.voiceAgent.selectSttModel(name); }
        onInstall: function(name) { if (whisperPane.hasVoiceAgent) whisperPane.voiceAgent.installSttModel(name); }
        onRemove: function(name) { if (whisperPane.hasVoiceAgent) whisperPane.voiceAgent.deleteSttModel(name); }
    }
}
