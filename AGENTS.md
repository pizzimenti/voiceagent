# AGENTS.md

## Review guidelines

- Treat startup hangs, blank windows, or network/model refresh work that blocks first paint as P1.
- Treat regressions in the three intended layouts as P1: compact under 250px, stacked medium mode up to about half-screen width, and large horizontal tiling above that.
- Treat clipping of the session controls, conversation pane, or microphone control at window edges as P1.
- Treat conversation-turn lifecycle regressions as P1. A user turn should keep one bubble from draft transcription through sent/final text. Assistant thinking/progress belongs in status text and the microphone control; only final assistant responses should appear as assistant bubbles.
- Treat message ordering regressions as P1. The finalized user bubble must remain above its corresponding assistant bubble.
- For QML/UI changes, verify `./voiceagent-compiletest.sh` passes. For startup-flow changes, verify the app window appears without freezing on launch.

## KDE/QML implementation memory

- Prefer stable `QAbstractListModel` objects for live QML lists. Avoid replacing `QVariantList` values for frequently changing views because delegate rebuilds can reset `contentY`, disturb current index, and cause visible jumps.
- For sticky conversation scrolling, keep one owner for scroll state. Track whether the user is attached to the tail, compute bounds with `originY`, and snap with explicit `contentY` only when sticky mode is active.
- Do not route wheel scrolling through `Flickable.flick()` unless the sticky state machine is designed around Flickable movement events. Native flick movement can emit `movementStarted` / `movementEnded` and accidentally detach bottom-stick behavior.
- Keep status/progress separate from conversation bubbles. Operational states such as `Thinking`, transcription, model loading, and playback belong in status rows or the microphone control; final user and assistant content belongs in bubbles.
- Treat `voiceagent-buildtest.sh` as a long-running app launcher. If another Voice Agent instance or lock exists, assume it may be intentional; do not kill the process or remove the runtime lock without explicit user approval.
- Keep network/model refreshes off the first paint path. Launch the QML window first, then defer autoconnect, model refresh, and heavy backend work.
