"""Exercise the session worker over a private live D-Bus, without a monitor."""
import asyncio
import contextlib
import io
import json
import shutil
import subprocess
from pathlib import Path
import pytest
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method
from sentinel.activity import session_worker


def test_worker_loads_own_script_gets_focus_and_pauses_on_lock(monkeypatch):
    if not shutil.which('dbus-daemon'):
        pytest.skip('dbus-daemon is unavailable')
    daemon = subprocess.Popen(['dbus-daemon','--session','--nofork','--print-address=1'],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    address = daemon.stdout.readline().strip()
    if not address:
        daemon.wait(timeout=3)
        daemon.stdout.close()
        pytest.skip('Private D-Bus sockets are unavailable in this environment')
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', address)
    output = io.StringIO()
    calls=[]
    locked=[False]
    original_sleep=asyncio.sleep
    ready=asyncio.Event()

    async def run():
        bus=await MessageBus(bus_address=address).connect()
        await bus.request_name('org.kde.KWin')
        await bus.request_name('org.freedesktop.ScreenSaver')

        class Scripting(ServiceInterface):
            def __init__(self):
                super().__init__('org.kde.kwin.Scripting')

            @method()
            def unloadScript(self, name:'s')->'b':
                calls.append(('unload',name))
                return True

            @method()
            def loadScript(self, path:'s', name:'s')->'i':
                assert Path(path).name=='focus.js' and Path(path).is_file()
                calls.append(('load',name))
                return 7

        class Script(ServiceInterface):
            def __init__(self):
                super().__init__('org.kde.kwin.Script')

            @method()
            async def run(self):
                await session_worker.call(bus, session_worker.NAME, '/org/sentinel/ScreenTime',
                    session_worker.NAME,'UpdateFocus','s',['kate'])
                calls.append(('run',7))

        class Saver(ServiceInterface):
            def __init__(self):
                super().__init__('org.freedesktop.ScreenSaver')

            @method()
            def GetActive(self)->'b':
                return locked[0]

        bus.export('/Scripting',Scripting())
        bus.export('/Scripting/Script7',Script())
        bus.export('/ScreenSaver',Saver())
        samples=[]
        async def accelerated_sleep(seconds):
            if seconds == 5:
                samples.append(json.loads(output.getvalue().splitlines()[-1]))
                if len(samples)==1:
                    locked[0]=True
                else:
                    ready.set()
            await original_sleep(.01)
        monkeypatch.setattr(session_worker.asyncio,'sleep',accelerated_sleep)
        task=asyncio.create_task(session_worker.main())
        try:
            await asyncio.wait_for(ready.wait(),3)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            bus.disconnect()
        assert samples[0]=={'app':'kate','active':True}
        assert samples[1]=={'app':'kate','active':False}
        assert calls == [('unload','sentinel-screen-time'),('load','sentinel-screen-time'),
                         ('run',7),('unload','sentinel-screen-time')]
    try:
        with contextlib.redirect_stdout(output):
            asyncio.run(run())
    finally:
        daemon.terminate()
        daemon.wait(timeout=3)
        daemon.stdout.close()
