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
                flickDeceleration: 1800
                maximumFlickVelocity: 24000
                ScrollBar.vertical: ScrollBar {}

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

                // MouseArea overlay captures wheel before the Flickable does.
                // acceptedButtons: Qt.NoButton lets mouse presses pass through to delegates.
                MouseArea {
                    anchors.fill: parent
                    acceptedButtons: Qt.NoButton
                    propagateComposedEvents: true
                    z: 2
                    onWheel: function(wheel) {
                        root.scrollList(conversationView, wheel);
                        conversationView.updateStickinessFromPosition();
                    }
                }

            delegate: Item {
                width: conversationView.width
                readonly property bool systemEntry: model.messageRole === "system"
                readonly property bool statusEntry: model.messageRole === "status"
                implicitHeight: statusEntry
                    ? statusMessage.implicitHeight
                    : (systemEntry ? systemMessage.implicitHeight : messageRow.implicitHeight)

                property bool assistant: model.messageRole === "assistant"
                readonly property string bubbleState: model.bubbleState || "sent"
                readonly property bool draft: bubbleState === "draft"
                // Theme luminance check — Kirigami doesn't expose a
                // "complementary panel" color set, so we hand-pick the
                // assistant bubble palette per theme. ITU-R BT.601
                // luma; the page bg's lightness is the only signal we
                // need (background under 0.5 → dark theme).
                readonly property bool darkTheme: {
                    const bg = Kirigami.Theme.backgroundColor;
                    return (0.299 * bg.r + 0.587 * bg.g + 0.114 * bg.b) < 0.5;
                }
                // Warm coffee / cream palette — complementary (~25°)
                // to the teal/green Plasma highlight typical of Breeze
                // accents (~170°). Inverted lightness across themes
                // keeps the visual identity coherent. Bypasses Kirigami
                // colorSet for the assistant bubble because every
                // available set converges near the page bg in dark
                // mode and reads as flat black.
                readonly property color assistantBg: darkTheme ? "#3d3027" : "#f4e3d0"
                readonly property color assistantTextColor: darkTheme ? "#f4e3d0" : "#3d3027"
                readonly property color systemTextColor: (model.level || "status") === "error"
                    ? Kirigami.Theme.negativeTextColor
                    : Kirigami.Theme.disabledTextColor
                // Hardcoded purple. Kirigami.Theme.linkColor on Breeze is blue,
                // not purple, so we tune this independently. Verified to read
                // against both Breeze Light and Dark backgrounds.
                readonly property color statusTextColor: "#9b6bcc"
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
                    visible: parent.statusEntry
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

                        // User-sent bubbles use Kirigami's `Selection`
                        // colorSet (Plasma "this-is-yours" highlight
                        // pair) so they track the user's accent. The
                        // assistant bubble uses an explicit warm
                        // coffee / cream palette — a complementary
                        // hue to teal/green that Kirigami's color
                        // sets don't offer and that hand-flips
                        // cleanly between Breeze Light and Dark.
                        Kirigami.Theme.colorSet: assistant
                            ? Kirigami.Theme.Window
                            : Kirigami.Theme.Selection
                        Kirigami.Theme.inherit: false

                        // Resolved bg / text per state. Assistant uses
                        // hand-picked palette; user-sent reads from
                        // the Selection colorSet (highlightColor /
                        // highlightedTextColor under the hood); draft
                        // (transcribing) keeps the pink hold-over.
                        readonly property color resolvedBg: draft
                            ? "#ff5c8a"
                            : (assistant ? assistantBg : Kirigami.Theme.backgroundColor)
                        readonly property color resolvedText: draft
                            ? "#ffffff"
                            : (assistant ? assistantTextColor : Kirigami.Theme.textColor)

                        background: Rectangle {
                            radius: root.compactMode ? Kirigami.Units.mediumSpacing : Kirigami.Units.largeSpacing
                            color: bubbleFrame.resolvedBg
                            // Subtle border so the bubble has edge
                            // definition on Breeze Light too (the
                            // assistant cream bg only differs from
                            // page bg by a few %).
                            border.color: Kirigami.Theme.separatorColor
                            border.width: draft ? 0 : 1
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
