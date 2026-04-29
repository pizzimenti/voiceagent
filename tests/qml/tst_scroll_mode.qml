import QtQuick
import QtQuick.Controls
import QtTest
import org.kde.kirigami 2.20 as Kirigami
import "." as Stubs

// Quick Test for the inertial wheel-scroll branch logic added in PR
// #23. `MainWindow.scrollList(listView, wheel)` selects between two
// scroll modes based on `listView.stickToBottom`:
//
//   stickToBottom === false  → inertial: listView.flick(0, velocity)
//   stickToBottom === true   → direct  : listView.contentY = ...
//
// Pytest can't easily reach a method living on a QML root object, so
// the test runs as a Quick Test: load MainWindow.qml, build mock
// listView + wheel objects exposing the surface scrollList reads,
// and verify the right branch fired.
//
// We don't instantiate a real ListView — the goal is to lock in the
// branch behavior, not to drive Qt's flick engine. Both branches are
// covered.
TestCase {
    id: testCase
    name: "ScrollMode"
    when: windowShown

    Stubs.StubVoiceAgent { id: stubAgent }

    property var window: null

    function init() {
        const url = Qt.resolvedUrl("../../src/voiceagent/qml/MainWindow.qml");
        const component = Qt.createComponent(url);
        verify(component.status === Component.Ready,
            "MainWindow.qml loaded: " + component.errorString());
        window = component.createObject(null, { voiceAgent: stubAgent });
        verify(window !== null);
        if (window.hasOwnProperty("visible")) {
            window.visible = false;
        }
    }

    function cleanup() {
        if (window !== null) {
            window.destroy();
            window = null;
        }
    }

    // Build a mock ListView exposing the surface scrollList probes.
    // QML doesn't have plain JS objects-with-methods that bind cleanly
    // to scrollList's `wheel.pixelDelta.y` etc; use a QtObject so all
    // the ".y" / ".y" lookups resolve through the real QML property
    // system.
    function _mockListView(stickToBottom) {
        return {
            stickToBottom: stickToBottom,
            contentY: 100,
            originY: 0,
            contentHeight: 1000,
            height: 400,
            flickCalls: [],
            contentYAssignments: [],
            // Capture flick(x, y) calls.
            flick: function(vx, vy) {
                this.flickCalls.push({ vx: vx, vy: vy });
            },
        };
    }

    function _mockWheel(pixelDeltaY, angleDeltaY) {
        return {
            pixelDelta: { y: pixelDeltaY },
            angleDelta: { y: angleDeltaY },
            accepted: false,
        };
    }

    function test_inertial_branch_fires_flick_when_unstuck() {
        // stickToBottom false + positive pixelDelta.y → inertial path:
        // flick(0, velocity) with velocity = pdy * 160 > 0 (positive y
        // velocity scrolls content toward the start).
        const listView = _mockListView(false);
        const wheel = _mockWheel(5, 0);
        const startContentY = listView.contentY;
        window.scrollList(listView, wheel);
        compare(listView.flickCalls.length, 1,
            "inertial branch should call flick exactly once");
        compare(listView.flickCalls[0].vx, 0,
            "flick x velocity must be 0 (vertical-only scroll)");
        verify(listView.flickCalls[0].vy > 0,
            "flick y velocity must be positive for upward wheel "
            + "(saw " + listView.flickCalls[0].vy + ")");
        // Magnitude check: pdy=5 → velocity=5*160=800 (v0.9.11 doubled
        // the v0.9.9 inertial multiplier; combined with halved
        // ListView.flickDeceleration this gives ~8x glide of v0.9.9).
        compare(listView.flickCalls[0].vy, 800,
            "pixelDelta path: velocity = pdy * 160");
        // Direct path must NOT have run: contentY unchanged from
        // assignment (we can't check that the setter wasn't called
        // without a Q_PROPERTY change-tracker, so we assert the value
        // is untouched at the JS-level — which the inertial branch
        // does NOT touch).
        compare(listView.contentY, startContentY,
            "inertial branch must not assign contentY directly");
        verify(wheel.accepted, "wheel.accepted must be set on consume");
    }

    function test_direct_branch_assigns_contentY_when_stuck() {
        // stickToBottom true → direct path: contentY = clamp(...)
        // with delta = pdy * 16 = 5 * 16 = 80, so new contentY =
        // clamp(originY=0, max=600, 100 - 80) = 20. (v0.9.9 doubled
        // the v0.8.0 PR #23 baseline of pdy*8.)
        const listView = _mockListView(true);
        const wheel = _mockWheel(5, 0);
        window.scrollList(listView, wheel);
        compare(listView.flickCalls.length, 0,
            "direct branch must NOT call flick");
        compare(listView.contentY, 20,
            "direct branch sets contentY to clamp(0, 600, 100 - 80)");
        verify(wheel.accepted);
    }

    function test_direct_branch_clamps_at_origin() {
        // contentY would go below originY=0 — clamp to 0.
        const listView = _mockListView(true);
        listView.contentY = 10;
        const wheel = _mockWheel(50, 0); // delta = 400, would yield -390
        window.scrollList(listView, wheel);
        compare(listView.contentY, 0, "direct branch clamps at originY=0");
    }

    function test_inertial_branch_uses_angle_delta_when_no_pixel() {
        // pixelDelta.y == 0, angleDelta.y == 120 → velocity =
        // (120/120) * gridUnit * 60 = gridUnit * 60. Sign: positive
        // (Qt convention: positive yVelocity scrolls content up).
        const listView = _mockListView(false);
        const wheel = _mockWheel(0, 120);
        window.scrollList(listView, wheel);
        compare(listView.flickCalls.length, 1);
        verify(listView.flickCalls[0].vy > 0,
            "angleDelta path: positive notch → positive velocity");
    }

    function test_zero_delta_is_noop() {
        const listView = _mockListView(false);
        const wheel = _mockWheel(0, 0);
        const startContentY = listView.contentY;
        window.scrollList(listView, wheel);
        compare(listView.flickCalls.length, 0,
            "zero delta must not call flick");
        compare(listView.contentY, startContentY,
            "zero delta must leave contentY untouched");
    }
}
