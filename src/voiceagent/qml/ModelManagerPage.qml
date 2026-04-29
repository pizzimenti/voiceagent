import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.kde.kirigami 2.20 as Kirigami

// Voice-models manager. Pushed onto `pageStack` from the `Voice
// Models` action on the main page, popped via the Close action in
// this page's header. Replaces the prior inline modal `Window` so
// the manager lives in the same QML tree and participates in
// Kirigami's page navigation conventions (header actions, padding,
// breakpoint behavior).
Kirigami.Page {
    id: managerPage

    objectName: "modelManagerPage"

    required property var voiceAgent
    required property bool compactMode
    required property int sttInstalledCount
    required property int ttsInstalledCount

    title: i18nCtx.i18n("Voice Models")
    padding: 0

    property string sttFilter: ""
    property string ttsFilter: ""

    actions: [
        Kirigami.Action {
            text: i18nCtx.i18n("Close")
            icon.name: "window-close"
            displayHint: Kirigami.DisplayHint.IconOnly
            onTriggered: applicationWindow().pageStack.pop()
        }
    ]

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        ColumnLayout {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.largeSpacing
            Layout.rightMargin: Kirigami.Units.largeSpacing
            Layout.topMargin: Kirigami.Units.largeSpacing
            Layout.bottomMargin: Kirigami.Units.mediumSpacing
            spacing: Kirigami.Units.smallSpacing

            Label {
                Layout.fillWidth: true
                text: i18nCtx.i18n("Install, remove, and switch local speech models here. Session selectors only show installed items.")
                wrapMode: Text.WordWrap
                color: Kirigami.Theme.disabledTextColor
            }

            GridLayout {
                Layout.fillWidth: true
                columns: managerPage.compactMode ? 1 : 2
                columnSpacing: Kirigami.Units.largeSpacing

                Label {
                    Layout.fillWidth: true
                    text: i18nCtx.i18n("%1 STT model(s) installed").arg(managerPage.sttInstalledCount)
                    font.weight: Font.DemiBold
                }

                Label {
                    Layout.fillWidth: true
                    text: i18nCtx.i18n("%1 TTS voice(s) installed").arg(managerPage.ttsInstalledCount)
                    font.weight: Font.DemiBold
                }
            }
        }

        TabBar {
            id: managerTabs
            Layout.fillWidth: true

            TabButton { text: i18nCtx.i18n("Speech To Text") }
            TabButton { text: i18nCtx.i18n("Text To Speech") }
        }

        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: managerTabs.currentIndex

            ColumnLayout {
                Layout.leftMargin: Kirigami.Units.largeSpacing
                Layout.rightMargin: Kirigami.Units.largeSpacing
                Layout.topMargin: Kirigami.Units.mediumSpacing
                Layout.bottomMargin: Kirigami.Units.largeSpacing
                spacing: Kirigami.Units.smallSpacing

                TextField {
                    Layout.fillWidth: true
                    placeholderText: i18nCtx.i18n("Filter STT models")
                    text: managerPage.sttFilter
                    onTextChanged: managerPage.sttFilter = text
                }

                CatalogList {
                    id: sttCatalogView
                    catalogModel: managerPage.voiceAgent ? managerPage.voiceAgent.sttCatalogModel : null
                    filterText: managerPage.sttFilter
                    selectedName: managerPage.voiceAgent ? managerPage.voiceAgent.selectedSttModel : ""
                    onSelect: function(name) {
                        if (managerPage.voiceAgent) {
                            managerPage.voiceAgent.selectSttModel(name);
                        }
                    }
                    onInstall: function(name) {
                        if (managerPage.voiceAgent) {
                            managerPage.voiceAgent.installSttModel(name);
                        }
                    }
                    onRemove: function(name) {
                        if (managerPage.voiceAgent) {
                            managerPage.voiceAgent.deleteSttModel(name);
                        }
                    }
                }
            }

            ColumnLayout {
                Layout.leftMargin: Kirigami.Units.largeSpacing
                Layout.rightMargin: Kirigami.Units.largeSpacing
                Layout.topMargin: Kirigami.Units.mediumSpacing
                Layout.bottomMargin: Kirigami.Units.largeSpacing
                spacing: Kirigami.Units.smallSpacing

                TextField {
                    Layout.fillWidth: true
                    placeholderText: i18nCtx.i18n("Filter TTS voices")
                    text: managerPage.ttsFilter
                    onTextChanged: managerPage.ttsFilter = text
                }

                CatalogList {
                    id: ttsCatalogView
                    catalogModel: managerPage.voiceAgent ? managerPage.voiceAgent.ttsCatalogModel : null
                    filterText: managerPage.ttsFilter
                    selectedName: managerPage.voiceAgent ? managerPage.voiceAgent.selectedTtsModel : ""
                    onSelect: function(name) {
                        if (managerPage.voiceAgent) {
                            managerPage.voiceAgent.selectTtsModel(name);
                        }
                    }
                    onInstall: function(name) {
                        if (managerPage.voiceAgent) {
                            managerPage.voiceAgent.installTtsModel(name);
                        }
                    }
                    onRemove: function(name) {
                        if (managerPage.voiceAgent) {
                            managerPage.voiceAgent.deleteTtsModel(name);
                        }
                    }
                }
            }
        }
    }
}
