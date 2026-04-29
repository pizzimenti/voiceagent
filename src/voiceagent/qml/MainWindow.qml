import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import org.kde.kirigami 2.20 as Kirigami

Kirigami.ApplicationWindow {
    id: root

    width: 512
    height: 512
    // Floors are intentionally aggressive: the user wants the option
    // to shrink to a postage-stamp mic-only widget. Below ~6 grid
    // units (~108 px @1.0x) the WM's title bar may itself cramp or
    // collapse — that is by design at these sizes. Height floor is
    // slightly higher (gu*8 ≈ 144 px) to keep the mic button from
    // having its bottom curve clipped by the window edge — the
    // alternative would be infinitely-shrinking icon/font sizes,
    // which look terrible.
    minimumWidth: Kirigami.Units.gridUnit * 6
    minimumHeight: Kirigami.Units.gridUnit * 8
    flags: Qt.Window
        | Qt.WindowTitleHint
        | Qt.WindowSystemMenuHint
        | Qt.WindowMinimizeButtonHint
        | Qt.WindowMaximizeButtonHint
        | Qt.WindowCloseButtonHint
    visible: true
    title: i18nCtx.i18n("Voice Agent")
    required property QtObject voiceAgent

    // CompactMode threshold raised from 35 → 40 grid units. v0.8.2 went
    // 25 → 35 to fix offscreen 1.0x captures, but the user's actual
    // Plasma desktop at higher Wayland scale showed the same form-column
    // squeeze + mic-overlap that supposedly was fixed: at 1.5x scale the
    // gridUnit-multiplied control widths (Speech/Voice combos, URL combo
    // + Connect button, mic frame) cumulatively exceed what mediumMode
    // can fit at typical 1000 px windows. 40 grid units gives enough
    // headroom that the form fits at common Plasma scales without
    // requiring an oversized window.
    readonly property bool compactMode: width < Kirigami.Units.gridUnit * 40
    readonly property bool mediumMode: !compactMode
    // ultraCompactMode: the conversation pane is down to ~one line of
    // visible content (mic button + a sliver of conversation feed); any
    // smaller and the conversation is dead space. Below this threshold
    // we hide the conversation entirely and let the mic button fill the
    // window. gridUnit-scaled so the threshold respects Plasma scale.
    readonly property bool ultraCompactMode: compactMode && height < Kirigami.Units.gridUnit * 10
    readonly property int sttInstalledCount: voiceAgent.sttInstalledCount
    readonly property int ttsInstalledCount: voiceAgent.ttsInstalledCount
    readonly property color micPulseColor: voiceAgent.talkReady ? Kirigami.Theme.highlightColor : Kirigami.Theme.disabledTextColor
    readonly property color micButtonColor: voiceAgent.voiceConnectionEnabled ? Kirigami.Theme.highlightColor : Kirigami.Theme.alternateBackgroundColor
    readonly property bool micPulseActive: voiceAgent.voiceConnectionEnabled || voiceAgent.talkReady
    // ultraCompact: hem the mic button against the window edge with no
    // extra padding. compact: a small breathing margin. medium and above:
    // standard spacing.
    readonly property real pageContentMargin: root.ultraCompactMode ? 0 : (root.compactMode ? Kirigami.Units.smallSpacing : (root.mediumMode ? Kirigami.Units.mediumSpacing : Kirigami.Units.largeSpacing))
    readonly property real pageContentSpacing: root.compactMode ? Kirigami.Units.smallSpacing : Kirigami.Units.largeSpacing

    // Wheel handler with two scrolling modes, gated on the ListView's
    // sticky-to-bottom state (see AGENTS.md "KDE/QML implementation
    // memory" — sticky-state owner must not fight Flickable movement):
    //
    //   Mode A (direct, when stickToBottom or no state-machine present):
    //     Direct `contentY` assignment with bounds clamp. Bypasses
    //     Flickable.flick() so the sticky-bottom state machine never
    //     sees movementStarted / movementEnded firings it would treat
    //     as the user manually detaching from bottom.
    //
    //   Mode B (inertial, when user has scrolled up off the tail):
    //     Convert wheel delta into a pixels-per-second velocity and
    //     call `Flickable.flick(0, velocity)` for native momentum
    //     scrolling. The existing onMovementEnded handler in
    //     ConversationPane reattaches stickToBottom if the flick
    //     lands at-or-near bottom (bottomEpsilon = 3 px), so
    //     auto-restore falls out of the existing state machine.
    //
    // Velocity scale: pixelDelta path uses ~40 px/wheel-pixel, angleDelta
    // path uses gridUnit*60 px/sec per 120-unit notch (one notch ≈ 18
    // gridUnits at 1800 px/s² deceleration, comparable to one direct-
    // assignment tick of gridUnit*12). Sign: Qt's positive yVelocity
    // moves content toward the start (scrolls view up), matching the
    // direct path's `contentY -= delta`.
    function scrollList(listView, wheel) {
        if (!listView) {
            return;
        }
        const pdy = wheel.pixelDelta ? wheel.pixelDelta.y : 0;
        const ady = wheel.angleDelta ? wheel.angleDelta.y : 0;
        const delta = pdy !== 0
            ? pdy * 8
            : (ady / 120) * Kirigami.Units.gridUnit * 12;
        const useInertial = (listView.stickToBottom !== undefined)
            ? !listView.stickToBottom
            : !listView.atYEnd;
        if (useInertial && delta !== 0) {
            const velocity = pdy !== 0
                ? pdy * 40
                : (ady / 120) * Kirigami.Units.gridUnit * 60;
            listView.flick(0, velocity);
            wheel.accepted = true;
            return;
        }
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
        text: i18nCtx.i18n("Voice Models")
        icon.name: "folder-cloud-symbolic"
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
        text: i18nCtx.i18n("Theme")
        icon.name: "preferences-desktop-theme-symbolic"
        displayHint: Kirigami.DisplayHint.IconOnly
        visible: !root.compactMode

        Kirigami.Action {
            text: i18nCtx.i18n("Auto")
            checkable: true
            checked: voiceAgent.themeMode === "auto"
            ActionGroup.group: themeActionGroup
            onTriggered: voiceAgent.setThemeMode("auto")
        }

        Kirigami.Action {
            text: i18nCtx.i18n("Light")
            checkable: true
            checked: voiceAgent.themeMode === "light"
            ActionGroup.group: themeActionGroup
            onTriggered: voiceAgent.setThemeMode("light")
        }

        Kirigami.Action {
            text: i18nCtx.i18n("Dark")
            checkable: true
            checked: voiceAgent.themeMode === "dark"
            ActionGroup.group: themeActionGroup
            onTriggered: voiceAgent.setThemeMode("dark")
        }
    }

    Kirigami.Action {
        id: muteAction
        text: voiceAgent.audioMuted ? i18nCtx.i18n("Unmute") : i18nCtx.i18n("Mute")
        icon.name: voiceAgent.audioMuted ? "audio-volume-muted-symbolic" : "audio-volume-high-symbolic"
        enabled: voiceAgent.talkReady
        onTriggered: voiceAgent.setAudioMuted(!voiceAgent.audioMuted)
    }

    // Verbose-log toggle, surfaced on the page header alongside theme /
    // mute / model-manager so all stateful UI affordances live on one
    // command surface rather than scattered across pane headers.
    // Hidden in compact mode (where the page header itself is hidden).
    Kirigami.Action {
        id: verboseLogAction
        text: voiceAgent.logVerboseMode
            ? i18nCtx.i18n("Hide pipeline activity in log")
            : i18nCtx.i18n("Show pipeline activity in log (new entries only)")
        icon.name: voiceAgent.logVerboseMode ? "view-visible-symbolic" : "view-hidden-symbolic"
        displayHint: Kirigami.DisplayHint.IconOnly
        visible: !root.compactMode
        onTriggered: voiceAgent.setLogVerboseMode(!voiceAgent.logVerboseMode)
    }

    // Cycle 9: replay-failure toast. MainWindow.replayMessage(int) emits
    // `replay_failed(QString reason)` when synthesis raises or the voice
    // is not yet `is_available`. Surface the reason via Kirigami's
    // standard passive notification (`"short"` ≈ 4s auto-dismiss) so a
    // failed replay click is visible without persisting in chrome.
    // The Python side already wraps static reasons via i18nCtx and
    // leaves dynamic exception text in English; we display the payload
    // as-is.
    //
    // Wiring uses `Component.onCompleted: signal.connect(handler)` rather
    // than a `Connections { function onReplayFailed(...) }` block. The
    // Python signal is `replay_failed` (snake_case, per the rest of the
    // window.py surface), and Qt's QML Connections signal-handler name
    // resolution does NOT auto-camelCase a snake_case Python signal name
    // at runtime — `onReplayFailed` would silently never fire (and the
    // QML parser warns "no signal of the target matches the name"). The
    // direct `signal.connect(fn)` form binds at runtime regardless of
    // the Python casing convention. The QA-automation suite's
    // `tests/test_replay_toast.py` locks this in.
    Component.onCompleted: {
        if (voiceAgent && voiceAgent.replay_failed) {
            voiceAgent.replay_failed.connect(function(reason) {
                root.showPassiveNotification(reason, "short");
            });
        }
    }

    Window {
        id: modelManagerWindow

        transientParent: root
        modality: Qt.ApplicationModal
        flags: Qt.Dialog | Qt.WindowTitleHint | Qt.WindowCloseButtonHint
        title: i18nCtx.i18n("Voice Models")
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
                            text: i18nCtx.i18n("Voice Models")
                            level: 2
                        }

                        Item {
                            Layout.fillWidth: true
                        }

                        ToolButton {
                            icon.name: "window-close"
                            text: i18nCtx.i18n("Close")
                            onClicked: modelManagerWindow.close()
                        }
                    }

                    Label {
                        Layout.fillWidth: true
                        text: i18nCtx.i18n("Install, remove, and switch local speech models here. Session selectors only show installed items.")
                        wrapMode: Text.WordWrap
                        color: Kirigami.Theme.disabledTextColor
                    }

                    GridLayout {
                        Layout.fillWidth: true
                        columns: root.compactMode ? 1 : 2
                        columnSpacing: Kirigami.Units.largeSpacing

                        Label {
                            Layout.fillWidth: true
                            text: i18nCtx.i18n("%1 STT model(s) installed").arg(root.sttInstalledCount)
                            font.weight: Font.DemiBold
                        }

                        Label {
                            Layout.fillWidth: true
                            text: i18nCtx.i18n("%1 TTS voice(s) installed").arg(root.ttsInstalledCount)
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
                            text: modelManagerWindow.sttFilter
                            onTextChanged: modelManagerWindow.sttFilter = text
                        }

                        CatalogList {
                            id: sttCatalogView
                            catalogModel: voiceAgent.sttCatalogModel
                            filterText: modelManagerWindow.sttFilter
                            selectedName: voiceAgent.selectedSttModel
                            onSelect: function(name) { voiceAgent.selectSttModel(name); }
                            onInstall: function(name) { voiceAgent.installSttModel(name); }
                            onRemove: function(name) { voiceAgent.deleteSttModel(name); }
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
                            text: modelManagerWindow.ttsFilter
                            onTextChanged: modelManagerWindow.ttsFilter = text
                        }

                        CatalogList {
                            id: ttsCatalogView
                            catalogModel: voiceAgent.ttsCatalogModel
                            filterText: modelManagerWindow.ttsFilter
                            selectedName: voiceAgent.selectedTtsModel
                            onSelect: function(name) { voiceAgent.selectTtsModel(name); }
                            onInstall: function(name) { voiceAgent.installTtsModel(name); }
                            onRemove: function(name) { voiceAgent.deleteTtsModel(name); }
                        }
                    }
                }
            }
        }
    }

    // Wrap ConversationPane.qml in an inline Component so the Loader call
    // sites below can pass `voiceAgent` explicitly. ConversationPane has a
    // `required property var voiceAgent`, which means a bare `source:`
    // Loader cannot satisfy the requirement — the property must be set at
    // construction time (Loader.item.voiceAgent = ... in onLoaded fires too
    // late and a `required` property must be wired in the inline binding).
    Component {
        id: conversationPaneComponent

        ConversationPane {
            voiceAgent: root.voiceAgent
        }
    }

    Component {
        id: sessionPaneComponent

        SessionSetupPane {
            voiceAgent: root.voiceAgent
            compactMode: root.compactMode
            mediumMode: root.mediumMode
            micPulseActive: root.micPulseActive
            micButtonColor: root.micButtonColor
            micPulseColor: root.micPulseColor
        }
    }

    pageStack.initialPage: Kirigami.Page {
        id: page
        title: i18nCtx.i18n("Voice Agent")
        actions: [
            themeAction,
            muteAction,
            verboseLogAction,
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

                ColumnLayout {
                    anchors.fill: parent
                    // 250 ms cross-fade when the responsive breakpoint
                    // flips. opacity drives a smooth transition; visible
                    // follows opacity so the Item drops out of layout
                    // after fade. Loader.active still gates instantiation
                    // by mode (smooth fade-IN, instant unload on hide is
                    // an acceptable simplification — the new mode's
                    // content fades in over the disappearing one).
                    opacity: root.mediumMode ? 1 : 0
                    visible: opacity > 0.01
                    spacing: Kirigami.Units.largeSpacing
                    Behavior on opacity { NumberAnimation { duration: 250; easing.type: Easing.InOutQuad } }

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
                    opacity: root.compactMode ? 1 : 0
                    visible: opacity > 0.01
                    Behavior on opacity { NumberAnimation { duration: 250; easing.type: Easing.InOutQuad } }
                    active: root.compactMode
                    sourceComponent: conversationPaneComponent
                }
            }
        }
    }
}
