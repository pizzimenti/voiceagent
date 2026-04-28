import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.kde.kirigami 2.20 as Kirigami

// Session Setup pane shown above the conversation in medium mode. Hosts
// the STT / TTS selectors, LLM URL + connect button, loaded-model
// selector, and the medium-mode pulsing mic frame, plus model / tts
// loading progress rows.
//
// This component takes voiceAgent + the few responsive-mode predicates +
// mic colors as required properties rather than reaching back through
// ApplicationWindow.window. Internal `voiceAgent.*` references use the
// `voiceAgent ? ... : fallback` ternary form for the same nested-binding
// timing reason documented on MicButton / ConversationPane.
//
// The label/control pairs use `Kirigami.FormLayout` (the documented
// Kirigami pattern for settings/control groups). Compact-mode collapse
// is driven by `Kirigami.FormLayout.wideMode: !compactMode` — wideMode
// false stacks each label above its control on one column; wideMode
// true puts label-and-control side-by-side. The medium-mode mic frame
// sits next to the form via the surrounding RowLayout, replacing the
// previous GridLayout's `Layout.row/Layout.column/Layout.rowSpan`
// hand-positioning.
Pane {
    id: sessionPane

    // Required injection of the backend controller.
    required property var voiceAgent

    // Responsive-mode predicates from the parent ApplicationWindow.
    required property bool compactMode
    required property bool mediumMode

    // Mic-frame inputs (medium-mode only).
    required property bool micPulseActive
    required property color micButtonColor
    required property color micPulseColor

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
            text: i18nCtx.i18n("Session Setup")
            level: 2
        }

        Pane {
            id: sessionSetupPaneInner
            Layout.fillWidth: true
            padding: Kirigami.Units.smallSpacing
            implicitHeight: sessionSetupRow.implicitHeight + padding * 2

            RowLayout {
                id: sessionSetupRow
                width: parent.width
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
                        Kirigami.FormData.label: i18nCtx.i18n("Speech:")
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 14
                        model: sessionPane.voiceAgent ? sessionPane.voiceAgent.sttOptions : []
                        currentIndex: sessionPane.voiceAgent
                            ? sessionPane._stringIndex(sessionPane.voiceAgent.sttOptions, sessionPane.voiceAgent.selectedSttModel)
                            : -1
                        displayText: currentIndex >= 0 ? currentText : i18nCtx.i18n("No installed STT models")
                        onActivated: {
                            if (sessionPane.voiceAgent) {
                                sessionPane.voiceAgent.selectSttModel(currentText);
                            }
                        }
                    }

                    ComboBox {
                        id: ttsSelector
                        Kirigami.FormData.label: i18nCtx.i18n("Voice:")
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 14
                        model: sessionPane.voiceAgent ? sessionPane.voiceAgent.ttsOptions : []
                        currentIndex: sessionPane.voiceAgent
                            ? sessionPane._stringIndex(sessionPane.voiceAgent.ttsOptions, sessionPane.voiceAgent.selectedTtsModel)
                            : -1
                        displayText: currentIndex >= 0 ? currentText : i18nCtx.i18n("No installed TTS voices")
                        onActivated: {
                            if (sessionPane.voiceAgent) {
                                sessionPane.voiceAgent.selectTtsModel(currentText);
                            }
                        }
                    }

                    RowLayout {
                        Kirigami.FormData.label: i18nCtx.i18n("LLM URL:")
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing

                        ComboBox {
                            id: llmUrlBox
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
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
                        Kirigami.FormData.label: i18nCtx.i18n("Loaded Model:")
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
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
                        displayText: currentIndex <= 0 ? i18nCtx.i18n("Select a loaded model") : currentText
                        onActivated: {
                            if (sessionPane.voiceAgent) {
                                sessionPane.voiceAgent.selectLlmModel(currentText);
                            }
                        }
                    }
                }

                MicButtonFrame {
                    id: mediumMicButtonFrame
                    visible: sessionPane.mediumMode
                    Layout.fillHeight: true
                    Layout.minimumWidth: Kirigami.Units.gridUnit * 9
                    Layout.preferredWidth: Kirigami.Units.gridUnit * 10
                    voiceAgent: sessionPane.voiceAgent
                    iconSize: 34
                    fontPixel: 11
                    borderWidth: 3
                    buttonColor: sessionPane.micButtonColor
                    pulseColor: sessionPane.micPulseColor
                    pulseActive: sessionPane.micPulseActive
                    animatePulse: true
                    // Initial glow values are overwritten by the
                    // SequentialAnimation as soon as it starts.
                    glowOpacity: sessionPane.micPulseActive ? 0.5 : 0.2
                    glowScale: 1.0
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
