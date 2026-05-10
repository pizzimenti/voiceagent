import QtQuick
import QtCore
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import org.kde.kirigami 2.20 as Kirigami
import ".."

// Chatterbox TTS config pane — voice-cloning-only engine with no
// built-in voices. The pane includes a reference-clip setup row
// (Record / Import) above the filter + catalog. The catalog itself
// lists user-supplied reference clips, since for Chatterbox a
// "voice" === a reference clip.
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
        text: chatterboxPane.tr("Chatterbox is voice-cloning only — no built-in voices. Record a reference clip from your microphone, or import an existing audio file.")
    }

    // Model precision selector. Each variant downloads independently
    // (HF cache keys per filename) so flipping among them is free
    // once each has been fetched. q4 is smallest / fastest-load;
    // q4f16 typically the fastest at inference on x86 with F16C+AVX-512;
    // fp16 a balanced option; fp32 reference-quality but largest.
    // Voice-clone pitch fidelity in particular benefits from higher
    // precision because pitch lives in the high-frequency tail of
    // the speaker embedding that aggressive quantization rounds away.
    RowLayout {
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing

        Label {
            text: chatterboxPane.tr("Model precision:")
            Layout.alignment: Qt.AlignVCenter
        }

        // dtype → human-readable label with approximate full-bundle
        // size. Sizes track _APPROX_BUNDLE_BYTES on the Python side
        // (chatterbox_tts.py) — keep them in sync if upstream sizes
        // shift.
        function _dtypeLabel(dtype) {
            switch (dtype) {
                case "q4":    return chatterboxPane.tr("q4 (~700 MB)");
                case "q4f16": return chatterboxPane.tr("q4f16 (~750 MB)");
                case "fp16":  return chatterboxPane.tr("fp16 (~1.4 GB)");
                case "fp32":  return chatterboxPane.tr("fp32 (~2.5 GB)");
                default:      return dtype;
            }
        }

        ComboBox {
            id: dtypeSelector
            Layout.fillWidth: true
            // ComboBox with a textRole-friendly model: the model contains
            // raw dtype names; the delegate + display rendering use the
            // `_dtypeLabel` helper so labels include size disclosure
            // without changing the value the Slot receives.
            model: chatterboxPane.hasVoiceAgent
                ? chatterboxPane.voiceAgent.chatterboxDtypeOptions
                : []
            currentIndex: {
                if (!chatterboxPane.hasVoiceAgent) return -1;
                var opts = chatterboxPane.voiceAgent.chatterboxDtypeOptions;
                var sel = chatterboxPane.voiceAgent.selectedChatterboxDtype;
                for (var i = 0; i < opts.length; i++) {
                    if (opts[i] === sel) return i;
                }
                return 0;
            }
            // Disable while a download is in flight — switching dtype
            // mid-download would orphan the worker on a wrong-variant
            // graph. Re-enabled once the engine state settles.
            enabled: chatterboxPane.hasVoiceAgent
                && !chatterboxPane.voiceAgent.chatterboxEngineDownloading
            displayText: parent._dtypeLabel(currentText)
            delegate: ItemDelegate {
                width: dtypeSelector.width
                contentItem: Label {
                    text: dtypeSelector.parent._dtypeLabel(modelData)
                    elide: Text.ElideRight
                    verticalAlignment: Text.AlignVCenter
                }
                highlighted: dtypeSelector.highlightedIndex === index
            }
            onActivated: {
                if (chatterboxPane.hasVoiceAgent) {
                    chatterboxPane.voiceAgent.selectChatterboxDtype(currentText);
                }
            }
        }
    }

    // Engine-state banner. Three visual states:
    //   1. Model NOT downloaded → warning banner + "Download model" button
    //      with the ~700 MB size disclosure.
    //   2. Model DOWNLOADING → information banner + progress bar.
    //   3. Model READY → positive banner (low-prominence; the catalog
    //      voices below are the real interaction surface once the
    //      engine is set up).
    //
    // The model is shared across all reference voices in the catalog,
    // which is why this affordance is engine-level (above the catalog)
    // rather than per-voice.
    Kirigami.InlineMessage {
        Layout.fillWidth: true
        visible: chatterboxPane.hasVoiceAgent
            && !chatterboxPane.voiceAgent.chatterboxEngineReady
            && !chatterboxPane.voiceAgent.chatterboxEngineDownloading
        type: Kirigami.MessageType.Warning
        text: {
            var dtype = chatterboxPane.hasVoiceAgent
                ? chatterboxPane.voiceAgent.selectedChatterboxDtype
                : "q4";
            var size = "~700 MB";
            if (dtype === "q4f16") size = "~750 MB";
            else if (dtype === "fp16") size = "~1.4 GB";
            else if (dtype === "fp32") size = "~2.5 GB";
            return chatterboxPane.tr(
                "Chatterbox model not downloaded — synthesis will fail until the %1 ONNX bundle is fetched."
            ).replace("%1", size);
        }
        actions: [
            Kirigami.Action {
                text: chatterboxPane.tr("Download model")
                icon.name: "download"
                onTriggered: {
                    if (chatterboxPane.hasVoiceAgent) {
                        chatterboxPane.voiceAgent.downloadChatterboxModel();
                    }
                }
            }
        ]
    }

    // Downloading state — uses a Frame instead of Kirigami.InlineMessage
    // because InlineMessage is designed for text + action-button rows.
    // Putting a ProgressBar inside its content slot collapsed the bar
    // to a hairline + left tall whitespace below the message. A plain
    // Frame with a ColumnLayout inside renders correctly and still
    // looks integrated with the surrounding UI.
    Frame {
        Layout.fillWidth: true
        visible: chatterboxPane.hasVoiceAgent
            && chatterboxPane.voiceAgent.chatterboxEngineDownloading
        padding: Kirigami.Units.largeSpacing

        contentItem: ColumnLayout {
            spacing: Kirigami.Units.smallSpacing

            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: {
                    if (!chatterboxPane.hasVoiceAgent) {
                        return chatterboxPane.tr("Downloading Chatterbox model…");
                    }
                    var pct = Math.round(
                        chatterboxPane.voiceAgent.chatterboxEngineDownloadProgress * 100
                    ).toString();
                    var bytes = chatterboxPane.voiceAgent.chatterboxEngineDownloadProgressLabel;
                    if (bytes && bytes !== "") {
                        return chatterboxPane.tr(
                            "Downloading Chatterbox model… %1% — %2"
                        ).replace("%1", pct).replace("%2", bytes);
                    }
                    return chatterboxPane.tr(
                        "Downloading Chatterbox model… %1%"
                    ).replace("%1", pct);
                }
            }

            ProgressBar {
                Layout.fillWidth: true
                from: 0.0
                to: 1.0
                value: chatterboxPane.hasVoiceAgent
                    ? chatterboxPane.voiceAgent.chatterboxEngineDownloadProgress
                    : 0.0
            }
        }
    }

    Kirigami.InlineMessage {
        Layout.fillWidth: true
        visible: chatterboxPane.hasVoiceAgent
            && chatterboxPane.voiceAgent.chatterboxEngineReady
            && !chatterboxPane.voiceAgent.chatterboxEngineDownloading
        type: Kirigami.MessageType.Positive
        text: chatterboxPane.tr("Chatterbox model ready.")
    }

    RowLayout {
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing

        Button {
            text: chatterboxPane.tr("Record from mic…")
            icon.name: "audio-input-microphone"
            enabled: chatterboxPane.hasVoiceAgent
                && !chatterboxPane.voiceAgent.chatterboxRecordingActive
            onClicked: {
                recordingNameField.text = "";
                recordingDialog.errorText = "";
                recordingDialog.open();
            }
        }

        Button {
            text: chatterboxPane.tr("Import audio file…")
            icon.name: "document-open"
            onClicked: chatterboxImportDialog.open()
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
        // StandardPaths.standardLocations() can return an empty list on
        // stripped/sandboxed runtimes (minimal containers, some Flatpak
        // setups). Indexing [0] on empty yields `undefined`, which
        // leaves currentFolder unset and the dialog opens at an
        // unpredictable cwd. Fall back to HomeLocation, then empty
        // string as a last resort so Qt picks a default.
        currentFolder: {
            const music = StandardPaths.standardLocations(StandardPaths.MusicLocation);
            if (music && music.length > 0) return music[0];
            const home = StandardPaths.standardLocations(StandardPaths.HomeLocation);
            return (home && home.length > 0) ? home[0] : "";
        }
        nameFilters: [
            chatterboxPane.tr("Audio files (*.wav *.mp3 *.flac *.ogg *.m4a *.aac)"),
            chatterboxPane.tr("All files (*)"),
        ]
        onAccepted: {
            if (!chatterboxPane.hasVoiceAgent) return;
            var url = String(selectedFile);
            var basename = url.substring(url.lastIndexOf("/") + 1);
            var dot = basename.lastIndexOf(".");
            var stem = dot > 0 ? basename.substring(0, dot) : basename;
            chatterboxPane.voiceAgent.importChatterboxReference(url, stem);
            // No special handling on success — the catalog refresh
            // happens inside the Slot via ui_changed.
        }
    }

    // Mic-recording session dialog.
    //
    // Two visual phases:
    //   - PRE-RECORD: name TextField + Start button + Cancel.
    //   - RECORDING: name read-only label, ProgressBar bound to
    //     `chatterboxRecordingProgress`, Stop button (saves whatever
    //     was captured up to that point), Cancel (no-op visually —
    //     the dialog stays open until the worker emits its finished
    //     signal so we can surface errors).
    //
    // The worker auto-stops at the requested duration (15 s by
    // default). On the finished signal handler in window.py the
    // dialog auto-closes via the Connections block below.
    Dialog {
        id: recordingDialog
        title: chatterboxPane.tr("Record reference voice")
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.NoButton
        closePolicy: Popup.NoAutoClose

        property string errorText: ""
        readonly property real recordingSeconds: 60.0
        readonly property bool isRecording: chatterboxPane.hasVoiceAgent
            && chatterboxPane.voiceAgent.chatterboxRecordingActive

        contentItem: ColumnLayout {
            spacing: Kirigami.Units.largeSpacing

            Label {
                Layout.fillWidth: true
                Layout.preferredWidth: Kirigami.Units.gridUnit * 24
                wrapMode: Text.WordWrap
                text: recordingDialog.isRecording
                    ? chatterboxPane.tr("Recording — speak now. Auto-stops in %1 seconds.").replace(
                        "%1", String(recordingDialog.recordingSeconds))
                    : chatterboxPane.tr("Choose a name for the new voice. Recording starts when you click Start and runs for up to %1 seconds.").replace(
                        "%1", String(recordingDialog.recordingSeconds))
            }

            // PRE-RECORD: editable name input
            TextField {
                id: recordingNameField
                Layout.fillWidth: true
                placeholderText: chatterboxPane.tr("voice name (e.g. my-voice)")
                visible: !recordingDialog.isRecording
                enabled: !recordingDialog.isRecording
                onAccepted: startButton.clicked()
            }

            // RECORDING: read-only name label
            Label {
                Layout.fillWidth: true
                visible: recordingDialog.isRecording
                text: chatterboxPane.tr("Voice: %1").replace(
                    "%1", chatterboxPane.hasVoiceAgent
                        ? chatterboxPane.voiceAgent.chatterboxRecordingName
                        : "")
                font.italic: true
            }

            ProgressBar {
                Layout.fillWidth: true
                from: 0.0
                to: 1.0
                value: chatterboxPane.hasVoiceAgent
                    ? chatterboxPane.voiceAgent.chatterboxRecordingProgress
                    : 0.0
                visible: recordingDialog.isRecording
            }

            Label {
                Layout.fillWidth: true
                visible: recordingDialog.errorText !== ""
                color: Kirigami.Theme.negativeTextColor
                wrapMode: Text.WordWrap
                text: recordingDialog.errorText
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Kirigami.Units.smallSpacing

                Item { Layout.fillWidth: true }

                Button {
                    text: recordingDialog.isRecording
                        ? chatterboxPane.tr("Stop")
                        : chatterboxPane.tr("Cancel")
                    onClicked: {
                        if (recordingDialog.isRecording) {
                            // Stop early; worker saves the captured prefix.
                            if (chatterboxPane.hasVoiceAgent) {
                                chatterboxPane.voiceAgent.cancelChatterboxRecording();
                            }
                        } else {
                            recordingDialog.close();
                        }
                    }
                }

                Button {
                    id: startButton
                    text: chatterboxPane.tr("Start")
                    visible: !recordingDialog.isRecording
                    enabled: !recordingDialog.isRecording
                        && recordingNameField.text.trim().length > 0
                        && chatterboxPane.hasVoiceAgent
                    onClicked: {
                        recordingDialog.errorText = "";
                        chatterboxPane.voiceAgent.startChatterboxRecording(
                            recordingNameField.text.trim(),
                            recordingDialog.recordingSeconds,
                        );
                    }
                }
            }
        }

        // Auto-close when the worker reports finished. The recording
        // state transition (chatterboxRecordingActive: true → false)
        // is the trigger; we react in onIsRecordingChanged via a
        // tracker. Errors land in window.py's conversation log.
        property bool _wasRecording: false
        onIsRecordingChanged: {
            if (recordingDialog._wasRecording && !recordingDialog.isRecording) {
                // Recording just finished. Close the dialog. The
                // catalog model refresh + voice selection both happen
                // inside the worker's finished-signal handler.
                recordingDialog.close();
            }
            recordingDialog._wasRecording = recordingDialog.isRecording;
        }
    }
}
