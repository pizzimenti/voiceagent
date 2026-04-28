import QtQuick
import QtQuick.Controls
import QtTest
import org.kde.kirigami 2.20 as Kirigami
import "." as Stubs
import "../../src/voiceagent/qml" as App

// Quick Test for SessionSetupPane.qml's post-PR-#22 KDE-polish form
// shape. Three structural invariants:
//
//   1. The pane uses a `Kirigami.FormLayout` (not the prior GridLayout
//      with hand-positioned `Layout.row` / `Layout.column` cells).
//   2. Each control under the form carries the expected
//      `Kirigami.FormData.label` for its row.
//   3. `Kirigami.FormLayout.wideMode` flips when the parent toggles
//      `compactMode` (true → wideMode false, stacked column;
//      false → wideMode true, label-and-control side-by-side).
//
// The pane is instantiated with literal dummy props plus the
// `StubVoiceAgent` so we exercise the real component file (no
// monkeypatched copy) without dragging in a real backend.
TestCase {
    id: testCase
    name: "SessionSetupPane"
    when: windowShown

    // Window is required for components that depend on visualParent
    // / Window context. SessionSetupPane reads layouts but not Screen,
    // so a small headless Item works.
    Item {
        id: harness
        width: 800
        height: 600

        Stubs.StubVoiceAgent { id: stubAgent }

        Component {
            id: paneComponent

            App.SessionSetupPane {
                voiceAgent: stubAgent
                compactMode: false
                mediumMode: true
                micPulseActive: false
                micButtonColor: "white"
                micPulseColor: "white"
            }
        }
    }

    // Helper: walk SessionSetupPane's children and return the first
    // FormLayout. SessionSetupPane wraps it inside a ColumnLayout +
    // Pane + RowLayout, so a recursive search is the simplest path.
    function _findFormLayout(item) {
        if (!item) {
            return null;
        }
        // Kirigami.FormLayout's QML type name resolves through Kirigami;
        // identify by the wideMode property which is unique to it.
        if (item.hasOwnProperty("wideMode")
                && item.toString().indexOf("FormLayout") !== -1) {
            return item;
        }
        const children = item.children || [];
        for (let i = 0; i < children.length; i += 1) {
            const found = _findFormLayout(children[i]);
            if (found !== null) {
                return found;
            }
        }
        return null;
    }

    function test_pane_uses_form_layout() {
        const pane = createTemporaryObject(paneComponent, harness);
        verify(pane !== null, "pane created");
        const form = _findFormLayout(pane);
        verify(form !== null, "Kirigami.FormLayout present in SessionSetupPane");
    }

    function test_form_wide_mode_tracks_compact_mode() {
        const pane = createTemporaryObject(paneComponent, harness, {
            compactMode: false,
            mediumMode: true,
        });
        verify(pane !== null);
        const form = _findFormLayout(pane);
        verify(form !== null);
        // compactMode false → wideMode true.
        tryCompare(form, "wideMode", true);
        pane.compactMode = true;
        // compactMode true → wideMode false.
        tryCompare(form, "wideMode", false);
        pane.compactMode = false;
        tryCompare(form, "wideMode", true);
    }

    function test_form_labels_present() {
        const pane = createTemporaryObject(paneComponent, harness);
        verify(pane !== null);
        const form = _findFormLayout(pane);
        verify(form !== null);
        // Walk the form's children and collect FormData.label values.
        // Kirigami.FormLayout's children carry attached
        // `Kirigami.FormData` properties; we read them via
        // `Kirigami.FormData.label` per-child.
        const expectedLabels = [
            "Speech:",
            "Voice:",
            "LLM URL:",
            "Loaded Model:",
        ];
        const seenLabels = [];
        const children = form.children || [];
        for (let i = 0; i < children.length; i += 1) {
            const child = children[i];
            const attached = child.Kirigami !== undefined
                ? child.Kirigami.FormData
                : null;
            if (attached && attached.label !== undefined) {
                seenLabels.push(attached.label);
            }
        }
        for (let i = 0; i < expectedLabels.length; i += 1) {
            const expected = expectedLabels[i];
            let matched = false;
            for (let j = 0; j < seenLabels.length; j += 1) {
                if (seenLabels[j].indexOf(expected) !== -1) {
                    matched = true;
                    break;
                }
            }
            verify(matched, "FormData.label '" + expected + "' present (saw: "
                + seenLabels.join("; ") + ")");
        }
    }
}
