import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.kde.kirigami 2.20 as Kirigami

// Reusable microphone toggle button used by the medium and compact mic
// panes. The animated glow frame (Item + SequentialAnimation) stays at
// the callsite because each pane references its own `*Mode` predicate; this
// component takes the resulting glow opacity / scale as bindable inputs.
Button {
    id: micButton

    // Required injection of the backend controller. Each callsite must
    // pass `voiceAgent: voiceAgent` explicitly. Internal `voiceAgent.*`
    // bindings below all use the `voiceAgent ? ... : fallback` ternary
    // form because nested-component instantiation can evaluate child
    // bindings before the parent's outer `voiceAgent: voiceAgent`
    // binding lands — without the lazy guard, first-paint TypeErrors
    // fire (PR #5 was reverted for exactly this reason).
    required property var voiceAgent

    // Bindable customization properties (defaults match the medium variant).
    property real iconSize: 34
    property real fontPixel: 11
    property real borderWidth: 3
    property real glowOpacity: 1.0
    property real glowScaleSource: 1.0
    property color buttonColor: "white"
    property color pulseColor: "white"
    // When true, the button uses full opacity (matches the running-pulse
    // visual). Defaults to true so callsites without a pulse animation
    // (e.g. compact mode) get the natural opaque look without extra wiring.
    property bool pulseActive: true

    enabled: micButton.voiceAgent ? micButton.voiceAgent.talkReady : false
    onClicked: {
        if (micButton.voiceAgent) {
            micButton.voiceAgent.setVoiceConnectionEnabled(!micButton.voiceAgent.voiceConnectionEnabled);
        }
    }

    display: AbstractButton.TextUnderIcon
    text: micButton.voiceAgent ? micButton.voiceAgent.micStatusLabel : ""
    font.pixelSize: micButton.fontPixel
    icon.name: "audio-input-microphone"
    icon.width: micButton.iconSize
    icon.height: micButton.iconSize
    icon.color: "white"
    palette.buttonText: "white"

    scale: micButton.glowScaleSource
    opacity: micButton.pulseActive ? 1 : 0.92

    // Custom contentItem to wordwrap the status label rather than elide.
    // Qt's default `IconLabel` for AbstractButton truncates with "..." at
    // narrow widths, which clipped "No model loaded" → "No model lo..."
    // at the smaller window sizes ultraCompactMode now allows. This
    // ColumnLayout lets long status strings wrap to two/three lines
    // instead of disappearing.
    contentItem: ColumnLayout {
        spacing: Kirigami.Units.smallSpacing

        Item { Layout.fillHeight: true }

        Kirigami.Icon {
            Layout.alignment: Qt.AlignHCenter
            source: micButton.icon.name
            implicitWidth: micButton.iconSize
            implicitHeight: micButton.iconSize
            color: micButton.icon.color
            isMask: true
        }

        Label {
            Layout.alignment: Qt.AlignHCenter
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.smallSpacing
            Layout.rightMargin: Kirigami.Units.smallSpacing
            text: micButton.text
            font.pixelSize: micButton.fontPixel
            color: micButton.palette.buttonText
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            elide: Text.ElideNone
        }

        Item { Layout.fillHeight: true }
    }

    background: Rectangle {
        radius: height / 2
        color: micButton.buttonColor
        border.width: micButton.borderWidth
        border.color: Qt.rgba(micButton.pulseColor.r,
                              micButton.pulseColor.g,
                              micButton.pulseColor.b,
                              Math.max(0.7, micButton.glowOpacity))
    }
}
