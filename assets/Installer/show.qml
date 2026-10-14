import QtQuick 2.0

Rectangle {
    id: presentation
    color: "#172338"
    property bool activatedInCalamares: false
    property int currentSlide: 0
    property var slides: [
        {title: qsTr("A desktop with boundaries you choose"),
         text: qsTr("Welcome to Sentinel Linux. Your first session starts Unlocked, with protection switches off. Open Sentinel Portal to choose the features that fit your life.")},
        {title: qsTr("Protection, one switch at a time"),
         text: qsTr("Choose image filtering, website categories, Safe Steam, Safe Crunchyroll, and installation limits. Test your setup while Unlocked before making a lasting commitment.")},
        {title: qsTr("Focus on what matters"),
         text: qsTr("Plan app, website, or screen-time windows in Calendar. Set a daily allowance and use Activity to understand your habits.")},
        {title: qsTr("Choose your lock mode carefully"),
         text: qsTr("Guardian Lock uses a guardian's PIN for changes. Self Lock is permanent and has no ordinary unlock path. Configure and test your protections first.")},
        {title: qsTr("Start here after installation"),
         text: qsTr("Find Start Here — Sentinel on your desktop for an offline guide. Customize Rofi and your terminal, install apps through Sentinel, and keep your system up to date.")}
    ]

    function onActivate() { currentSlide = 0; activatedInCalamares = true }
    function onLeave() { activatedInCalamares = false }

    Image {
        id: shield
        source: "logo.svg"
        width: Math.min(100, parent.width * 0.18)
        height: width
        fillMode: Image.PreserveAspectFit
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: copy.top
        anchors.bottomMargin: 24
    }
    Column {
        id: copy
        width: parent.width * 0.83
        anchors.centerIn: parent
        anchors.verticalCenterOffset: 36
        spacing: 20
        Text {
            width: parent.width
            text: presentation.slides[presentation.currentSlide].title
            font.pixelSize: 28
            font.bold: true
            color: "#78c8ff"
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
        }
        Text {
            width: parent.width
            text: presentation.slides[presentation.currentSlide].text
            font.pixelSize: 18
            color: "#eef5ff"
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            lineHeight: 1.35
        }
    }
    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 16
        text: qsTr("SENTINEL 0.1.0 BETA") + "   ·   " + (presentation.currentSlide + 1) + " / " + presentation.slides.length
        color: "#a6bbd5"
        font.pixelSize: 12
    }
    Timer {
        interval: 12000
        running: presentation.activatedInCalamares
        repeat: true
        onTriggered: presentation.currentSlide = (presentation.currentSlide + 1) % presentation.slides.length
    }
}
