import QtQuick
import QtQuick.Controls
import org.kde.kirigami as Kirigami

Item {
    id: root

    property var samples: []
    property real level: 0.0
    property color traceColor: "#58a6ff"
    property string label: ""

    implicitHeight: 92

    Rectangle {
        anchors.fill: parent
        radius: 18
        color: Qt.rgba(0.06, 0.09, 0.12, 0.82)
        border.width: 1
        border.color: Qt.rgba(1, 1, 1, 0.08)
    }

    Rectangle {
        anchors.left: parent.left
        anchors.bottom: parent.bottom
        anchors.margins: 1
        width: parent.width - 2
        height: (parent.height - 2) * Math.max(0.08, Math.min(1.0, root.level))
        radius: 17
        color: Qt.rgba(root.traceColor.r, root.traceColor.g, root.traceColor.b, 0.12)
    }

    Canvas {
        id: canvas
        anchors.fill: parent
        anchors.margins: 10
        antialiasing: true

        onPaint: {
            const ctx = getContext("2d");
            ctx.reset();

            const centerY = height / 2;
            ctx.strokeStyle = Qt.rgba(1, 1, 1, 0.08);
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(0, centerY);
            ctx.lineTo(width, centerY);
            ctx.stroke();

            if (!root.samples || root.samples.length === 0) {
                return;
            }

            ctx.strokeStyle = root.traceColor;
            ctx.lineWidth = 2.2;
            ctx.lineJoin = "round";
            ctx.lineCap = "round";
            ctx.beginPath();

            for (let i = 0; i < root.samples.length; i += 1) {
                const x = (i / Math.max(1, root.samples.length - 1)) * width;
                const y = centerY - (root.samples[i] * (height * 0.38));
                if (i === 0) {
                    ctx.moveTo(x, y);
                } else {
                    ctx.lineTo(x, y);
                }
            }
            ctx.stroke();
        }

        Connections {
            target: root
            function onSamplesChanged() { canvas.requestPaint(); }
            function onLevelChanged() { canvas.requestPaint(); }
            function onTraceColorChanged() { canvas.requestPaint(); }
            function onWidthChanged() { canvas.requestPaint(); }
            function onHeightChanged() { canvas.requestPaint(); }
        }
    }

    Label {
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.margins: 12
        text: root.label
        color: Kirigami.Theme.disabledTextColor
        font.pixelSize: 12
        font.weight: Font.DemiBold
    }
}
