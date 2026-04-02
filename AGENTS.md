# AGENTS.md

## Review guidelines

- Treat startup hangs, blank windows, or network/model refresh work that blocks first paint as P1.
- Treat regressions in the three intended layouts as P1: compact under 250px, stacked medium mode up to about half-screen width, and large horizontal tiling above that.
- Treat clipping of the session controls, conversation pane, or microphone control at window edges as P1.
- Treat conversation-turn lifecycle regressions as P1. A user turn should keep one bubble from draft transcription through sent/final text, and an assistant turn should keep one bubble from `Thinking...` through the final response.
- Treat message ordering regressions as P1. The finalized user bubble must remain above its corresponding assistant bubble.
- For QML/UI changes, verify `./voiceagent-compiletest` passes. For startup-flow changes, verify the app window appears without freezing on launch.
