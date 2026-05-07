import QtQuick
import QtCore
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import org.kde.kirigami 2.20 as Kirigami
import ".."

// Chatterbox TTS config pane — voice-cloning-only engine with no
// built-in voices, so the pane includes a reference-clip setup row
// (Record / Import / Use bundled default) above the filter + catalog.
// The catalog itself lists imported reference clips, since for
// Chatterbox a "voice" === a reference clip.
//
// Tunable parameter sliders (cfg_weight, exaggeration) will land here
// once the corresponding Slots exist on `voiceAgent`. See the
// "Future: tunable parameter sliders" block below.
ColumnLayout {
    id: chatterboxPane

    // Backend controller (Python `MainWindow`).
    property var voiceAgent: null

    // Live filter string mirrored from the parent.
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

    readonly property bool hasVoiceAgent: chatterboxPane.voiceAgent !== null && chatterboxPane.voiceAgent !== undefined

    Kirigami.InlineMessage {
        Layout.fillWidth: true
        visible: true
        type: Kirigami.MessageType.Information
        text: chatterboxPane.tr("Chatterbox is voice-cloning only — no built-in voices. Add a reference clip below, or load the bundled default voice.")
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing

        Button {
            text: chatterboxPane.tr("Record from mic…")
            icon.name: "audio-input-microphone"
            enabled: false  // option A — wired in v0.12.1
            ToolTip.visible: hovered
            ToolTip.text: chatterboxPane.tr("Microphone-record reference clip — coming in v0.12.1")
        }

        Button {
            text: chatterboxPane.tr("Import audio file…")
            icon.name: "document-open"
            onClicked: chatterboxImportDialog.open()
        }

        Button {
            text: chatterboxPane.tr("Use bundled default")
            icon.name: "audio-volume-medium"
            onClicked: {
                if (chatterboxPane.hasVoiceAgent) {
                    var saved = chatterboxPane.voiceAgent.useChatterboxBundledDefault();
                    if (saved !== "") {
                        // tts catalog refreshes via the Slot's
                        // ui_changed.emit().
                    }
                }
            }
        }
    }

    // Future: tunable parameter sliders.
    // Once `voiceAgent` exposes Slots like `setChatterboxCfgWeight(value)`
    // and `setChatterboxExaggeration(value)`, drop two Kirigami.FormLayout
    // rows here:
    //   - cfg_weight slider (0.0 – 1.0, default 0.5) — controls how
    //     closely the synthesis adheres to the reference clip's tone.
    //   - exaggeration slider (0.0 – 2.0, default 1.0) — amplifies
    //     prosodic variation; values > 1.0 push toward dramatic /
    //     expressive delivery.
    // No backend Slots yet, so leaving the sliders unwired would create
    // dead UI. Block intentionally empty until the Slots land.

    TextField {
        Layout.fillWidth: true
        placeholderText: chatterboxPane.tr("Filter TTS voices")
        text: chatterboxPane.filterText
        onTextChanged: chatterboxPane.filterText = text
    }

    CatalogList {
        id: ttsCatalogView
        catalogModel: chatterboxPane.hasVoiceAgent ? chatterboxPane.voiceAgent.ttsCatalogModel : null
        filterText: chatterboxPane.filterText
        selectedName: chatterboxPane.hasVoiceAgent ? chatterboxPane.voiceAgent.selectedTtsModel : ""
        onSelect: function(name) { if (chatterboxPane.hasVoiceAgent) chatterboxPane.voiceAgent.selectTtsModel(name); }
        onInstall: function(name) { if (chatterboxPane.hasVoiceAgent) chatterboxPane.voiceAgent.installTtsModel(name); }
        onRemove: function(name) { if (chatterboxPane.hasVoiceAgent) chatterboxPane.voiceAgent.deleteTtsModel(name); }
    }

    FileDialog {
        id: chatterboxImportDialog
        title: chatterboxPane.tr("Choose a reference audio file")
        currentFolder: StandardPaths.standardLocations(StandardPaths.MusicLocation)[0]
        nameFilters: [
            chatterboxPane.tr("Audio files (*.wav *.mp3 *.flac *.ogg *.m4a *.aac)"),
            chatterboxPane.tr("All files (*)"),
        ]
        onAccepted: {
            if (!chatterboxPane.hasVoiceAgent) return;
            var url = String(selectedFile);
            // Derive a default name from the file's basename.
            var basename = url.substring(url.lastIndexOf("/") + 1);
            var dot = basename.lastIndexOf(".");
            var stem = dot > 0 ? basename.substring(0, dot) : basename;
            var saved = chatterboxPane.voiceAgent.importChatterboxReference(url, stem);
            // No special handling on success — the catalog refresh
            // happens inside the Slot via ui_changed.
        }
    }
}
