import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.kde.kirigami 2.20 as Kirigami

// Reusable wrapper around MicButton that adds an optional breathing-pulse
// animation. Used by both the medium-mode session pane (animated) and the
// compact-mode conversation pane (static, no animation).
//
// `animatePulse` toggles the SequentialAnimation between the two glow-state
// keyframes. When false, the inner MicButton runs with the externally-pinned
// `glowOpacity` / `glowScale` values directly (compact mode pins them).
//
// `voiceAgent` is required so MicButton's internal bindings (talkReady,
// voiceConnectionEnabled, micStatusLabel) resolve, and so the animation's
// target glow opacity / duration can read voiceConnectionEnabled to vary
// pulse intensity between idle vs. live mic.
Item {
    id: micFrame

    // Required injection of the backend controller. Internal `voiceAgent.*`
    // bindings use the `voiceAgent ? ... : fallback` ternary form because
    // nested-component instantiation can evaluate child bindings before
    // the parent's outer `voiceAgent: voiceAgent` binding lands — without
    // the lazy guard, first-paint TypeErrors fire.
    required property var voiceAgent

    // MicButton appearance pass-throughs.
    property real iconSize: 34
    property real fontPixel: 11
    property real borderWidth: 3
    property color buttonColor: "white"
    property color pulseColor: "white"
    property bool pulseActive: true

    // When true, the SequentialAnimation drives `glowOpacity` and
    // `glowScale`. When false, the values stay at whatever the caller
    // pinned them to (compact-mode does this with 0.85 / 1.0).
    property bool animatePulse: false

    // Glow values fed to the inner MicButton. Default to a pinned static
    // visual so a non-animating callsite without overrides still looks
    // correct; the animation overwrites these at runtime when active.
    property real glowOpacity: 0.85
    property real glowScale: 1.0

    SequentialAnimation {
        running: micFrame.animatePulse && micFrame.pulseActive
        loops: Animation.Infinite

        ParallelAnimation {
            NumberAnimation {
                target: micFrame
                property: "glowOpacity"
                to: (micFrame.voiceAgent && micFrame.voiceAgent.voiceConnectionEnabled) ? 1.0 : 0.78
                duration: (micFrame.voiceAgent && micFrame.voiceAgent.voiceConnectionEnabled) ? 700 : 1200
                easing.type: Easing.InOutSine
            }
            NumberAnimation {
                target: micFrame
                property: "glowScale"
                to: 1.02
                duration: (micFrame.voiceAgent && micFrame.voiceAgent.voiceConnectionEnabled) ? 700 : 1200
                easing.type: Easing.InOutSine
            }
        }

        ParallelAnimation {
            NumberAnimation {
                target: micFrame
                property: "glowOpacity"
                to: (micFrame.voiceAgent && micFrame.voiceAgent.voiceConnectionEnabled) ? 0.45 : 0.35
                duration: (micFrame.voiceAgent && micFrame.voiceAgent.voiceConnectionEnabled) ? 700 : 1200
                easing.type: Easing.InOutSine
            }
            NumberAnimation {
                target: micFrame
                property: "glowScale"
                to: 1.0
                duration: (micFrame.voiceAgent && micFrame.voiceAgent.voiceConnectionEnabled) ? 700 : 1200
                easing.type: Easing.InOutSine
            }
        }
    }

    MicButton {
        anchors.fill: parent
        anchors.margins: 0
        voiceAgent: micFrame.voiceAgent
        iconSize: micFrame.iconSize
        fontPixel: micFrame.fontPixel
        borderWidth: micFrame.borderWidth
        glowOpacity: micFrame.glowOpacity
        glowScaleSource: micFrame.glowScale
        buttonColor: micFrame.buttonColor
        pulseColor: micFrame.pulseColor
        pulseActive: micFrame.pulseActive
    }
}
