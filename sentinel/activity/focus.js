// Independent of the image-filtering bridge. Only foreground app metadata.
function sendFocus() {
    const w = workspace.activeWindow;
    const app = w && !w.specialWindow && !w.minimized ? String(w.resourceClass || '') : '';
    callDBus('org.sentinel.ScreenTime', '/org/sentinel/ScreenTime',
             'org.sentinel.ScreenTime', 'UpdateFocus', app, function() {});
}
const timer = new QTimer();
timer.interval = 1000;
timer.timeout.connect(sendFocus);
timer.start();
workspace.windowActivated.connect(sendFocus);
sendFocus();
