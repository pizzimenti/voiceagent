import QtQuick
import QtQuick.Controls
import QtTest
import org.kde.kirigami 2.20 as Kirigami
import "." as Stubs

// Lighter structural sanity check for MainWindow.qml after PR #22.
// Verifies the page-header `Kirigami.Action` items the v0.8.0 polish
// migration introduced exist on the loaded window, and that the
// verbose-log action specifically is reachable through the page's
// `actions` array.
//
// MainWindow.qml is a top-level `Kirigami.ApplicationWindow`, so we
// instantiate it via `Qt.createComponent` and `createObject(null, ...)`
// (null parent = standalone window) rather than nesting inside the
// harness Item. We then inspect `pageStack.initialPage.actions`.
TestCase {
    id: testCase
    name: "MainWindow"
    when: windowShown

    Stubs.StubVoiceAgent { id: stubAgent }

    property var window: null

    function init() {
        const url = Qt.resolvedUrl("../../src/voiceagent/qml/MainWindow.qml");
        const component = Qt.createComponent(url);
        verify(component.status === Component.Ready,
            "MainWindow.qml loaded: " + component.errorString());
        window = component.createObject(null, { voiceAgent: stubAgent });
        verify(window !== null, "MainWindow.qml instantiated");
        // The window may auto-show; explicit hide keeps headless runs
        // from popping anything onto $DISPLAY.
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

    function test_page_actions_present() {
        verify(window.pageStack !== null);
        const page = window.pageStack.initialPage;
        verify(page !== null);
        const actions = page.actions || [];
        verify(actions.length >= 4,
            "page exposes at least 4 header actions; saw " + actions.length);
    }

    function test_verbose_log_action_present() {
        const page = window.pageStack.initialPage;
        const actions = page.actions || [];
        let foundVerboseLog = false;
        for (let i = 0; i < actions.length; i += 1) {
            const a = actions[i];
            if (!a) {
                continue;
            }
            const text = (a.text || "").toLowerCase();
            const icon = (a.icon && a.icon.name) ? a.icon.name : "";
            if (text.indexOf("pipeline") !== -1
                    || icon.indexOf("view-visible") !== -1
                    || icon.indexOf("view-hidden") !== -1) {
                foundVerboseLog = true;
                break;
            }
        }
        verify(foundVerboseLog,
            "verbose-log toggle action present on page-header actions");
    }

    function test_voice_agent_null_falls_back_to_inert_state() {
        window.voiceAgent = null;
        compare(window.sttInstalledCount, 0);
        compare(window.ttsInstalledCount, 0);
    }
}
