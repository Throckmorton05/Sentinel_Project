import QtQuick
import QtQuick.Window
import org.kde.layershell 1.0 as LayerShell

Item {
    id: root

    //
    // One countdown shared by every display.
    //
    property int remainingSeconds: tamperDurationSeconds

    //
    // Create one independent overlay window per physical display.
    //
    Instantiator {
        id: blockerWindows

        model: Qt.application.screens.length

        delegate: Window {
            id: blocker

            required property int index

            screen: Qt.application.screens[index]

            visible: true

            color: "#000000"

            flags: Qt.FramelessWindowHint

            title: "Sentinel Anti-Tamper Block"

            LayerShell.Window.scope:
                "sentinel-anti-tamper"

            LayerShell.Window.layer:
                LayerShell.Window.LayerOverlay

            LayerShell.Window.anchors:
                LayerShell.Window.AnchorTop
                | LayerShell.Window.AnchorBottom
                | LayerShell.Window.AnchorLeft
                | LayerShell.Window.AnchorRight

            LayerShell.Window.keyboardInteractivity:
                LayerShell.Window.KeyboardInteractivityExclusive

            LayerShell.Window.exclusionZone: -1

            onClosing: function(close) {
                close.accepted = false
            }

            Rectangle {
                anchors.fill: parent

                color: "#000000"

                focus: true

                Keys.onPressed: function(event) {
                    event.accepted = true
                }

                Column {
                    anchors.centerIn: parent

                    width: Math.min(
                        blocker.width * 0.80,
                        900
                    )

                    spacing: 28

                    Image {
                        anchors.horizontalCenter:
                            parent.horizontalCenter

                        width: 400
                        height: 250

                        source: sentinelLogo

                        fillMode:
                            Image.PreserveAspectFit

                        smooth: true
                    }

                    Text {
                        anchors.horizontalCenter:
                            parent.horizontalCenter

                        width: parent.width

                        horizontalAlignment:
                            Text.AlignHCenter

                        text:
                            "SENTINEL ANTI-TAMPER BLOCK"

                        color: "#c7efff"

                        font.pixelSize: 38
                        font.bold: true
                        font.letterSpacing: 2

                        wrapMode:
                            Text.WordWrap
                    }

                    Text {
                        anchors.horizontalCenter:
                            parent.horizontalCenter

                        width: parent.width

                        horizontalAlignment:
                            Text.AlignHCenter

                        text:
                            "The Sentinel screen monitor was repeatedly terminated.\n\n"
                            + "Access to this device has been temporarily blocked "
                            + "while Sentinel restores protection."

                        color: "#ffffff"

                        font.pixelSize: 19
                        font.weight: Font.Medium

                        wrapMode:
                            Text.WordWrap
                    }

                    Text {
                        anchors.horizontalCenter:
                            parent.horizontalCenter

                        width: parent.width

                        horizontalAlignment:
                            Text.AlignHCenter

                        text: {
                            var value = Math.max(
                                0,
                                root.remainingSeconds
                            )

                            var minutes = Math.floor(
                                value / 60
                            )

                            var seconds =
                                value % 60

                            return (
                                String(minutes).padStart(
                                    2,
                                    "0"
                                )
                                + ":"
                                + String(seconds).padStart(
                                    2,
                                    "0"
                                )
                            )
                        }

                        color: "#c7efff"

                        font.pixelSize: 34
                        font.bold: true
                    }

                    Text {
                        anchors.horizontalCenter:
                            parent.horizontalCenter

                        width: parent.width

                        horizontalAlignment:
                            Text.AlignHCenter

                        text:
                            "Sentinel is restoring protected monitoring services."

                        color: "#8f9aa3"

                        font.pixelSize: 14

                        wrapMode:
                            Text.WordWrap
                    }
                }
            }
        }
    }

    //
    // One timer controls every blocker window.
    //
    Timer {
        interval: 1000

        repeat: true

        running: true

        onTriggered: {
            if (root.remainingSeconds <= 1) {
                root.remainingSeconds = 0

                Qt.quit()

                return
            }

            root.remainingSeconds--
        }
    }
}
