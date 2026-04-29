import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.kde.kirigami 2.20 as Kirigami

// Conversation pane shown by every responsive layout (medium/compact).
// Hosts the rolling transcript ListView and scroll-to-bottom button. The
// compact-mode mic button is rendered at page level by MainWindow.qml;
// `micAnchor` here reserves the layout slot the mic occupies in this
// pane.
//
// `root` here is aliased to the containing ApplicationWindow so the existing
// bindings (root.compactMode, root.bubbleText(...), root.scrollList(...))
// keep resolving against the MainWindow scope after the move out of the
// inline Component definition.
Pane {
    id: conversationPane

    // Required injection of the backend controller. Each callsite must
    // pass `voiceAgent: voiceAgent` explicitly. Internal `voiceAgent.*`
    // bindings below all use the `voiceAgent ? ... : fallback` ternary
    // form because nested-component instantiation can evaluate child
    // bindings before the parent's outer `voiceAgent: voiceAgent`
    // binding lands — without the lazy guard, first-paint TypeErrors
    // fire (PR #5 was reverted for exactly this reason).
    required property var voiceAgent

    // Exposed so MainWindow.qml's page-level mic can bind its geometry
    // to this pane's reserved mic slot via Loader.item.micAnchor.
    property alias micAnchor: micAnchorItem

    readonly property var root: ApplicationWindow.window

    // ultraCompact: zero padding so the mic button reaches the pane
    // edges. compact/medium: small breathing room.
    padding: root.ultraCompactMode ? 0 : (root.compactMode ? Kirigami.Units.smallSpacing : (root.mediumMode ? Kirigami.Units.smallSpacing : Kirigami.Units.mediumSpacing))

    ColumnLayout {
        id: conversationContent
        anchors.fill: parent
        spacing: root.compactMode ? Kirigami.Units.smallSpacing : Kirigami.Units.mediumSpacing

        RowLayout {
            Layout.fillWidth: true
            // In ultraCompactMode the entire conversation feed is hidden
            // and the mic fills the window — collapse this header row too
            // so it doesn't reserve a tiny empty band above the mic.
            visible: !root.ultraCompactMode

            Kirigami.Heading {
                visible: !root.compactMode
                text: i18nCtx.i18n("Conversation")
                level: 2
            }

            Item {
                Layout.fillWidth: true
            }

            // The verbose-log toggle that previously sat here became
            // `verboseLogAction` on the page header in MainWindow.qml
            // (Kirigami.Action conversion). Keeping all stateful page
            // affordances on one command surface reads more naturally
            // than scattering them across pane headers.

            Label {
                visible: !root.compactMode
                text: (conversationPane.voiceAgent && conversationPane.voiceAgent.voiceConnectionEnabled) ? i18nCtx.i18n("Live") : i18nCtx.i18n("Idle")
                color: Kirigami.Theme.disabledTextColor
            }
        }

        Item {
            id: conversationFeedArea
            // Conversation feed area. In ultraCompactMode the conversation
            // is dead space — collapse it via Layout.maximumHeight so the
            // mic button below smoothly slides up into the freed space
            // (rather than fading and leaving empty room). 250 ms.
            Layout.fillWidth: true
            Layout.fillHeight: !root.ultraCompactMode
            Layout.maximumHeight: root.ultraCompactMode ? 0 : 100000
            visible: Layout.maximumHeight > 0
            Behavior on Layout.maximumHeight { NumberAnimation { duration: 250; easing.type: Easing.OutCubic } }

            ListView {
                id: conversationView
                anchors.fill: parent
                clip: true
                spacing: Kirigami.Units.smallSpacing
                model: conversationPane.voiceAgent ? conversationPane.voiceAgent.conversationModel : null
                boundsBehavior: Flickable.StopAtBounds
                // Default Flickable deceleration is restored — the
                // hand-tuned scrollList curve (v0.8.0 → v0.9.11) is
                // gone in favor of `Kirigami.WheelHandler` below,
                // which manages velocity itself.
                maximumFlickVelocity: 24000
                ScrollBar.vertical: ScrollBar {}

                // Kirigami's purpose-built wheel handler. Replaces the
                // custom MouseArea + MainWindow.scrollList curve that
                // had to be retuned three times (v0.9.9 / .11 / .x)
                // and still shipped at 1/5 native speed on Plasma 6 /
                // Wayland with high-resolution scroll input. The
                // handler routes wheel events into the underlying
                // Flickable's native flick, which gives us proper
                // inertia and source-aware velocity (mouse wheel,
                // hi-res mouse, touchpad continuous events) for free.
                Kirigami.WheelHandler {
                    target: conversationView
                    filterMouseEvents: true
                }

                // The model is stable; only rows mutate. Keep scrolling deterministic:
                // stick to the true bottom until the user intentionally leaves it.
                property bool stickToBottom: true
                property bool adjustingScroll: false
                readonly property real bottomEpsilon: 3

                function bottomContentY() {
                    return originY + Math.max(0, contentHeight - height);
                }

                function isAtBottom() {
                    return contentY >= bottomContentY() - bottomEpsilon;
                }

                function forceBottom() {
                    adjustingScroll = true;
                    contentY = bottomContentY();
                    adjustingScroll = false;
                }

                function scheduleBottomStick() {
                    if (!stickToBottom) {
                        return;
                    }
                    forceBottom();
                    bottomStickTimer.restart();
                }

                function scrollToBottom() {
                    stickToBottom = true;
                    scheduleBottomStick();
                }

                function updateStickinessFromPosition() {
                    if (!adjustingScroll) {
                        stickToBottom = isAtBottom();
                    }
                }

                onCountChanged: scheduleBottomStick()
                onContentHeightChanged: scheduleBottomStick()
                onHeightChanged: scheduleBottomStick()
                onWidthChanged: scheduleBottomStick()
                onContentYChanged: updateStickinessFromPosition()
                onMovementStarted: {
                    if (!adjustingScroll) {
                        stickToBottom = false;
                    }
                }
                onMovementEnded: {
                    if (!adjustingScroll) {
                        stickToBottom = isAtBottom();
                        scheduleBottomStick();
                    }
                }

                Timer {
                    id: bottomStickTimer
                    interval: 0
                    repeat: false
                    onTriggered: {
                        if (conversationView.stickToBottom) {
                            conversationView.forceBottom();
                        }
                    }
                }

            delegate: Item {
                width: conversationView.width
                readonly property bool systemEntry: model.messageRole === "system"
                readonly property bool statusEntry: model.messageRole === "status"
                // Verbose toggle hides existing status entries on the
                // fly. The coordinator already gates *new* status rows
                // on logVerboseMode, but rows already in the model
                // would otherwise stay visible until restart. Read the
                // live property here so toggling rebuilds the layout.
                readonly property bool verboseMode: conversationPane.voiceAgent
                    ? conversationPane.voiceAgent.logVerboseMode
                    : false
                readonly property bool statusEntryVisible: statusEntry && verboseMode
                implicitHeight: statusEntry
                    ? (verboseMode ? statusMessage.implicitHeight : 0)
                    : (systemEntry ? systemMessage.implicitHeight : messageRow.implicitHeight)
                visible: !statusEntry || verboseMode

                property bool assistant: model.messageRole === "assistant"
                readonly property string bubbleState: model.bubbleState || "sent"
                readonly property bool draft: bubbleState === "draft"
                // Theme luminance check — we override Kirigami's
                // color sets entirely for both bubble sides to land a
                // consistent violet "AI app" identity instead of
                // tracking the user's Plasma accent. ITU-R BT.601
                // luma; bg under 0.5 → dark theme.
                readonly property bool darkTheme: {
                    const bg = Kirigami.Theme.backgroundColor;
                    return (0.299 * bg.r + 0.587 * bg.g + 0.114 * bg.b) < 0.5;
                }
                // Violet branded palette — `sent` is the user's bubble
                // (saturated violet pill, no border); `recv` is the
                // assistant's bubble (soft lavender on light, cool
                // gray on dark, with explicit border). Hand-tuned per
                // theme — Kirigami color sets all converge near page
                // bg on dark and don't give us this visual identity.
                readonly property color sentBg: darkTheme ? "#A78BFA" : "#7C3AED"
                readonly property color sentTextColor: darkTheme ? "#140A2B" : "#FFFFFF"
                readonly property color recvBg: darkTheme ? "#374151" : "#EDE9FE"
                readonly property color recvTextColor: darkTheme ? "#F9FAFB" : "#1F1736"
                readonly property color recvBorderColor: darkTheme ? "#475569" : "#DDD6FE"
                readonly property color systemTextColor: (model.level || "status") === "error"
                    ? Kirigami.Theme.negativeTextColor
                    : Kirigami.Theme.disabledTextColor
                // Status-row purple, hand-tuned per theme. Original
                // single `#9b6bcc` was too light against white in
                // Light mode and too dark against the page bg in
                // Dark mode. Same hue (~270°), shifted lightness:
                //   - Light theme: deeper purple for stronger
                //     contrast on white.
                //   - Dark theme : softer lavender for stronger
                //     contrast on the dark page bg.
                readonly property color statusTextColor: darkTheme ? "#bf95e8" : "#7b4ab8"
                readonly property real maxBubbleWidth: Math.min(
                    conversationView.width * (root.compactMode ? 0.96 : (root.mediumMode ? 0.9 : 0.78)),
                    Kirigami.Units.gridUnit * (root.compactMode ? 18 : (root.mediumMode ? 28 : 34))
                )

                Label {
                    id: systemMessage
                    visible: parent.systemEntry
                    width: parent.width
                    text: (model.timestampLabel || "") + ((model.timestampLabel || "") ? "  " : "") + root.bubbleText(model.text)
                    wrapMode: Text.WordWrap
                    color: parent.systemTextColor
                    textFormat: Text.PlainText
                    horizontalAlignment: Text.AlignLeft
                    verticalAlignment: Text.AlignVCenter
                    font.pixelSize: 12
                }

                Label {
                    id: statusMessage
                    visible: parent.statusEntryVisible
                    width: parent.width
                    text: root.bubbleText(model.text)
                    wrapMode: Text.WordWrap
                    color: parent.statusTextColor
                    textFormat: Text.PlainText
                    horizontalAlignment: Text.AlignLeft
                    verticalAlignment: Text.AlignVCenter
                    font.pixelSize: 12
                    font.italic: true
                }

                RowLayout {
                    id: messageRow
                    width: parent.width
                    visible: !parent.systemEntry && !parent.statusEntry
                    spacing: Kirigami.Units.smallSpacing
                    layoutDirection: assistant ? Qt.LeftToRight : Qt.RightToLeft

                    Frame {
                        id: bubbleFrame
                        Layout.fillWidth: true
                        Layout.maximumWidth: maxBubbleWidth

                        // Disable Kirigami theme inheritance — both
                        // bubble sides ship explicit branded colors,
                        // so the resolved colorSet is irrelevant for
                        // bg/text. We still set Window so anything
                        // inside that *does* read theme roles falls
                        // back to a sane default.
                        Kirigami.Theme.colorSet: Kirigami.Theme.Window
                        Kirigami.Theme.inherit: false

                        // Resolved bg / text / border per role and
                        // state. Sent (user) is a saturated violet
                        // pill with no border; recv (assistant) is a
                        // soft panel with an explicit lavender/cool-
                        // gray border; draft (transcribing) keeps the
                        // pink hold-over as a transient marker.
                        readonly property color resolvedBg: draft
                            ? "#ff5c8a"
                            : (assistant ? recvBg : sentBg)
                        readonly property color resolvedText: draft
                            ? "#ffffff"
                            : (assistant ? recvTextColor : sentTextColor)
                        readonly property bool showBorder: !draft && assistant

                        background: Rectangle {
                            radius: root.compactMode ? Kirigami.Units.mediumSpacing : Kirigami.Units.largeSpacing
                            color: bubbleFrame.resolvedBg
                            border.color: bubbleFrame.showBorder ? recvBorderColor : "transparent"
                            border.width: bubbleFrame.showBorder ? 1 : 0
                        }

                        contentItem: ColumnLayout {
                            spacing: 4

                            Label {
                                visible: !root.compactMode
                                text: assistant ? i18nCtx.i18n("Assistant") : i18nCtx.i18n("You")
                                color: bubbleFrame.resolvedText
                                opacity: 0.8
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                            }

                            Label {
                                Layout.fillWidth: true
                                text: root.bubbleText(model.text)
                                wrapMode: Text.WordWrap
                                color: bubbleFrame.resolvedText
                                textFormat: Text.PlainText
                            }

                            Label {
                                visible: !!(model.timestampLabel || "")
                                Layout.fillWidth: true
                                text: model.timestampLabel || ""
                                // Track resolvedText at 0.72 opacity so
                                // the timestamp reads on every variant.
                                color: Qt.rgba(bubbleFrame.resolvedText.r,
                                               bubbleFrame.resolvedText.g,
                                               bubbleFrame.resolvedText.b,
                                               0.72)
                                font.pixelSize: 10
                                horizontalAlignment: Text.AlignLeft
                            }
                        }
                    }

                    Button {
                        // Coerce `model.replayable` to bool — the role
                        // returns `undefined` for rows that never set
                        // it (system / status / draft entries), and
                        // QML's `visible: ... && undefined` produces a
                        // log warning per row per resize.
                        visible: !root.compactMode && !!model.replayable
                        text: i18nCtx.i18n("Replay")
                        Layout.alignment: Qt.AlignBottom
                        onClicked: {
                            if (conversationPane.voiceAgent) {
                                conversationPane.voiceAgent.replayMessage(index);
                            }
                        }
                    }
                }
            }

                footer: Kirigami.PlaceholderMessage {
                    width: conversationView.width
                    visible: conversationView.count === 0
                    text: i18nCtx.i18n("Spoken turns will appear here once voice mode is active.")
                }
            }

            Button {
                id: scrollToBottomButton
                // Also gate on !ultraCompactMode — the scroll-to-bottom
                // button is anchored to the conversation feed area; when
                // that area collapses to 0 height, the button still
                // renders at parent.bottom which floats it on top of
                // the (otherwise empty) collapsed area.
                visible: !root.ultraCompactMode && conversationView.contentHeight > conversationView.height && !conversationView.isAtBottom()
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                anchors.rightMargin: Kirigami.Units.largeSpacing
                anchors.bottomMargin: Kirigami.Units.largeSpacing
                text: "↓"
                font.pixelSize: 16
                padding: Kirigami.Units.smallSpacing
                width: Kirigami.Units.gridUnit * 2
                height: Kirigami.Units.gridUnit * 2
                opacity: 0.85
                z: 10
                ToolTip.visible: hovered
                ToolTip.text: i18nCtx.i18n("Scroll to bottom")
                onClicked: {
                    conversationView.scrollToBottom();
                }
                background: Rectangle {
                    radius: height / 2
                    color: Kirigami.Theme.highlightColor
                    border.width: 1
                    border.color: Qt.rgba(0, 0, 0, 0.3)
                }
                contentItem: Label {
                    text: scrollToBottomButton.text
                    color: Kirigami.Theme.highlightedTextColor
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
            }
        }

        Item {
            id: micAnchorItem
            objectName: "micAnchor"
            visible: root.compactMode
            Layout.fillWidth: true
            // ultraCompact: claim all freed vertical space (the conversation
            // area's Layout.maximumHeight collapses to 0, fillHeight here
            // takes the rest). compact (with conversation): fixed gu*5 strip
            // at the bottom.
            Layout.fillHeight: root.ultraCompactMode
            Layout.preferredHeight: root.ultraCompactMode ? -1 : Kirigami.Units.gridUnit * 5
            Layout.minimumHeight: Kirigami.Units.gridUnit * 4
        }
    }
}
