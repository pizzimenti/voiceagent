import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.kde.kirigami 2.20 as Kirigami

// Reusable catalog list view used by the Model Manager for both STT models
// and TTS voices. Each row exposes name + status + a "Use" button (when
// installed) and an Install/Remove action button driven by the catalog
// item's own `installed` / `managed` / `downloadable` flags.
//
// `voiceAgent` is intentionally NOT injected here — the parent passes the
// concrete model + filter + selected-name string + action callbacks, so this
// component is decoupled from the backend controller and reusable across the
// two existing call sites and any future catalog views.
ListView {
    id: catalogList

    // The QAbstractListModel powering the rows. Each row exposes
    // `name`, `installed`, `managed`, `downloadable`, `loading`,
    // `progress` per CatalogModel.
    required property var catalogModel

    // Live filter string. When non-empty, rows whose `name` does not
    // contain the filter (case-insensitive) are hidden.
    required property string filterText

    // Name of the currently-selected model. Drives the bold styling
    // and "Use" / "Current" toggle on the per-row select button.
    required property string selectedName

    // Per-row action callbacks. `name` is the row's `model.name`.
    required property var onSelect
    required property var onInstall
    required property var onRemove

    Layout.fillWidth: true
    Layout.fillHeight: true
    clip: true
    spacing: 0
    model: catalogList.catalogModel
    boundsBehavior: Flickable.StopAtBounds
    flickDeceleration: 1800
    maximumFlickVelocity: 24000
    ScrollBar.vertical: ScrollBar {}

    function tr(text) {
        if (typeof i18nCtx !== "undefined" && i18nCtx) {
            return i18nCtx.i18n(text);
        }
        return text;
    }

    function _matches(name, filter) {
        if (!filter) {
            return true;
        }
        return name.toLowerCase().indexOf(filter.toLowerCase()) !== -1;
    }

    function _statusSummary(item) {
        if (item.installed && !item.managed) {
            return catalogList.tr("Custom path");
        }
        return item.installed ? catalogList.tr("Installed") : catalogList.tr("Available to download");
    }

    function _actionLabel(item) {
        return item.installed ? catalogList.tr("Remove") : catalogList.tr("Install");
    }

    // Install / Remove acts on managed catalog entries. Two row shapes
    // hide the action button:
    //   * `installed && !managed` — custom path (e.g., WHISPER_MODEL=/dir).
    //     Voice Agent does not own that file's lifecycle.
    //   * `!installed && !downloadable` — a configured name we can't
    //     resolve to a download source; clicking Install would fail.
    function _actionVisible(item) {
        return item.installed ? item.managed : item.downloadable;
    }

    function _scrollList(wheel) {
        const pdy = wheel.pixelDelta ? wheel.pixelDelta.y : 0;
        const ady = wheel.angleDelta ? wheel.angleDelta.y : 0;
        const delta = pdy !== 0
            ? pdy * 8
            : (ady / 120) * Kirigami.Units.gridUnit * 12;
        const minY = catalogList.originY || 0;
        const maxY = minY + Math.max(0, catalogList.contentHeight - catalogList.height);
        catalogList.contentY = Math.max(minY, Math.min(maxY, catalogList.contentY - delta));
        wheel.accepted = true;
    }

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.NoButton
        propagateComposedEvents: true
        z: 2
        onWheel: function(wheel) {
            catalogList._scrollList(wheel);
        }
    }

    delegate: Item {
        id: catalogDelegate
        width: ListView.view ? ListView.view.width : 0
        visible: catalogList._matches(model.name, catalogList.filterText)
        height: visible ? catalogRow.implicitHeight + Kirigami.Units.mediumSpacing * 2 : 0
        readonly property bool downloading: model.loading
        readonly property real downloadProgress: model.progress

        RowLayout {
            id: catalogRow
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.margins: Kirigami.Units.mediumSpacing
            spacing: Kirigami.Units.mediumSpacing

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2

                Label {
                    Layout.fillWidth: true
                    text: model.name
                    font.weight: catalogList.selectedName === model.name ? Font.DemiBold : Font.Normal
                    wrapMode: Text.WordWrap
                }

                Label {
                    Layout.fillWidth: true
                    text: catalogList._statusSummary(model)
                    color: Kirigami.Theme.disabledTextColor
                    wrapMode: Text.WordWrap
                }
            }

            RowLayout {
                spacing: Kirigami.Units.smallSpacing

                ToolButton {
                    visible: model.installed
                    text: catalogList.selectedName === model.name ? catalogList.tr("Current") : catalogList.tr("Use")
                    enabled: catalogList.selectedName !== model.name
                    onClicked: catalogList.onSelect(model.name)
                }

                ToolButton {
                    visible: catalogList._actionVisible(model)
                    text: catalogDelegate.downloading ? catalogList.tr("Installing…") : catalogList._actionLabel(model)
                    enabled: !catalogDelegate.downloading
                    onClicked: {
                        if (model.installed) {
                            catalogList.onRemove(model.name);
                        } else {
                            catalogList.onInstall(model.name);
                        }
                    }
                }
            }
        }

        Rectangle {
            id: progressTrack
            visible: catalogDelegate.downloading
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: 2
            color: Kirigami.Theme.alternateBackgroundColor
            Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: parent.width * catalogDelegate.downloadProgress
                color: Kirigami.Theme.highlightColor
            }
        }
    }
}
