import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.kde.kirigami 2.20 as Kirigami

// Session Setup pane shown above the conversation in medium mode. Hosts
// the STT / TTS selectors, LLM URL + connect button, loaded-model
// selector, plus model / tts loading progress rows. The medium-mode
// mic button is rendered at page level by MainWindow.qml so its
// geometry can animate across the compact ↔ medium breakpoint flip.
// `micAnchor` here reserves the layout slot the mic occupies in this
// pane; the actual MicButtonFrame floats over the anchor at page level.
//
// Compact-mode collapse is driven by `Kirigami.FormLayout.wideMode`:
// wideMode false stacks each label above its control on one column;
// wideMode true puts label-and-control side-by-side.
Pane {
    id: sessionPane

    // Required injection of the backend controller.
    required property var voiceAgent

    // Responsive-mode predicates from the parent ApplicationWindow.
    required property bool compactMode
    required property bool mediumMode

    // Exposed so MainWindow.qml's page-level mic can bind its geometry
    // to this pane's reserved mic slot via Loader.item.micAnchor.
    property alias micAnchor: micAnchorItem

    function tr(text) {
        if (typeof i18nCtx !== "undefined" && i18nCtx) {
            return i18nCtx.i18n(text);
        }
        return text;
    }

    function _stringIndex(options, value) {
        for (let i = 0; i < options.length; i += 1) {
            if (options[i] === value) {
                return i;
            }
        }
        return -1;
    }

    implicitHeight: sessionContent.implicitHeight + padding * 2
    padding: sessionPane.compactMode ? Kirigami.Units.smallSpacing : (sessionPane.mediumMode ? Kirigami.Units.smallSpacing : Kirigami.Units.mediumSpacing)

    ColumnLayout {
        id: sessionContent
        anchors.fill: parent
        spacing: Kirigami.Units.mediumSpacing

        Kirigami.Heading {
            text: sessionPane.tr("Session Setup")
            level: 2
        }

        Pane {
            id: sessionSetupPaneInner
            Layout.fillWidth: true
            padding: Kirigami.Units.smallSpacing
            implicitHeight: sessionSetupRow.implicitHeight + padding * 2

            RowLayout {
                id: sessionSetupRow
                // anchors.fill respects the Pane's padding (contentItem
                // bounds); `width: parent.width` overshoots into the
                // padding region and lets children render flush against
                // the window edge. That manifested as form labels being
                // left-clipped because the Pane's left padding wasn't
                // honored.
                anchors.fill: parent
                spacing: Kirigami.Units.largeSpacing

                Kirigami.FormLayout {
                    id: sessionSetupForm
                    Layout.fillWidth: true
                    // wideMode true keeps the label-and-control on one
                    // line (medium mode); wideMode false collapses to
                    // a label-on-top-of-control stack (compact mode).
                    wideMode: !sessionPane.compactMode

                    ComboBox {
                        id: sttSelector
                        Kirigami.FormData.label: sessionPane.tr("Speech:")
                        Layout.fillWidth: false
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 14
                        model: sessionPane.voiceAgent ? sessionPane.voiceAgent.sttOptions : []
                        currentIndex: sessionPane.voiceAgent
                            ? sessionPane._stringIndex(sessionPane.voiceAgent.sttOptions, sessionPane.voiceAgent.selectedSttModel)
                            : -1
                        displayText: currentIndex >= 0 ? currentText : sessionPane.tr("No installed STT models")
                        onActivated: {
                            if (sessionPane.voiceAgent) {
                                sessionPane.voiceAgent.selectSttModel(currentText);
                            }
                        }
                    }

                    ComboBox {
                        id: ttsEngineSelector
                        Kirigami.FormData.label: sessionPane.tr("Engine:")
                        Layout.fillWidth: false
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 14
                        model: sessionPane.voiceAgent ? sessionPane.voiceAgent.ttsEngineOptions : []
                        currentIndex: sessionPane.voiceAgent
                            ? sessionPane._stringIndex(sessionPane.voiceAgent.ttsEngineOptions, sessionPane.voiceAgent.selectedTtsEngine)
                            : -1
                        onActivated: {
                            if (sessionPane.voiceAgent) {
                                sessionPane.voiceAgent.selectTtsEngine(currentText);
                            }
                        }
                    }

                    ComboBox {
                        id: ttsSelector
                        Kirigami.FormData.label: sessionPane.tr("Voice:")
                        Layout.fillWidth: false
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 14
                        model: sessionPane.voiceAgent ? sessionPane.voiceAgent.ttsOptions : []
                        currentIndex: sessionPane.voiceAgent
                            ? sessionPane._stringIndex(sessionPane.voiceAgent.ttsOptions, sessionPane.voiceAgent.selectedTtsModel)
                            : -1
                        displayText: currentIndex >= 0 ? currentText : sessionPane.tr("No installed TTS voices")
                        onActivated: {
                            if (sessionPane.voiceAgent) {
                                sessionPane.voiceAgent.selectTtsModel(currentText);
                            }
                        }
                    }

                    RowLayout {
                        Kirigami.FormData.label: sessionPane.tr("LLM URL:")
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing

                        ComboBox {
                            id: llmUrlBox
                            Layout.fillWidth: false
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 16
                            editable: sessionPane.voiceAgent
                                ? (!sessionPane.voiceAgent.llmServerConnected && !sessionPane.voiceAgent.llmModelBusy)
                                : false
                            enabled: sessionPane.voiceAgent
                                ? (!sessionPane.voiceAgent.llmServerConnected && !sessionPane.voiceAgent.llmModelBusy)
                                : false
                            model: sessionPane.voiceAgent ? sessionPane.voiceAgent.llmUrls : []
                            currentIndex: sessionPane.voiceAgent
                                ? sessionPane._stringIndex(sessionPane.voiceAgent.llmUrls, sessionPane.voiceAgent.currentLlmUrl)
                                : -1
                            Component.onCompleted: {
                                if (sessionPane.voiceAgent) {
                                    editText = sessionPane.voiceAgent.currentLlmUrl;
                                }
                            }
                            onAccepted: {
                                if (sessionPane.voiceAgent) {
                                    sessionPane.voiceAgent.setCurrentLlmUrl(editText);
                                    sessionPane.voiceAgent.persistCurrentLlmUrl();
                                }
                            }
                            onActivated: {
                                if (sessionPane.voiceAgent) {
                                    sessionPane.voiceAgent.setCurrentLlmUrl(currentText);
                                }
                            }
                        }

                        Button {
                            Layout.minimumWidth: Kirigami.Units.gridUnit * 9
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 10
                            text: sessionPane.voiceAgent ? sessionPane.voiceAgent.llmConnectionButtonText : ""
                            // Always require !llmConnectionBusy. The previous
                            // `(!llmServerConnected || !llmConnectionBusy)` clause
                            // let the user spam-click Connect while a refresh
                            // was already in flight, queueing extra
                            // _start_refresh() calls in LlmController.
                            enabled: sessionPane.voiceAgent
                                ? (!!llmUrlBox.editText.trim()
                                    && !sessionPane.voiceAgent.llmModelBusy
                                    && !sessionPane.voiceAgent.llmConnectionBusy)
                                : false
                            onClicked: {
                                if (sessionPane.voiceAgent) {
                                    sessionPane.voiceAgent.toggleLlmServerConnection(llmUrlBox.editText);
                                }
                            }
                        }
                    }

                    ComboBox {
                        Kirigami.FormData.label: sessionPane.tr("Loaded Model:")
                        Layout.fillWidth: false
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 16
                        enabled: sessionPane.voiceAgent
                            ? (sessionPane.voiceAgent.llmServerConnected
                                && !sessionPane.voiceAgent.llmConnectionBusy
                                && !sessionPane.voiceAgent.llmModelBusy)
                            : false
                        model: sessionPane.voiceAgent ? sessionPane.voiceAgent.llmModelOptions : []
                        currentIndex: sessionPane.voiceAgent
                            ? sessionPane._stringIndex(sessionPane.voiceAgent.llmModelOptions, sessionPane.voiceAgent.selectedLlmModel)
                            : -1
                        // `<= 0` (NOT `< 0`) because `llmModelOptions` is
                        // `["", *self._llm.models]` — index 0 is the empty-
                        // string sentinel for "no LLM loaded". Showing
                        // `currentText` for index 0 would render an empty
                        // string instead of the placeholder. The other
                        // combos (STT/TTS) use `< 0` because their option
                        // lists do not carry a leading "" sentinel.
                        displayText: currentIndex <= 0 ? sessionPane.tr("Select a loaded model") : currentText
                        onActivated: {
                            if (sessionPane.voiceAgent) {
                                sessionPane.voiceAgent.selectLlmModel(currentText);
                            }
                        }
                    }
                }

                Item {
                    id: micAnchorItem
                    objectName: "micAnchor"
                    visible: sessionPane.mediumMode
                    Layout.fillHeight: true
                    Layout.minimumWidth: Kirigami.Units.gridUnit * 9
                    Layout.preferredWidth: Kirigami.Units.gridUnit * 10
                }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing

            ProgressBar {
                Layout.fillWidth: true
                visible: sessionPane.voiceAgent ? sessionPane.voiceAgent.modelLoading : false
                from: 0
                to: 1
                indeterminate: sessionPane.voiceAgent ? sessionPane.voiceAgent.modelProgressIndeterminate : false
                value: sessionPane.voiceAgent ? sessionPane.voiceAgent.modelProgressValue : 0
            }

            Label {
                Layout.fillWidth: true
                visible: sessionPane.voiceAgent ? sessionPane.voiceAgent.modelLoading : false
                text: sessionPane.voiceAgent ? sessionPane.voiceAgent.modelProgressText : ""
                wrapMode: Text.WordWrap
                color: Kirigami.Theme.disabledTextColor
            }

            ProgressBar {
                Layout.fillWidth: true
                visible: sessionPane.voiceAgent ? sessionPane.voiceAgent.ttsLoading : false
                from: 0
                to: 1
                indeterminate: sessionPane.voiceAgent ? sessionPane.voiceAgent.ttsProgressIndeterminate : false
                value: sessionPane.voiceAgent ? sessionPane.voiceAgent.ttsProgressValue : 0
            }

            Label {
                Layout.fillWidth: true
                visible: sessionPane.voiceAgent ? sessionPane.voiceAgent.ttsLoading : false
                text: sessionPane.voiceAgent ? sessionPane.voiceAgent.ttsProgressText : ""
                wrapMode: Text.WordWrap
                color: Kirigami.Theme.disabledTextColor
            }
        }
    }
}
