import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import org.kde.kirigami 2.20 as Kirigami

Kirigami.ApplicationWindow {
    id: root

    width: 1180
    height: 840
    visible: true
    title: "Voice Agent"

    QtObject {
        id: nullVoiceAgent

        property var sttCatalog: []
        property var ttsCatalog: []
        property bool talkReady: false
        property bool voiceConnectionEnabled: false
        property string selectedSttModel: ""
        property string selectedTtsModel: ""
        property string currentLlmUrl: ""
        property string selectedLlmModel: ""
        property string themeModeLabel: "Auto"
        property string themeMode: "auto"
        property bool audioMuted: false
        property string modelStatus: "No STT models installed"
        property var sttOptions: []
        property bool modelLoading: false
        property real modelProgressValue: 0
        property bool modelProgressIndeterminate: false
        property string modelProgressText: ""
        property string ttsStatus: "No TTS voices installed"
        property var ttsOptions: []
        property bool ttsLoading: false
        property real ttsProgressValue: 0
        property bool ttsProgressIndeterminate: false
        property string ttsProgressText: ""
        property bool llmServerConnected: false
        property bool llmConnectionBusy: false
        property bool llmModelBusy: false
        property var llmUrls: []
        property string llmConnectionButtonText: "Connect"
        property var llmModelOptions: [""]
        property var conversationMessages: []

        function setThemeMode(_) {}
        function setAudioMuted(_) {}
        function selectSttModel(_) {}
        function deleteSttModel(_) {}
        function installSttModel(_) {}
        function selectTtsModel(_) {}
        function deleteTtsModel(_) {}
        function installTtsModel(_) {}
        function setVoiceConnectionEnabled(_) {}
        function setCurrentLlmUrl(_) {}
        function persistCurrentLlmUrl() {}
        function toggleLlmServerConnection(_) {}
        function selectLlmModel(_) {}
        function replayMessage(_) {}
    }

    readonly property var agent: (typeof voiceAgent !== "undefined" && voiceAgent !== null) ? voiceAgent : nullVoiceAgent

    readonly property bool compactMode: width < Kirigami.Units.gridUnit * 25
    readonly property bool largeMode: width >= Kirigami.Units.gridUnit * 50
    readonly property bool mediumMode: !compactMode && !largeMode
    readonly property bool ultraCompactMode: compactMode
    readonly property int dashboardColumns: largeMode ? 2 : 1
    readonly property int sttInstalledCount: countInstalled(root.agent.sttCatalog)
    readonly property int ttsInstalledCount: countInstalled(root.agent.ttsCatalog)
    readonly property color micPulseColor: root.agent.talkReady ? Kirigami.Theme.highlightColor : Kirigami.Theme.disabledTextColor
    readonly property color micButtonColor: root.agent.voiceConnectionEnabled ? Kirigami.Theme.highlightColor : Kirigami.Theme.alternateBackgroundColor
    readonly property bool micPulseActive: root.agent.voiceConnectionEnabled || root.agent.talkReady
    readonly property real pageContentMargin: root.compactMode ? Kirigami.Units.smallSpacing : (root.mediumMode ? Kirigami.Units.mediumSpacing : Kirigami.Units.largeSpacing)
    readonly property real pageContentSpacing: root.compactMode ? Kirigami.Units.smallSpacing : Kirigami.Units.largeSpacing

    function stringIndex(options, value) {
        for (let i = 0; i < options.length; i += 1) {
            if (options[i] === value) {
                return i;
            }
        }
        return -1;
    }

    function countInstalled(items) {
        let count = 0;
        for (let i = 0; i < items.length; i += 1) {
            if (items[i].installed) {
                count += 1;
            }
        }
        return count;
    }

    function catalogMatches(name, filterText) {
        if (!filterText) {
            return true;
        }
        return name.toLowerCase().indexOf(filterText.toLowerCase()) !== -1;
    }

    function sessionReadinessText() {
        const missing = [];
        if (!root.agent.selectedSttModel) {
            missing.push("an STT model");
        }
        if (!root.agent.selectedTtsModel) {
            missing.push("a TTS voice");
        }
        if (!root.agent.currentLlmUrl) {
            missing.push("an LLM URL");
        }
        if (!root.agent.selectedLlmModel) {
            missing.push("a loaded LLM");
        }
        if (missing.length === 0) {
            return "Everything is ready for voice mode.";
        }
        return "Still needed: " + missing.join(", ") + ".";
    }

    function modelStatusSummary(item) {
        return item.installed ? "Installed" : "Available to download";
    }

    function modelActionLabel(item) {
        return item.installed ? "Remove" : "Install";
    }

    function scrollList(listView, wheel) {
        if (!listView) {
            return;
        }
        const delta = wheel.pixelDelta.y !== 0
            ? wheel.pixelDelta.y * 2.5
            : (wheel.angleDelta.y / 120) * Kirigami.Units.gridUnit * 10;
        const maxY = Math.max(0, listView.contentHeight - listView.height);
        listView.contentY = Math.max(0, Math.min(maxY, listView.contentY - delta));
        wheel.accepted = true;
    }

    function bubbleText(text) {
        const content = (text || "").trim();
        return content;
    }

    Kirigami.Action {
        id: modelManagerAction
        text: "Voice Models"
        icon.name: "folder-cloud"
        visible: !root.compactMode
        onTriggered: {
            modelManagerWindow.x = root.x + Math.max(0, (root.width - modelManagerWindow.width) / 2);
            modelManagerWindow.y = root.y + Math.max(0, (root.height - modelManagerWindow.height) / 2);
            modelManagerWindow.show();
            modelManagerWindow.raise();
            modelManagerWindow.requestActivate();
        }
    }

    ActionGroup {
        id: themeActionGroup
    }

    Kirigami.Action {
        id: themeAction
        text: "Theme: " + root.agent.themeModeLabel
        icon.name: "preferences-desktop-theme-global"
        visible: !root.compactMode

        Kirigami.Action {
            text: "Auto"
            checkable: true
            checked: root.agent.themeMode === "auto"
            ActionGroup.group: themeActionGroup
            onTriggered: root.agent.setThemeMode("auto")
        }

        Kirigami.Action {
            text: "Light"
            checkable: true
            checked: root.agent.themeMode === "light"
            ActionGroup.group: themeActionGroup
            onTriggered: root.agent.setThemeMode("light")
        }

        Kirigami.Action {
            text: "Dark"
            checkable: true
            checked: root.agent.themeMode === "dark"
            ActionGroup.group: themeActionGroup
            onTriggered: root.agent.setThemeMode("dark")
        }
    }

    Kirigami.Action {
        id: muteAction
        text: root.agent.audioMuted ? "Unmute" : "Mute"
        icon.name: root.agent.audioMuted ? "audio-volume-muted" : "audio-volume-high"
        enabled: root.agent.talkReady
        onTriggered: root.agent.setAudioMuted(!root.agent.audioMuted)
    }

    Window {
        id: modelManagerWindow

        transientParent: root
        modality: Qt.ApplicationModal
        flags: Qt.Dialog | Qt.WindowTitleHint | Qt.WindowCloseButtonHint
        title: "Voice Models"
        visible: false
        width: Math.min(root.width - Kirigami.Units.gridUnit * 4, Kirigami.Units.gridUnit * 58)
        height: Math.min(root.height - Kirigami.Units.gridUnit * 4, Kirigami.Units.gridUnit * 42)
        minimumWidth: Kirigami.Units.gridUnit * 30
        minimumHeight: Kirigami.Units.gridUnit * 24
        color: Kirigami.Theme.backgroundColor

        property string sttFilter: ""
        property string ttsFilter: ""

        Pane {
            anchors.fill: parent
            padding: 0

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

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.mediumSpacing

                        Kirigami.Heading {
                            text: "Voice Models"
                            level: 2
                        }

                        Item {
                            Layout.fillWidth: true
                        }

                        ToolButton {
                            icon.name: "window-close"
                            text: "Close"
                            onClicked: modelManagerWindow.close()
                        }
                    }

                    Label {
                        Layout.fillWidth: true
                        text: "Install, remove, and switch local speech models here. Session selectors only show installed items."
                        wrapMode: Text.WordWrap
                        color: Kirigami.Theme.disabledTextColor
                    }

                    GridLayout {
                        Layout.fillWidth: true
                        columns: root.compactMode ? 1 : 2
                        columnSpacing: Kirigami.Units.largeSpacing

                        Label {
                            Layout.fillWidth: true
                            text: root.sttInstalledCount + " STT model(s) installed"
                            font.weight: Font.DemiBold
                        }

                        Label {
                            Layout.fillWidth: true
                            text: root.ttsInstalledCount + " TTS voice(s) installed"
                            font.weight: Font.DemiBold
                        }
                    }
                }

                TabBar {
                    id: managerTabs
                    Layout.fillWidth: true

                    TabButton { text: "Speech To Text" }
                    TabButton { text: "Text To Speech" }
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
                            placeholderText: "Filter STT models"
                            text: modelManagerWindow.sttFilter
                            onTextChanged: modelManagerWindow.sttFilter = text
                        }

                        ListView {
                            id: sttCatalogView
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: 0
                            model: root.agent.sttCatalog
                            boundsBehavior: Flickable.StopAtBounds
                            flickDeceleration: 1800
                            maximumFlickVelocity: 24000
                            ScrollBar.vertical: ScrollBar {}

                            WheelHandler {
                                target: null
                                onWheel: function(event) {
                                    root.scrollList(sttCatalogView, event);
                                }
                            }

                            delegate: ItemDelegate {
                                width: ListView.view ? ListView.view.width : 0
                                visible: root.catalogMatches(modelData.name, modelManagerWindow.sttFilter)
                                height: visible ? implicitHeight : 0
                                padding: Kirigami.Units.mediumSpacing
                                onClicked: {
                                    if (modelData.installed) {
                                        root.agent.selectSttModel(modelData.name);
                                    }
                                }
                                background: Rectangle {
                                    color: parent.down || parent.hovered ? Kirigami.Theme.hoverColor : "transparent"
                                }
                                contentItem: RowLayout {
                                    spacing: Kirigami.Units.mediumSpacing

                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 2

                                        Label {
                                            Layout.fillWidth: true
                                            text: modelData.name
                                            font.weight: root.agent.selectedSttModel === modelData.name ? Font.DemiBold : Font.Normal
                                            wrapMode: Text.WordWrap
                                        }

                                        Label {
                                            Layout.fillWidth: true
                                            text: root.modelStatusSummary(modelData)
                                            color: Kirigami.Theme.disabledTextColor
                                            wrapMode: Text.WordWrap
                                        }
                                    }

                                    RowLayout {
                                        spacing: Kirigami.Units.smallSpacing

                                        ToolButton {
                                            visible: modelData.installed
                                            text: root.agent.selectedSttModel === modelData.name ? "Current" : "Use"
                                            enabled: root.agent.selectedSttModel !== modelData.name
                                            onClicked: root.agent.selectSttModel(modelData.name)
                                        }

                                        ToolButton {
                                            text: root.modelActionLabel(modelData)
                                            enabled: !root.agent.modelLoading
                                            onClicked: {
                                                if (modelData.installed) {
                                                    root.agent.deleteSttModel(modelData.name);
                                                } else {
                                                    root.agent.installSttModel(modelData.name);
                                                }
                                            }
                                        }
                                    }
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
                            placeholderText: "Filter TTS voices"
                            text: modelManagerWindow.ttsFilter
                            onTextChanged: modelManagerWindow.ttsFilter = text
                        }

                        ListView {
                            id: ttsCatalogView
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: 0
                            model: root.agent.ttsCatalog
                            boundsBehavior: Flickable.StopAtBounds
                            flickDeceleration: 1800
                            maximumFlickVelocity: 24000
                            ScrollBar.vertical: ScrollBar {}

                            WheelHandler {
                                target: null
                                onWheel: function(event) {
                                    root.scrollList(ttsCatalogView, event);
                                }
                            }

                            delegate: ItemDelegate {
                                width: ListView.view ? ListView.view.width : 0
                                visible: root.catalogMatches(modelData.name, modelManagerWindow.ttsFilter)
                                height: visible ? implicitHeight : 0
                                padding: Kirigami.Units.mediumSpacing
                                onClicked: {
                                    if (modelData.installed) {
                                        root.agent.selectTtsModel(modelData.name);
                                    }
                                }
                                background: Rectangle {
                                    color: parent.down || parent.hovered ? Kirigami.Theme.hoverColor : "transparent"
                                }
                                contentItem: RowLayout {
                                    spacing: Kirigami.Units.mediumSpacing

                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 2

                                        Label {
                                            Layout.fillWidth: true
                                            text: modelData.name
                                            font.weight: root.agent.selectedTtsModel === modelData.name ? Font.DemiBold : Font.Normal
                                            wrapMode: Text.WordWrap
                                        }

                                        Label {
                                            Layout.fillWidth: true
                                            text: root.modelStatusSummary(modelData)
                                            color: Kirigami.Theme.disabledTextColor
                                            wrapMode: Text.WordWrap
                                        }
                                    }

                                    RowLayout {
                                        spacing: Kirigami.Units.smallSpacing

                                        ToolButton {
                                            visible: modelData.installed
                                            text: root.agent.selectedTtsModel === modelData.name ? "Current" : "Use"
                                            enabled: root.agent.selectedTtsModel !== modelData.name
                                            onClicked: root.agent.selectTtsModel(modelData.name)
                                        }

                                        ToolButton {
                                            text: root.modelActionLabel(modelData)
                                            enabled: !root.agent.ttsLoading
                                            onClicked: {
                                                if (modelData.installed) {
                                                    root.agent.deleteTtsModel(modelData.name);
                                                } else {
                                                    root.agent.installTtsModel(modelData.name);
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    Component {
        id: sessionPaneComponent

        Pane {
            implicitHeight: sessionContent.implicitHeight + padding * 2
            padding: root.compactMode ? Kirigami.Units.smallSpacing : (root.mediumMode ? Kirigami.Units.smallSpacing : Kirigami.Units.mediumSpacing)

            ColumnLayout {
                id: sessionContent
                anchors.fill: parent
                spacing: Kirigami.Units.mediumSpacing

                Kirigami.Heading {
                    text: "Session Setup"
                    level: 2
                }

                Pane {
                    id: sessionSetupPane
                    Layout.fillWidth: true
                    padding: Kirigami.Units.smallSpacing
                    implicitHeight: sessionSetupGrid.implicitHeight + padding * 2

                    GridLayout {
                        id: sessionSetupGrid
                        width: parent.width
                        columns: root.compactMode ? 1 : (root.largeMode ? 2 : 3)
                        columnSpacing: Kirigami.Units.mediumSpacing
                        rowSpacing: Kirigami.Units.smallSpacing

                        Label {
                            Layout.fillWidth: true
                            text: "Speech: " + root.agent.modelStatus
                            color: Kirigami.Theme.disabledTextColor
                            wrapMode: Text.WordWrap
                        }

                        ComboBox {
                            id: sttSelector
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 14
                            model: root.agent.sttOptions
                            currentIndex: root.stringIndex(root.agent.sttOptions, root.agent.selectedSttModel)
                            displayText: currentIndex >= 0 ? currentText : "No installed STT models"
                            onActivated: root.agent.selectSttModel(currentText)
                        }

                        Item {
                            id: mediumMicButtonFrame
                            visible: root.mediumMode
                            Layout.row: 0
                            Layout.column: 2
                            Layout.rowSpan: 4
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.minimumWidth: Kirigami.Units.gridUnit * 9
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 10
                            property real glowOpacity: root.micPulseActive ? 0.5 : 0.2
                            property real glowScale: 1.0

                            SequentialAnimation {
                                running: root.micPulseActive
                                loops: Animation.Infinite

                                ParallelAnimation {
                                    NumberAnimation {
                                        target: mediumMicButtonFrame
                                        property: "glowOpacity"
                                        to: root.agent.voiceConnectionEnabled ? 1.0 : 0.78
                                        duration: root.agent.voiceConnectionEnabled ? 700 : 1200
                                        easing.type: Easing.InOutSine
                                    }
                                    NumberAnimation {
                                        target: mediumMicButtonFrame
                                        property: "glowScale"
                                        to: 1.02
                                        duration: root.agent.voiceConnectionEnabled ? 700 : 1200
                                        easing.type: Easing.InOutSine
                                    }
                                }

                                ParallelAnimation {
                                    NumberAnimation {
                                        target: mediumMicButtonFrame
                                        property: "glowOpacity"
                                        to: root.agent.voiceConnectionEnabled ? 0.45 : 0.35
                                        duration: root.agent.voiceConnectionEnabled ? 700 : 1200
                                        easing.type: Easing.InOutSine
                                    }
                                    NumberAnimation {
                                        target: mediumMicButtonFrame
                                        property: "glowScale"
                                        to: 1.0
                                        duration: root.agent.voiceConnectionEnabled ? 700 : 1200
                                        easing.type: Easing.InOutSine
                                    }
                                }
                            }

                            Button {
                                anchors.fill: parent
                                anchors.margins: 0
                                text: "\ud83c\udf99\ufe0f"
                                enabled: root.agent.talkReady
                                font.pixelSize: 38
                                scale: mediumMicButtonFrame.glowScale
                                opacity: root.micPulseActive ? 1 : 0.92
                                onClicked: root.agent.setVoiceConnectionEnabled(!root.agent.voiceConnectionEnabled)

                                background: Rectangle {
                                    radius: height / 2
                                    color: root.micButtonColor
                                    border.width: 3
                                    border.color: Qt.rgba(root.micPulseColor.r, root.micPulseColor.g, root.micPulseColor.b, Math.max(0.7, mediumMicButtonFrame.glowOpacity))
                                }
                            }
                        }

                        Label {
                            Layout.fillWidth: true
                            text: "Voice: " + root.agent.ttsStatus
                            color: Kirigami.Theme.disabledTextColor
                            wrapMode: Text.WordWrap
                        }

                        ComboBox {
                            id: ttsSelector
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 14
                            model: root.agent.ttsOptions
                            currentIndex: root.stringIndex(root.agent.ttsOptions, root.agent.selectedTtsModel)
                            displayText: currentIndex >= 0 ? currentText : "No installed TTS voices"
                            onActivated: root.agent.selectTtsModel(currentText)
                        }

                        Label {
                            Layout.fillWidth: true
                            text: "LLM URL:"
                            color: Kirigami.Theme.disabledTextColor
                            wrapMode: Text.WordWrap
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Kirigami.Units.smallSpacing

                            ComboBox {
                                id: llmUrlBox
                                Layout.fillWidth: true
                                Layout.minimumWidth: 0
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 16
                                editable: !root.agent.llmServerConnected && !root.agent.llmModelBusy
                                enabled: !root.agent.llmServerConnected && !root.agent.llmModelBusy
                                model: root.agent.llmUrls
                                currentIndex: root.stringIndex(root.agent.llmUrls, root.agent.currentLlmUrl)
                                Component.onCompleted: editText = root.agent.currentLlmUrl
                                onAccepted: {
                                    root.agent.setCurrentLlmUrl(editText);
                                    root.agent.persistCurrentLlmUrl();
                                }
                                onActivated: root.agent.setCurrentLlmUrl(currentText)
                            }

                            Button {
                                Layout.minimumWidth: Kirigami.Units.gridUnit * 9
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 10
                                text: root.agent.llmConnectionButtonText
                                enabled: !!llmUrlBox.editText.trim() && !root.agent.llmModelBusy
                                    && (!root.agent.llmServerConnected || !root.agent.llmConnectionBusy)
                                onClicked: root.agent.toggleLlmServerConnection(llmUrlBox.editText)
                            }
                        }

                        Label {
                            Layout.fillWidth: true
                            text: "Loaded Model:"
                            color: Kirigami.Theme.disabledTextColor
                            wrapMode: Text.WordWrap
                        }

                        ComboBox {
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 16
                            enabled: root.agent.llmServerConnected && !root.agent.llmConnectionBusy && !root.agent.llmModelBusy
                            model: root.agent.llmModelOptions
                            currentIndex: root.stringIndex(root.agent.llmModelOptions, root.agent.selectedLlmModel)
                            displayText: currentIndex <= 0 ? "Select a loaded model" : currentText
                            onActivated: root.agent.selectLlmModel(currentText)
                        }
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing

                    ProgressBar {
                        Layout.fillWidth: true
                        visible: root.agent.modelLoading
                        from: 0
                        to: 1
                        indeterminate: root.agent.modelProgressIndeterminate
                        value: root.agent.modelProgressValue
                    }

                    Label {
                        Layout.fillWidth: true
                        visible: root.agent.modelLoading
                        text: root.agent.modelProgressText
                        wrapMode: Text.WordWrap
                        color: Kirigami.Theme.disabledTextColor
                    }

                    ProgressBar {
                        Layout.fillWidth: true
                        visible: root.agent.ttsLoading
                        from: 0
                        to: 1
                        indeterminate: root.agent.ttsProgressIndeterminate
                        value: root.agent.ttsProgressValue
                    }

                    Label {
                        Layout.fillWidth: true
                        visible: root.agent.ttsLoading
                        text: root.agent.ttsProgressText
                        wrapMode: Text.WordWrap
                        color: Kirigami.Theme.disabledTextColor
                    }
                }
            }
        }
    }

    Component {
        id: conversationPaneComponent

        Pane {
            padding: root.compactMode ? Kirigami.Units.smallSpacing : (root.mediumMode ? Kirigami.Units.smallSpacing : Kirigami.Units.mediumSpacing)

            ColumnLayout {
                id: conversationContent
                anchors.fill: parent
                spacing: root.compactMode ? Kirigami.Units.smallSpacing : Kirigami.Units.mediumSpacing

                RowLayout {
                    Layout.fillWidth: true

                    Kirigami.Heading {
                        visible: !root.compactMode
                        text: "Conversation"
                        level: 2
                    }

                    Item {
                        Layout.fillWidth: true
                    }

                    Label {
                        visible: !root.compactMode
                        text: root.agent.voiceConnectionEnabled ? "Live" : "Idle"
                        color: Kirigami.Theme.disabledTextColor
                    }
                }

                ListView {
                    id: conversationView
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    spacing: Kirigami.Units.smallSpacing
                    model: root.agent.conversationMessages

                    delegate: Item {
                        width: conversationView.width
                        readonly property bool systemEntry: modelData.role === "system"
                        implicitHeight: systemEntry ? systemMessage.implicitHeight : messageRow.implicitHeight

                        property bool assistant: modelData.role === "assistant"
                        readonly property string bubbleState: modelData.bubbleState || "sent"
                        readonly property color bubbleColor: bubbleState === "thinking" || bubbleState === "draft"
                            ? "#ff5c8a"
                            : (assistant ? "#34c759" : "#4a4a4f")
                        readonly property color bubbleTextColor: "#ffffff"
                        readonly property color systemTextColor: (modelData.level || "status") === "error"
                            ? Kirigami.Theme.negativeTextColor
                            : Kirigami.Theme.disabledTextColor
                        readonly property real maxBubbleWidth: Math.min(
                            conversationView.width * (root.compactMode ? 0.96 : (root.mediumMode ? 0.9 : 0.78)),
                            Kirigami.Units.gridUnit * (root.compactMode ? 18 : (root.mediumMode ? 28 : 34))
                        )

                        Label {
                            id: systemMessage
                            visible: parent.systemEntry
                            width: parent.width
                            text: (modelData.timestampLabel || "") + ((modelData.timestampLabel || "") ? "  " : "") + root.bubbleText(modelData.text)
                            wrapMode: Text.WordWrap
                            color: parent.systemTextColor
                            textFormat: Text.PlainText
                            horizontalAlignment: Text.AlignLeft
                            verticalAlignment: Text.AlignVCenter
                            font.pixelSize: 12
                        }

                        RowLayout {
                            id: messageRow
                            width: parent.width
                            visible: !parent.systemEntry
                            spacing: Kirigami.Units.smallSpacing
                            layoutDirection: assistant ? Qt.LeftToRight : Qt.RightToLeft

                            Frame {
                                Layout.preferredWidth: maxBubbleWidth
                                Layout.maximumWidth: maxBubbleWidth

                                background: Rectangle {
                                    radius: root.compactMode ? Kirigami.Units.mediumSpacing : Kirigami.Units.largeSpacing
                                    color: bubbleColor
                                }

                                contentItem: ColumnLayout {
                                    spacing: 4

                                    Label {
                                        visible: !root.compactMode
                                        text: assistant ? "Assistant" : "You"
                                        color: bubbleTextColor
                                        opacity: 0.8
                                        font.pixelSize: 12
                                        font.weight: Font.DemiBold
                                    }

                                    Label {
                                        Layout.fillWidth: true
                                        text: root.bubbleText(modelData.text)
                                        wrapMode: Text.WordWrap
                                        color: bubbleTextColor
                                        textFormat: Text.PlainText
                                    }

                                    Label {
                                        visible: !!(modelData.timestampLabel || "")
                                        Layout.fillWidth: true
                                        text: modelData.timestampLabel || ""
                                        color: Qt.rgba(1, 1, 1, 0.72)
                                        font.pixelSize: 10
                                        horizontalAlignment: Text.AlignLeft
                                    }
                                }
                            }

                            Button {
                                visible: !root.compactMode && modelData.replayable
                                text: "Replay"
                                Layout.alignment: Qt.AlignBottom
                                onClicked: root.agent.replayMessage(index)
                            }
                        }
                    }

                    footer: Kirigami.PlaceholderMessage {
                        width: conversationView.width
                        visible: root.agent.conversationMessages.length === 0
                        text: "Spoken turns will appear here once voice mode is active."
                    }
                }

                Button {
                    visible: root.compactMode
                    Layout.fillWidth: true
                    Layout.preferredHeight: Kirigami.Units.gridUnit * 5
                    text: "\ud83c\udf99\ufe0f"
                    enabled: root.agent.talkReady
                    font.pixelSize: 32
                    onClicked: root.agent.setVoiceConnectionEnabled(!root.agent.voiceConnectionEnabled)
                    background: Rectangle {
                        radius: height / 2
                        color: root.micButtonColor
                        border.width: root.compactMode ? 3 : 0
                        border.color: Qt.rgba(root.micPulseColor.r, root.micPulseColor.g, root.micPulseColor.b, 0.85)
                    }
                }
            }
        }
    }

    Component {
        id: largeMicPaneComponent

        Pane {
            padding: Kirigami.Units.mediumSpacing
            implicitWidth: Kirigami.Units.gridUnit * 12

            Item {
                id: largeMicButtonFrame
                anchors.fill: parent
                property real glowOpacity: root.micPulseActive ? 0.5 : 0.2
                property real glowScale: 1.0

                SequentialAnimation {
                    running: root.largeMode && root.micPulseActive
                    loops: Animation.Infinite

                    ParallelAnimation {
                        NumberAnimation {
                            target: largeMicButtonFrame
                            property: "glowOpacity"
                            to: root.agent.voiceConnectionEnabled ? 1.0 : 0.78
                            duration: root.agent.voiceConnectionEnabled ? 700 : 1200
                            easing.type: Easing.InOutSine
                        }
                        NumberAnimation {
                            target: largeMicButtonFrame
                            property: "glowScale"
                            to: 1.02
                            duration: root.agent.voiceConnectionEnabled ? 700 : 1200
                            easing.type: Easing.InOutSine
                        }
                    }

                    ParallelAnimation {
                        NumberAnimation {
                            target: largeMicButtonFrame
                            property: "glowOpacity"
                            to: root.agent.voiceConnectionEnabled ? 0.45 : 0.35
                            duration: root.agent.voiceConnectionEnabled ? 700 : 1200
                            easing.type: Easing.InOutSine
                        }
                        NumberAnimation {
                            target: largeMicButtonFrame
                            property: "glowScale"
                            to: 1.0
                            duration: root.agent.voiceConnectionEnabled ? 700 : 1200
                            easing.type: Easing.InOutSine
                        }
                    }
                }

                    Button {
                        anchors.fill: parent
                        anchors.margins: 0
                        text: "\ud83c\udf99\ufe0f"
                        enabled: root.agent.talkReady
                        font.pixelSize: 34
                    scale: largeMicButtonFrame.glowScale
                    opacity: root.micPulseActive ? 1 : 0.92
                    onClicked: root.agent.setVoiceConnectionEnabled(!root.agent.voiceConnectionEnabled)

                    background: Rectangle {
                        radius: height / 2
                        color: root.micButtonColor
                        border.width: 3
                        border.color: Qt.rgba(root.micPulseColor.r, root.micPulseColor.g, root.micPulseColor.b, Math.max(0.7, largeMicButtonFrame.glowOpacity))
                    }
                }
            }
        }
    }

    pageStack.initialPage: Kirigami.Page {
        id: page
        title: "Voice Agent"
        actions: [
            themeAction,
            muteAction,
            modelManagerAction
        ]

        ColumnLayout {
            id: pageContent
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            anchors.margins: root.pageContentMargin
            spacing: root.pageContentSpacing

            Item {
                id: dashboardModes
                Layout.fillWidth: true
                Layout.fillHeight: true
                readonly property real largeMicMinimumHeight: Kirigami.Units.gridUnit * 6.5
                readonly property bool largeMicPriorityMode: root.largeMode
                    && largeControlsColumn.height > 0
                    && largeControlsColumn.height < ((largeSessionLoader.item ? largeSessionLoader.item.implicitHeight : 0)
                        + largeMicMinimumHeight + Kirigami.Units.largeSpacing)

                RowLayout {
                    id: largeDashboardRow
                    anchors.fill: parent
                    visible: root.largeMode
                    spacing: Kirigami.Units.largeSpacing

                    ColumnLayout {
                        id: largeControlsColumn
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.preferredWidth: 1
                        spacing: Kirigami.Units.largeSpacing

                        Loader {
                            id: largeSessionLoader
                            active: root.largeMode
                            visible: !dashboardModes.largeMicPriorityMode
                            sourceComponent: sessionPaneComponent
                            Layout.fillWidth: true
                        }

                        Loader {
                            id: largeMicLoader
                            active: root.largeMode
                            sourceComponent: largeMicPaneComponent
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                        }
                    }

                    Loader {
                        active: root.largeMode
                        sourceComponent: conversationPaneComponent
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.preferredWidth: 1
                    }
                }

                ColumnLayout {
                    anchors.fill: parent
                    visible: root.mediumMode
                    spacing: Kirigami.Units.largeSpacing

                    Loader {
                        active: root.mediumMode
                        sourceComponent: sessionPaneComponent
                        Layout.fillWidth: true
                    }

                    Loader {
                        active: root.mediumMode
                        sourceComponent: conversationPaneComponent
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                    }
                }

                Loader {
                    anchors.fill: parent
                    visible: root.compactMode
                    active: root.compactMode
                    sourceComponent: conversationPaneComponent
                }
            }
        }
    }
}
