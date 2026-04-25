import QtQuick
import QtQuick.Controls
import org.kde.kirigami 2.20 as Kirigami

// Reusable microphone toggle button used by the medium, compact, and large
// mic panes. The animated glow frame (Item + SequentialAnimation) stays at
// the callsite because each pane references its own `*Mode` predicate; this
// component takes the resulting glow opacity / scale as bindable inputs.
Button {
    id: micButton

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

    enabled: voiceAgent.talkReady
    onClicked: voiceAgent.setVoiceConnectionEnabled(!voiceAgent.voiceConnectionEnabled)

    display: AbstractButton.TextUnderIcon
    text: voiceAgent.micStatusLabel
    font.pixelSize: micButton.fontPixel
    icon.name: "audio-input-microphone"
    icon.width: micButton.iconSize
    icon.height: micButton.iconSize
    icon.color: "white"
    palette.buttonText: "white"

    scale: micButton.glowScaleSource
    opacity: micButton.pulseActive ? 1 : 0.92

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
