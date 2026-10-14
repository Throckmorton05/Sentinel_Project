"""The guide works offline and cannot execute commands supplied by a launcher."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication
from sentinel.daemon import client
from sentinel.guardian_apps import desktop, launch
from sentinel.portal.guide import GuideWindow, SECTIONS
from sentinel.portal import main as portal_main


def test_offline_guide_navigation_never_contacts_backend():
    app = QApplication.instance() or QApplication([])
    with patch.object(client, 'request', side_effect=AssertionError('guide must stay offline')):
        window = GuideWindow()
        window.show()
        for row in range(len(SECTIONS)):
            window.navigation.setCurrentRow(row)
            app.processEvents()
            assert window.stack.currentIndex() == row
        window.close()
    assert 'Self Lock is permanent' in str(SECTIONS)


def test_guardian_guide_launcher_uses_fixed_read_only_route():
    entry = {'desktop_id': 'sentinel-getting-started.desktop',
             'source': '[Desktop Entry]\nType=Application\nName=Guide\nExec=/tmp/untrusted --args\n'}
    rendered = desktop.render_launcher(entry, True)
    assert desktop.GUIDE_EXEC in rendered
    assert '/tmp/untrusted' not in rendered
    assert entry['desktop_id'] in desktop.BUILTIN_IDS
    with patch.object(launch.os, 'geteuid', return_value=1000), \
         patch.object(launch.sys, 'argv', ['launcher', entry['desktop_id'], '/tmp/untrusted']), \
         patch.object(launch, 'request', side_effect=AssertionError('offline guide must not need RPC')), \
         patch.object(launch.subprocess, 'Popen') as popen:
        launch.main()
    popen.assert_called_once_with(
        ['/usr/lib/sentinel/venv/bin/python', '-I', '-m', 'sentinel.portal.main', '--guide'],
        start_new_session=True)


def test_guide_entrypoint_does_not_construct_the_portal():
    with patch.object(portal_main.os, 'geteuid', return_value=1000), \
         patch.object(portal_main, 'QApplication') as application, \
         patch.object(portal_main, 'PortalWindow', side_effect=AssertionError('no Portal RPC workers')), \
         patch('sentinel.portal.guide.GuideWindow') as guide, \
         patch.object(portal_main, 'finish_gui') as finish:
        application.return_value.exec.return_value = 0
        assert portal_main.main(['sentinel-portal', '--guide', '--system']) == 0
        guide.assert_called_once_with()
        finish.assert_called_once_with(application.return_value, guide.return_value)
