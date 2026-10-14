"""Unprivileged KWin focus reader. Private stdout is consumed by sentineld."""
import asyncio
import json
import signal
import time
from pathlib import Path
from dbus_next import Message, MessageType, NameFlag, RequestNameReply
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method

NAME = 'org.sentinel.ScreenTime'
PLUGIN = 'sentinel-screen-time'


class Focus(ServiceInterface):
    def __init__(self):
        super().__init__(NAME)
        self.app = ''
        self.updated = 0

    @method()
    def UpdateFocus(self, app: 's'):
        self.app = app[:256]
        self.updated = time.monotonic()


async def call(bus, destination, path, interface, member, signature='', body=None):
    reply = await asyncio.wait_for(bus.call(Message(destination=destination, path=path,
        interface=interface, member=member, signature=signature, body=body or [])), 3)
    if reply.message_type == MessageType.ERROR:
        raise RuntimeError('Desktop service unavailable.')
    return reply.body


async def main():
    task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    bus = await MessageBus().connect()
    if await bus.request_name(NAME, NameFlag.DO_NOT_QUEUE) != RequestNameReply.PRIMARY_OWNER:
        raise RuntimeError('Screen-time collector is already running.')
    focus = Focus()
    bus.export('/org/sentinel/ScreenTime', focus)
    try:
        await call(bus, 'org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting', 'unloadScript', 's', [PLUGIN])
        script = (await call(bus, 'org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting',
            'loadScript', 'ss', [str(Path(__file__).with_name('focus.js')), PLUGIN]))[0]
        if script < 0:
            raise RuntimeError('Could not load screen-time script.')
        await call(bus, 'org.kde.KWin', f'/Scripting/Script{script}', 'org.kde.kwin.Script', 'run')
        while True:
            # KDE's screen-lock status is Wayland compatible. IdleHint is read
            # from logind by the parent; GetSessionIdleTime is X11-only.
            locked = (await call(bus, 'org.freedesktop.ScreenSaver', '/ScreenSaver',
                'org.freedesktop.ScreenSaver', 'GetActive'))[0]
            print(json.dumps({'app': focus.app, 'active': not locked and time.monotonic()-focus.updated < 3}), flush=True)
            await asyncio.sleep(5)
    finally:
        try:
            await call(bus, 'org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting', 'unloadScript', 's', [PLUGIN])
        finally:
            bus.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
