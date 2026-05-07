import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.kde.kirigami 2.20 as Kirigami
import ".."

// Piper TTS config pane — hosts the filter field and CatalogList for the
// Piper voice catalog. Decoupled from `voiceAgent` directly: the parent
// (MainWindow's Loader) injects the backend controller as `voiceAgent`
// and a two-way `filterText` binding so this pane stays unit-testable
// against stub data.
ColumnLayout {
    id: piperPane

    // Backend controller (Python `MainWindow`). The parent Loader sets
    // this in onLoaded; defaults to null so the pane fails-soft if a
    // future test mounts it bare.
    property var voiceAgent: null

    // Live filter string. The parent owns the canonical value (so
    // Speech-To-Text and Text-To-Speech tabs can each have their own
    // filter persisted across pane swaps); we mirror it here and
    // write back through the binding when the TextField changes.
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

    readonly property bool hasVoiceAgent: piperPane.voiceAgent !== null && piperPane.voiceAgent !== undefined

    TextField {
        Layout.fillWidth: true
        placeholderText: piperPane.tr("Filter TTS voices")
        text: piperPane.filterText
        onTextChanged: piperPane.filterText = text
    }

    CatalogList {
        id: ttsCatalogView
        catalogModel: piperPane.hasVoiceAgent ? piperPane.voiceAgent.ttsCatalogModel : null
        filterText: piperPane.filterText
        selectedName: piperPane.hasVoiceAgent ? piperPane.voiceAgent.selectedTtsModel : ""
        onSelect: function(name) { if (piperPane.hasVoiceAgent) piperPane.voiceAgent.selectTtsModel(name); }
        onInstall: function(name) { if (piperPane.hasVoiceAgent) piperPane.voiceAgent.installTtsModel(name); }
        onRemove: function(name) { if (piperPane.hasVoiceAgent) piperPane.voiceAgent.deleteTtsModel(name); }
    }
}
