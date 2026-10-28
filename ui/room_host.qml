// File: ui/room_host.qml

import QtQuick
import QtWebEngine
import QtWebChannel

WebEngineView {
    id: web
    backgroundColor: "#15171a"
    webChannel: WebChannel { id: channel }

    Component.onCompleted: {
        var script = WebEngine.script()
        script.name = "qwebchannel"
        script.sourceUrl = "qrc:/qtwebchannel/qwebchannel.js"
        script.injectionPoint = WebEngineScript.DocumentCreation
        script.worldId = WebEngineScript.MainWorld
        script.runOnSubframes = false
        web.userScripts.insert(script)
        channel.registerObjects({ "host": host })
    }

    onJavaScriptConsoleMessage: function(level, message, line, source) {
        host.consoleMessage(level, message, line, source)
    }
}
