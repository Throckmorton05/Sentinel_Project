let commandListenerStarted = false;
let commandPollTimer = null;
let commandRequestInFlight = false;
let commandRequestStartedAt = 0;
let snapshotTimer = null;


const SNAPSHOT_INTERVAL_MS = 1000;
const COMMAND_POLL_INTERVAL_MS = 100;
const COMMAND_REQUEST_TIMEOUT_MS = 1000;

const quarantinedWindows = {};


function isOnCurrentDesktop(w) {
    if (w.onAllDesktops) {
        return true;
    }

    const currentDesktop = workspace.currentDesktop;

    if (!currentDesktop) {
        return true;
    }

    const desktops = w.desktops;

    for (let i = 0; i < desktops.length; i++) {
        if (
            String(desktops[i].id) ===
            String(currentDesktop.id)
        ) {
            return true;
        }
    }

    return false;
}


function findWindowById(windowId) {
    const windows = workspace.stackingOrder;

    for (let i = 0; i < windows.length; i++) {
        const w = windows[i];

        if (String(w.internalId) === String(windowId)) {
            return w;
        }
    }

    return null;
}


function reportCommandResult(
    commandId,
    success,
    message
) {
    print(
        "Sentinel: reporting command result:",
        commandId,
        success,
        message
    );

    callDBus(
        "org.sentinel.KWinBridge",
        "/org/sentinel/KWinBridge",
        "org.sentinel.KWinBridge",
        "ReportCommandResult",
        String(commandId),
        Boolean(success),
        String(message),
        function() {
            print(
                "Sentinel: command result delivered:",
                commandId
            );
        }
    );
}


function minimizeWindow(windowId) {
    const w = findWindowById(windowId);

    if (!w) {
        print(
            "Sentinel: quarantine failed, window not found:",
            windowId
        );

        return {
            success: false,
            message: "window not found"
        };
    }

    if (!w.minimizable) {
        print(
            "Sentinel: quarantine failed, window is not minimizable:",
            windowId
        );

        return {
            success: false,
            message: "window is not minimizable"
        };
    }

    const id = String(windowId);

    // Save the application's original KWin visibility settings
    // only the first time Sentinel quarantines this window.
    if (!quarantinedWindows[id]) {
        quarantinedWindows[id] = {
            skipTaskbar: Boolean(w.skipTaskbar),
            skipSwitcher: Boolean(w.skipSwitcher),
            skipPager: Boolean(w.skipPager),

            // Normally the blocked window must remain hidden.
            allowVisible: false
        };
    }

    quarantinedWindows[id].allowVisible = false;

    // Remove every normal user route back to the window.
    w.skipTaskbar = true;
    w.skipSwitcher = true;
    w.skipPager = true;

    // Stop KWin from painting the window.
    w.minimized = true;

    print(
        "Sentinel: quarantined window:",
        windowId
    );

    sendAllWindows();

    return {
        success: true,
        message: "window quarantined"
    };
}


function restoreWindow(windowId) {
    const w = findWindowById(windowId);

    if (!w) {
        print(
            "Sentinel: restore failed, window not found:",
            windowId
        );

        return {
            success: false,
            message: "window not found"
        };
    }

    const id = String(windowId);

    if (quarantinedWindows[id]) {
        // Sentinel is intentionally making the quarantined
        // window visible underneath the privacy overlays.
        quarantinedWindows[id].allowVisible = true;

        // IMPORTANT:
        // Keep it hidden from the taskbar/switcher/pager.
        w.skipTaskbar = true;
        w.skipSwitcher = true;
        w.skipPager = true;
    }

    w.minimized = false;

    print(
        "Sentinel: restored quarantined window for recheck:",
        windowId
    );

    sendAllWindows();

    return {
        success: true,
        message: "window restored for recheck"
    };
}


function releaseWindow(windowId) {
    const w = findWindowById(windowId);

    if (!w) {
        delete quarantinedWindows[String(windowId)];

        return {
            success: false,
            message: "window not found"
        };
    }

    const id = String(windowId);
    const state = quarantinedWindows[id];

    if (!state) {
        return {
            success: true,
            message: "window was not quarantined"
        };
    }

    // Restore exactly what the application had before Sentinel
    // quarantined it.
    w.skipTaskbar = state.skipTaskbar;
    w.skipSwitcher = state.skipSwitcher;
    w.skipPager = state.skipPager;

    w.minimized = false;

    delete quarantinedWindows[id];

    print(
        "Sentinel: released quarantined window:",
        windowId
    );

    sendAllWindows();

    return {
        success: true,
        message: "window released"
    };
}




function closeWindow(windowId) {
    const w = findWindowById(windowId);

    if (!w) {
        print(
            "Sentinel: close failed, window not found:",
            windowId
        );

        return {
            success: false,
            message: "window not found"
        };
    }

    if (!w.closeable) {
        print(
            "Sentinel: close failed, window is not closeable:",
            windowId
        );

        return {
            success: false,
            message: "window is not closeable"
        };
    }

    print(
        "Sentinel: closing window:",
        windowId
    );

    w.closeWindow();

    return {
        success: true,
        message: "close requested"
    };
}


function checkForCommand() {
    const now = Date.now();

    if (commandRequestInFlight) {
        const requestAge =
            now - commandRequestStartedAt;

        if (
            requestAge <
            COMMAND_REQUEST_TIMEOUT_MS
        ) {
            return;
        }

        print(
            "Sentinel: clearing stale command request after",
            requestAge,
            "ms"
        );

        commandRequestInFlight = false;
    }

    commandRequestInFlight = true;
    commandRequestStartedAt = now;

    callDBus(
        "org.sentinel.KWinBridge",
        "/org/sentinel/KWinBridge",
        "org.sentinel.KWinBridge",
        "GetNextCommand",
        function(payload) {
            commandRequestInFlight = false;
            commandRequestStartedAt = 0;


            if (!payload) {
                return;
            }

            let command;

            try {
                command = JSON.parse(payload);
            } catch (error) {
                print(
                    "Sentinel: invalid command payload:",
                    payload
                );

                return;
            }

            const commandId = String(
                command.command_id || ""
            );

            const action = String(
                command.action || ""
            );

            const windowId = String(
                command.window_id || ""
            );

            if (!commandId) {
                print(
                    "Sentinel: command missing command_id:",
                    payload
                );

                return;
            }

            print(
                "Sentinel: received command:",
                commandId,
                action,
                windowId
            );

            let result;

            try {
     
                if (action === "minimize") {
                    result = minimizeWindow(windowId);

                } else if (action === "restore") {
                    result = restoreWindow(windowId);

                } else if (action === "release") {
                    result = releaseWindow(windowId);

                } else if (action === "close") {
                    result = closeWindow(windowId);


                } else {
                    print(
                        "Sentinel: unknown command:",
                        action
                    );

                    result = {
                        success: false,
                        message: "unknown command: " + action
                    };
                }

            } catch (error) {
                print(
                    "Sentinel: command execution exception:",
                    commandId,
                    error
                );

                result = {
                    success: false,
                    message: "exception: " + String(error)
                };
            }

            print(
                "Sentinel: command executed:",
                commandId,
                result.success,
                result.message
            );

            reportCommandResult(
                commandId,
                result.success,
                result.message
            );
        }
    );
}


function startSnapshotHeartbeat() {
    if (snapshotTimer !== null) {
        return;
    }

    snapshotTimer = new QTimer();

    snapshotTimer.interval =
        SNAPSHOT_INTERVAL_MS;

    snapshotTimer.singleShot = false;

    snapshotTimer.timeout.connect(
        sendAllWindows
    );

    snapshotTimer.start();

    print(
        "Sentinel: window snapshot heartbeat started:",
        SNAPSHOT_INTERVAL_MS,
        "ms"
    );
}




function startCommandPolling() {
    if (commandPollTimer !== null) {
        return;
    }

    commandPollTimer = new QTimer();

    commandPollTimer.interval =
        COMMAND_POLL_INTERVAL_MS;

    commandPollTimer.singleShot = false;

    commandPollTimer.timeout.connect(
        checkForCommand
    );

    commandPollTimer.start();

    print(
        "Sentinel: command polling started:",
        COMMAND_POLL_INTERVAL_MS,
        "ms"
    );
}





function sendAllWindows() {
    const windows = workspace.stackingOrder;
    const data = [];

    for (let i = 0; i < windows.length; i++) {
        const w = windows[i];

        if (
            w.specialWindow ||
            w.deleted ||
            !w.managed ||
            w.popupWindow ||
            w.tooltip ||
            w.dropdownMenu ||
            w.popupMenu ||
            w.notification ||
            w.criticalNotification ||
            w.onScreenDisplay ||
            w.comboBox ||
            w.appletPopup
        ) {
            continue;
        }

        data.push({
            window_id: String(w.internalId),

            title: String(w.caption),
            app_id: String(w.resourceClass),
            resource_name: String(w.resourceName),

            pid: Number(w.pid),

            x: Math.round(w.x),
            y: Math.round(w.y),
            width: Math.round(w.width),
            height: Math.round(w.height),

            focused: Boolean(w.active),
            minimized: Boolean(w.minimized),

            on_current_desktop: isOnCurrentDesktop(w),

            normal_window: Boolean(w.normalWindow),
            dialog: Boolean(w.dialog)
        });
    }

    const payload = JSON.stringify({
        schema_version: 1,
        windows: data
    });

    print(
        "Sentinel Window Bridge sending",
        data.length,
        "windows"
    );

    callDBus(
            "org.sentinel.KWinBridge",
            "/org/sentinel/KWinBridge",
            "org.sentinel.KWinBridge",
            "UpdateWindows",
            payload,

            function() {
                print("Sentinel window list sent");

                if (!commandListenerStarted) {
                    commandListenerStarted = true;

                    print(
                        "Sentinel: bridge online; starting command polling"
                    );

                    startCommandPolling();
                } else {
                    checkForCommand();
                }
            }
        );   


}


workspace.windowAdded.connect(function(window) {
    print("Sentinel: window added");
    sendAllWindows();
});


workspace.windowRemoved.connect(function(window) {
    const id = String(window.internalId);

    if (quarantinedWindows[id]) {
        print(
            "Sentinel: removing quarantine state for closed window:",
            id
        );

        delete quarantinedWindows[id];
    }

    print("Sentinel: window removed");
    sendAllWindows();
});


workspace.windowActivated.connect(function(window) {
    print("Sentinel: window activated");
    sendAllWindows();
});

workspace.currentDesktopChanged.connect(function() {
    print("Sentinel: desktop changed");
    sendAllWindows();
});


print("Sentinel Window Bridge started");


startSnapshotHeartbeat();
sendAllWindows();
