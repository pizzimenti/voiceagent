import QtQuick
import QtQuick.Controls

// Minimal voiceAgent stand-in for Quick Tests of components that bind
// to `voiceAgent.*`. Exposes the surface SessionSetupPane,
// ConversationPane, and MicButton(Frame) read at construction. Slot-
// shaped methods return immediately so click-side wiring has something
// to call into without fanning out to backend logic.
//
// Kept as a QtObject (not a real voiceAgent) — the production
// `MainWindow` class is the single source of truth for the slot
// surface, and this stub deliberately does NOT try to mirror it.
// Drift between the stub and the real class is caught by
// `voiceagent-compiletest.sh` (which runs the real MainWindow against
// the real engine).
QtObject {
    id: stub

    // SessionSetupPane reads
    property var sttOptions: ["tiny.en", "base.en"]
    property var ttsOptions: ["en_US-amy-low"]
    property string selectedSttModel: "tiny.en"
    property string selectedTtsModel: "en_US-amy-low"
    property var llmUrls: ["http://localhost:1234"]
    property string currentLlmUrl: "http://localhost:1234"
    property string llmConnectionButtonText: "Connect"
    property bool llmServerConnected: false
    property bool llmModelBusy: false
    property bool llmConnectionBusy: false
    property var llmModelOptions: []
    property string selectedLlmModel: ""
    property bool modelLoading: false
    property bool modelProgressIndeterminate: false
    property real modelProgressValue: 0
    property string modelProgressText: ""
    property bool ttsLoading: false
    property bool ttsProgressIndeterminate: false
    property real ttsProgressValue: 0
    property string ttsProgressText: ""

    // MicButton(Frame) reads
    property bool talkReady: true
    property bool voiceConnectionEnabled: false
    property string micStatusLabel: "Idle"

    // MainWindow reads
    property int sttInstalledCount: 1
    property int ttsInstalledCount: 1
    property string themeMode: "auto"
    property bool logVerboseMode: false
    property int contextTokensUsed: 0
    property int contextTokensCeiling: 0
    // ConversationPane reads — drives the per-bubble ▶/🤫 toggle.
    property int speakingRow: -1

    // Catalog models — empty placeholders since CatalogList isn't on
    // the rendered surface for these tests.
    property var sttCatalogModel: null
    property var ttsCatalogModel: null

    // Slot-shaped no-ops covering both SessionSetupPane click handlers
    // and MainWindow header actions.
    function selectSttModel(name) {}
    function selectTtsModel(name) {}
    function setCurrentLlmUrl(url) {}
    function persistCurrentLlmUrl() {}
    function toggleLlmServerConnection(url) {}
    function selectLlmModel(name) {}
    function installSttModel(name) {}
    function deleteSttModel(name) {}
    function installTtsModel(name) {}
    function deleteTtsModel(name) {}
    function setThemeMode(mode) {}
    function setLogVerboseMode(value) {}
    function setVoiceConnectionEnabled(value) {}
    function setThinkingExpanded(index, value) {}
    function replayMessage(index) {}
    function stopSpeaking() {}
}
