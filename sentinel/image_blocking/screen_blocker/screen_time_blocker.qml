import QtQuick
import QtQuick.Window
import org.kde.layershell 1.0 as LayerShell

Item {
    id: root
    property double clock: Date.now() / 1000
    Timer {
        interval: 1000; repeat: true; running: true
        onTriggered: root.clock = Date.now() / 1000
    }
    Instantiator {
        model: Qt.application.screens
        delegate: Window {
            id: blocker
            required property var modelData
            screen: modelData
            visible: true
            color: "#1E1E1E"
            flags: Qt.FramelessWindowHint
            title: "Sentinel Screen Time Block"
            LayerShell.Window.scope: "sentinel-screen-time"
            LayerShell.Window.layer: LayerShell.Window.LayerOverlay
            LayerShell.Window.anchors: LayerShell.Window.AnchorTop | LayerShell.Window.AnchorBottom
                | LayerShell.Window.AnchorLeft | LayerShell.Window.AnchorRight
            LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityExclusive
            LayerShell.Window.exclusionZone: -1
            onClosing: function(close) { close.accepted = screenTime.allowedToClose }

            Column {
                id: content
                anchors.centerIn: parent
                width: Math.min(680, blocker.width * 0.85)
                spacing: 20
                Image {
                    width: 190; height: 135
                    anchors.horizontalCenter: parent.horizontalCenter
                    source: sentinelLogo
                    fillMode: Image.PreserveAspectFit
                }
                Text {
                    width: parent.width; horizontalAlignment: Text.AlignHCenter
                    text: "SCREEN TIME BREAK"
                    color: "#FFFFFF"; font.pixelSize: 32; font.bold: true
                }
                Text {
                    width: parent.width; horizontalAlignment: Text.AlignHCenter
                    text: "Take a break from your computer. Sentinel will restore access when your block ends."
                    wrapMode: Text.WordWrap; color: "#D0D0D0"; font.pixelSize: 17
                }
                Flickable {
                    id: scroller
                    width: parent.width
                    height: Math.min(cards.height, Math.max(140, blocker.height - 390))
                    contentWidth: width; contentHeight: cards.height; clip: true
                    Rectangle {
                        parent: scroller
                        width: 4
                        height: scroller.height * scroller.visibleArea.heightRatio
                        x: scroller.width - width
                        y: scroller.height * scroller.visibleArea.yPosition
                        visible: scroller.contentHeight > scroller.height
                        radius: 2; color: "#6B6B6B"
                        z: 1
                    }
                    Column {
                        id: cards
                        width: parent.width; spacing: 18
                        Repeater {
                            model: screenTime.windows
                            delegate: Rectangle {
                                id: card
                                required property var modelData
                                width: cards.width; height: fields.height + 36
                                radius: 12; color: "#292929"
                                Column {
                                    id: fields
                                    width: parent.width - 36
                                    anchors.centerIn: parent; spacing: 12
                                    Text {
                                        width: parent.width; text: card.modelData.name
                                        textFormat: Text.PlainText
                                        wrapMode: Text.WordWrap; color: "#FFFFFF"
                                        font.pixelSize: 21; font.bold: true
                                    }
                                    Text {
                                        width: parent.width
                                        text: {
                                            var seconds = Math.max(0, Math.ceil(card.modelData.end_at - root.clock))
                                            return "Time remaining: " + Math.floor(seconds / 3600) + ":"
                                                + String(Math.floor(seconds / 60) % 60).padStart(2, "0") + ":"
                                                + String(seconds % 60).padStart(2, "0")
                                        }
                                        color: "#D0D0D0"; font.pixelSize: 18
                                    }
                                    Text {
                                        width: parent.width; wrapMode: Text.WordWrap
                                        text: card.modelData.kind === "daily_limit"
                                            ? (card.modelData.mode === "guardian_lock"
                                                ? "Your daily allowance is used up. Enter the Guardian PIN to turn the daily limit off. Enable it again in System & desktop when you want it back on."
                                                : card.modelData.mode === "self_lock"
                                                ? (card.modelData.extension_available
                                                    ? "Your daily allowance is used up. You can take two minutes once to open System & desktop and change this limit."
                                                    : "Your two-minute extension has been used. Your daily allowance resets at midnight.")
                                                : "Your daily allowance is used up. You can turn the daily limit off, then enable it again in System & desktop.")
                                            : card.modelData.mode === "guardian_lock"
                                            ? "Enter the Guardian PIN to bypass this occurrence. Future repeats stay scheduled."
                                            : card.modelData.mode === "self_lock"
                                            ? (card.modelData.extension_available
                                                ? "You can take two minutes once to open the calendar and disable this schedule."
                                                : "Your two-minute extension has been used. Access returns when this block ends.")
                                            : "You can bypass this occurrence or disable the schedule in the calendar."
                                        color: "#D0D0D0"; font.pixelSize: 15
                                    }
                                    Rectangle {
                                        width: parent.width
                                        height: 44
                                        visible: card.modelData.mode === "guardian_lock"
                                        color: "#1E1E1E"; radius: 8; border.color: "#6B6B6B"
                                        TextInput {
                                            id: pin
                                            objectName: "guardianPin-" + card.modelData.id
                                            anchors.fill: parent
                                            leftPadding: 12; rightPadding: 12
                                            verticalAlignment: TextInput.AlignVCenter
                                            font.pixelSize: 16
                                            echoMode: TextInput.Password
                                            selectByMouse: true
                                            activeFocusOnTab: true
                                            Accessible.role: Accessible.EditableText
                                            Accessible.name: "Guardian PIN"
                                            clip: true
                                            color: "#FFFFFF"; selectionColor: "#4A4A4A"
                                            onAccepted: bypass.trigger()
                                            Text {
                                                anchors.left: parent.left; anchors.leftMargin: 12
                                                anchors.verticalCenter: parent.verticalCenter
                                                visible: pin.text.length === 0
                                                text: "Guardian PIN"; color: "#A0A0A0"
                                                font.pixelSize: 16
                                            }
                                        }
                                    }
                                    Rectangle {
                                        id: bypass
                                        width: parent.width; height: 48
                                        radius: 10
                                        activeFocusOnTab: true
                                        Accessible.role: Accessible.Button
                                        Accessible.name: text
                                        Accessible.onPressAction: trigger()
                                        visible: card.modelData.mode !== "self_lock" || card.modelData.extension_available
                                        enabled: !screenTime.busy
                                        property string text: card.modelData.kind === "daily_limit" && card.modelData.mode !== "self_lock"
                                            ? (card.modelData.mode === "guardian_lock" ? "Turn off with Guardian PIN" : "Turn off daily limit")
                                            : card.modelData.mode === "guardian_lock" ? "Bypass with Guardian PIN"
                                            : card.modelData.mode === "self_lock" ? "2 minutes to turn this off"
                                            : "Bypass this occurrence"
                                        color: clickArea.pressed ? "#151515" : clickArea.containsMouse ? "#6B6B6B" : "#4A4A4A"
                                        opacity: enabled ? 1 : 0.6
                                        border.width: activeFocus ? 1 : 0
                                        border.color: "#D0D0D0"
                                        Text {
                                            anchors.fill: parent
                                            text: bypass.text; color: "#FFFFFF"; font.pixelSize: 16
                                            horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                                        }
                                        function trigger() {
                                            if (!enabled) return
                                            screenTime.bypass(card.modelData.id, pin.text)
                                            pin.clear()
                                        }
                                        Keys.onReturnPressed: trigger()
                                        Keys.onSpacePressed: trigger()
                                        MouseArea {
                                            id: clickArea
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: bypass.trigger()
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                Text {
                    width: parent.width; text: screenTime.error
                    textFormat: Text.PlainText
                    visible: text.length > 0; wrapMode: Text.WordWrap
                    horizontalAlignment: Text.AlignHCenter
                    color: "#ffb8b8"; font.pixelSize: 15
                }
            }
        }
    }
}
