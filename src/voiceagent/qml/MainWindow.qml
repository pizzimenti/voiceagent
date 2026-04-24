import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import org.kde.kirigami 2.20 as Kirigami

Kirigami.ApplicationWindow {
    id: root

    width: 512
    height: 512
    visible: true
    title: "Voice Agent"
    required property QtObject voiceAgent

    readonly property bool compactMode: width < Kirigami.Units.gridUnit * 25
    readonly property bool largeMode: width >= Kirigami.Units.gridUnit * 50
    readonly property bool mediumMode: !compactMode && !largeMode
    readonly property bool ultraCompactMode: compactMode
    readonly property int dashboardColumns: largeMode ? 2 : 1
    readonly property int sttInstalledCount: countInstalled(voiceAgent.sttCatalog)
    readonly property int ttsInstalledCount: countInstalled(voiceAgent.ttsCatalog)
    readonly property color micPulseColor: voiceAgent.talkReady ? Kirigami.Theme.highlightColor : Kirigami.Theme.disabledTextColor
    readonly property color micButtonColor: voiceAgent.voiceConnectionEnabled ? Kirigami.Theme.highlightColor : Kirigami.Theme.alternateBackgroundColor
    readonly property bool micPulseActive: voiceAgent.voiceConnectionEnabled || voiceAgent.talkReady
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
        if (!voiceAgent.selectedSttModel) {
            missing.push("an STT model");
        }
        if (!voiceAgent.selectedTtsModel) {
            missing.push("a TTS voice");
        }
        if (!voiceAgent.currentLlmUrl) {
            missing.push("an LLM URL");
        }
        if (!voiceAgent.selectedLlmModel) {
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
        const pdy = wheel.pixelDelta ? wheel.pixelDelta.y : 0;
        const ady = wheel.angleDelta ? wheel.angleDelta.y : 0;
        const delta = pdy !== 0
            ? pdy * 8
            : (ady / 120) * Kirigami.Units.gridUnit * 12;
        const minY = listView.originY || 0;
        const maxY = minY + Math.max(0, listView.contentHeight - listView.height);
        listView.contentY = Math.max(minY, Math.min(maxY, listView.contentY - delta));
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
        text: "Theme: " + voiceAgent.themeModeLabel
        icon.name: "preferences-desktop-theme-global"
        visible: !root.compactMode

        Kirigami.Action {
            text: "Auto"
            checkable: true
            checked: voiceAgent.themeMode === "auto"
            ActionGroup.group: themeActionGroup
            onTriggered: voiceAgent.setThemeMode("auto")
        }

        Kirigami.Action {
            text: "Light"
            checkable: true
            checked: voiceAgent.themeMode === "light"
            ActionGroup.group: themeActionGroup
            onTriggered: voiceAgent.setThemeMode("light")
        }

        Kirigami.Action {
            text: "Dark"
            checkable: true
            checked: voiceAgent.themeMode === "dark"
            ActionGroup.group: themeActionGroup
            onTriggered: voiceAgent.setThemeMode("dark")
        }
    }

    Kirigami.Action {
        id: muteAction
        text: voiceAgent.audioMuted ? "Unmute" : "Mute"
        icon.name: voiceAgent.audioMuted ? "audio-volume-muted" : "audio-volume-high"
        enabled: voiceAgent.talkReady
        onTriggered: voiceAgent.setAudioMuted(!voiceAgent.audioMuted)
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
                            model: voiceAgent.sttCatalogModel
                            boundsBehavior: Flickable.StopAtBounds
                            flickDeceleration: 1800
                            maximumFlickVelocity: 24000
                            ScrollBar.vertical: ScrollBar {}

                            MouseArea {
                                anchors.fill: parent
                                acceptedButtons: Qt.NoButton
                                propagateComposedEvents: true
                                z: 2
                                onWheel: function(wheel) {
                                    root.scrollList(sttCatalogView, wheel);
                                }
                            }

                            delegate: Item {
                                id: sttDelegate
                                width: ListView.view ? ListView.view.width : 0
                                visible: root.catalogMatches(model.name, modelManagerWindow.sttFilter)
                                height: visible ? sttRow.implicitHeight + Kirigami.Units.mediumSpacing * 2 : 0
                                readonly property bool downloading: voiceAgent.sttDownloadingList.indexOf(model.name) >= 0
                                readonly property real downloadProgress: voiceAgent.sttProgressMap[model.name] || 0

                                RowLayout {
                                    id: sttRow
                                    anchors.top: parent.top
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.margins: Kirigami.Units.mediumSpacing
                                    spacing: Kirigami.Units.mediumSpacing

                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 2

                                        Label {
                                            Layout.fillWidth: true
                                            text: model.name
                                            font.weight: voiceAgent.selectedSttModel === model.name ? Font.DemiBold : Font.Normal
                                            wrapMode: Text.WordWrap
                                        }

                                        Label {
                                            Layout.fillWidth: true
                                            text: root.modelStatusSummary(model)
                                            color: Kirigami.Theme.disabledTextColor
                                            wrapMode: Text.WordWrap
                                        }
                                    }

                                    RowLayout {
                                        spacing: Kirigami.Units.smallSpacing

                                        ToolButton {
                                            visible: model.installed
                                            text: voiceAgent.selectedSttModel === model.name ? "Current" : "Use"
                                            enabled: voiceAgent.selectedSttModel !== model.name
                                            onClicked: voiceAgent.selectSttModel(model.name)
                                        }

                                        ToolButton {
                                            text: sttDelegate.downloading ? "Installing…" : root.modelActionLabel(model)
                                            enabled: !sttDelegate.downloading
                                            onClicked: {
                                                if (model.installed) {
                                                    voiceAgent.deleteSttModel(model.name);
                                                } else {
                                                    voiceAgent.installSttModel(model.name);
                                                }
                                            }
                                        }
                                    }
                                }

                                Rectangle {
                                    id: sttProgressTrack
                                    visible: sttDelegate.downloading
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.bottom: parent.bottom
                                    height: 2
                                    color: Kirigami.Theme.alternateBackgroundColor
                                    Rectangle {
                                        anchors.left: parent.left
                                        anchors.top: parent.top
                                        anchors.bottom: parent.bottom
                                        width: parent.width * sttDelegate.downloadProgress
                                        color: Kirigami.Theme.highlightColor
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
                            model: voiceAgent.ttsCatalogModel
                            boundsBehavior: Flickable.StopAtBounds
                            flickDeceleration: 1800
                            maximumFlickVelocity: 24000
                            ScrollBar.vertical: ScrollBar {}

                            MouseArea {
                                anchors.fill: parent
                                acceptedButtons: Qt.NoButton
                                propagateComposedEvents: true
                                z: 2
                                onWheel: function(wheel) {
                                    root.scrollList(ttsCatalogView, wheel);
                                }
                            }

                            delegate: Item {
                                id: ttsDelegate
                                width: ListView.view ? ListView.view.width : 0
                                visible: root.catalogMatches(model.name, modelManagerWindow.ttsFilter)
                                height: visible ? ttsRow.implicitHeight + Kirigami.Units.mediumSpacing * 2 : 0
                                readonly property bool downloading: voiceAgent.ttsDownloadingList.indexOf(model.name) >= 0
                                readonly property real downloadProgress: voiceAgent.ttsProgressMap[model.name] || 0

                                RowLayout {
                                    id: ttsRow
                                    anchors.top: parent.top
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.margins: Kirigami.Units.mediumSpacing
                                    spacing: Kirigami.Units.mediumSpacing

                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        spacing: 2

                                        Label {
                                            Layout.fillWidth: true
                                            text: model.name
                                            font.weight: voiceAgent.selectedTtsModel === model.name ? Font.DemiBold : Font.Normal
                                            wrapMode: Text.WordWrap
                                        }

                                        Label {
                                            Layout.fillWidth: true
                                            text: root.modelStatusSummary(model)
                                            color: Kirigami.Theme.disabledTextColor
                                            wrapMode: Text.WordWrap
                                        }
                                    }

                                    RowLayout {
                                        spacing: Kirigami.Units.smallSpacing

                                        ToolButton {
                                            visible: model.installed
                                            text: voiceAgent.selectedTtsModel === model.name ? "Current" : "Use"
                                            enabled: voiceAgent.selectedTtsModel !== model.name
                                            onClicked: voiceAgent.selectTtsModel(model.name)
                                        }

                                        ToolButton {
                                            text: ttsDelegate.downloading ? "Installing…" : root.modelActionLabel(model)
                                            enabled: !ttsDelegate.downloading
                                            onClicked: {
                                                if (model.installed) {
                                                    voiceAgent.deleteTtsModel(model.name);
                                                } else {
                                                    voiceAgent.installTtsModel(model.name);
                                                }
                                            }
                                        }
                                    }
                                }

                                Rectangle {
                                    id: ttsProgressTrack
                                    visible: ttsDelegate.downloading
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.bottom: parent.bottom
                                    height: 2
                                    color: Kirigami.Theme.alternateBackgroundColor
                                    Rectangle {
                                        anchors.left: parent.left
                                        anchors.top: parent.top
                                        anchors.bottom: parent.bottom
                                        width: parent.width * ttsDelegate.downloadProgress
                                        color: Kirigami.Theme.highlightColor
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
                            text: "Speech: " + voiceAgent.modelStatus
                            color: Kirigami.Theme.disabledTextColor
                            wrapMode: Text.WordWrap
                        }

                        ComboBox {
                            id: sttSelector
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 14
                            model: voiceAgent.sttOptions
                            currentIndex: root.stringIndex(voiceAgent.sttOptions, voiceAgent.selectedSttModel)
                            displayText: currentIndex >= 0 ? currentText : "No installed STT models"
                            onActivated: voiceAgent.selectSttModel(currentText)
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
                                        to: voiceAgent.voiceConnectionEnabled ? 1.0 : 0.78
                                        duration: voiceAgent.voiceConnectionEnabled ? 700 : 1200
                                        easing.type: Easing.InOutSine
                                    }
                                    NumberAnimation {
                                        target: mediumMicButtonFrame
                                        property: "glowScale"
                                        to: 1.02
                                        duration: voiceAgent.voiceConnectionEnabled ? 700 : 1200
                                        easing.type: Easing.InOutSine
                                    }
                                }

                                ParallelAnimation {
                                    NumberAnimation {
                                        target: mediumMicButtonFrame
                                        property: "glowOpacity"
                                        to: voiceAgent.voiceConnectionEnabled ? 0.45 : 0.35
                                        duration: voiceAgent.voiceConnectionEnabled ? 700 : 1200
                                        easing.type: Easing.InOutSine
                                    }
                                    NumberAnimation {
                                        target: mediumMicButtonFrame
                                        property: "glowScale"
                                        to: 1.0
                                        duration: voiceAgent.voiceConnectionEnabled ? 700 : 1200
                                        easing.type: Easing.InOutSine
                                    }
                                }
                            }

                            MicButton {
                                anchors.fill: parent
                                anchors.margins: 0
                                iconSize: 34
                                fontPixel: 11
                                borderWidth: 3
                                glowOpacity: mediumMicButtonFrame.glowOpacity
                                glowScaleSource: mediumMicButtonFrame.glowScale
                                buttonColor: root.micButtonColor
                                pulseColor: root.micPulseColor
                                pulseActive: root.micPulseActive
                            }
                        }

                        Label {
                            Layout.fillWidth: true
                            text: "Voice: " + voiceAgent.ttsStatus
                            color: Kirigami.Theme.disabledTextColor
                            wrapMode: Text.WordWrap
                        }

                        ComboBox {
                            id: ttsSelector
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            Layout.preferredWidth: Kirigami.Units.gridUnit * 14
                            model: voiceAgent.ttsOptions
                            currentIndex: root.stringIndex(voiceAgent.ttsOptions, voiceAgent.selectedTtsModel)
                            displayText: currentIndex >= 0 ? currentText : "No installed TTS voices"
                            onActivated: voiceAgent.selectTtsModel(currentText)
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
                                editable: !voiceAgent.llmServerConnected && !voiceAgent.llmModelBusy
                                enabled: !voiceAgent.llmServerConnected && !voiceAgent.llmModelBusy
                                model: voiceAgent.llmUrls
                                currentIndex: root.stringIndex(voiceAgent.llmUrls, voiceAgent.currentLlmUrl)
                                Component.onCompleted: editText = voiceAgent.currentLlmUrl
                                onAccepted: {
                                    voiceAgent.setCurrentLlmUrl(editText);
                                    voiceAgent.persistCurrentLlmUrl();
                                }
                                onActivated: voiceAgent.setCurrentLlmUrl(currentText)
                            }

                            Button {
                                Layout.minimumWidth: Kirigami.Units.gridUnit * 9
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 10
                                text: voiceAgent.llmConnectionButtonText
                                enabled: !!llmUrlBox.editText.trim() && !voiceAgent.llmModelBusy
                                    && (!voiceAgent.llmServerConnected || !voiceAgent.llmConnectionBusy)
                                onClicked: voiceAgent.toggleLlmServerConnection(llmUrlBox.editText)
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
                            enabled: voiceAgent.llmServerConnected && !voiceAgent.llmConnectionBusy && !voiceAgent.llmModelBusy
                            model: voiceAgent.llmModelOptions
                            currentIndex: root.stringIndex(voiceAgent.llmModelOptions, voiceAgent.selectedLlmModel)
                            displayText: currentIndex <= 0 ? "Select a loaded model" : currentText
                            onActivated: voiceAgent.selectLlmModel(currentText)
                        }
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing

                    ProgressBar {
                        Layout.fillWidth: true
                        visible: voiceAgent.modelLoading
                        from: 0
                        to: 1
                        indeterminate: voiceAgent.modelProgressIndeterminate
                        value: voiceAgent.modelProgressValue
                    }

                    Label {
                        Layout.fillWidth: true
                        visible: voiceAgent.modelLoading
                        text: voiceAgent.modelProgressText
                        wrapMode: Text.WordWrap
                        color: Kirigami.Theme.disabledTextColor
                    }

                    ProgressBar {
                        Layout.fillWidth: true
                        visible: voiceAgent.ttsLoading
                        from: 0
                        to: 1
                        indeterminate: voiceAgent.ttsProgressIndeterminate
                        value: voiceAgent.ttsProgressValue
                    }

                    Label {
                        Layout.fillWidth: true
                        visible: voiceAgent.ttsLoading
                        text: voiceAgent.ttsProgressText
                        wrapMode: Text.WordWrap
                        color: Kirigami.Theme.disabledTextColor
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
                            to: voiceAgent.voiceConnectionEnabled ? 1.0 : 0.78
                            duration: voiceAgent.voiceConnectionEnabled ? 700 : 1200
                            easing.type: Easing.InOutSine
                        }
                        NumberAnimation {
                            target: largeMicButtonFrame
                            property: "glowScale"
                            to: 1.02
                            duration: voiceAgent.voiceConnectionEnabled ? 700 : 1200
                            easing.type: Easing.InOutSine
                        }
                    }

                    ParallelAnimation {
                        NumberAnimation {
                            target: largeMicButtonFrame
                            property: "glowOpacity"
                            to: voiceAgent.voiceConnectionEnabled ? 0.45 : 0.35
                            duration: voiceAgent.voiceConnectionEnabled ? 700 : 1200
                            easing.type: Easing.InOutSine
                        }
                        NumberAnimation {
                            target: largeMicButtonFrame
                            property: "glowScale"
                            to: 1.0
                            duration: voiceAgent.voiceConnectionEnabled ? 700 : 1200
                            easing.type: Easing.InOutSine
                        }
                    }
                }

                    MicButton {
                        anchors.fill: parent
                        anchors.margins: 0
                        iconSize: 32
                        fontPixel: 12
                        borderWidth: 3
                        glowOpacity: largeMicButtonFrame.glowOpacity
                        glowScaleSource: largeMicButtonFrame.glowScale
                        buttonColor: root.micButtonColor
                        pulseColor: root.micPulseColor
                        pulseActive: root.micPulseActive
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
                        source: "ConversationPane.qml"
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
                        source: "ConversationPane.qml"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                    }
                }

                Loader {
                    anchors.fill: parent
                    visible: root.compactMode
                    active: root.compactMode
                    source: "ConversationPane.qml"
                }
            }
        }
    }
}
