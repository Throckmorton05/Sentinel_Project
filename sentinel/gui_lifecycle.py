"""Finish Python-backed Qt workers before destroying their widgets or app."""
from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, QThread, QThreadPool, QTimer


def finish_gui(app, window):
    """Called after app.exec(), while both Qt objects are still alive.

    A worker's finished signal is not a thread join. Background pollers must
    also finish before Python releases the window and QApplication wrappers.
    No worker is terminated and no backend operation is cancelled here.
    """
    if not sip.isdeleted(window):
        for timer in window.findChildren(QTimer):
            timer.stop()
        threads = window.findChildren(QThread)
    else:
        threads = []
    # waitForDone/wait release the GIL, allowing Python callbacks to return.
    # Do not pump normal events here: that could start another periodic read.
    QThreadPool.globalInstance().waitForDone()
    for thread in threads:
        if not sip.isdeleted(thread):
            thread.wait()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    if not sip.isdeleted(window):
        sip.delete(window)
    if not sip.isdeleted(app):
        sip.delete(app)
