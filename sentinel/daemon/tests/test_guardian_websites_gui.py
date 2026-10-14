"""Selection edits, cancellation, denial, and pending enforcement in the GUI."""
import os
from unittest.mock import patch

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication, QMessageBox
from sentinel.guardian_websites import manager


@pytest.fixture
def window():
    app = QApplication.instance() or QApplication([])
    result = {'enabled': False, 'domains': ['school.example.org'],
              'browser_application': {'status': 'applied', 'error': None},
              'browser_restrictions': {'status': 'active', 'error': None}}
    with patch.object(manager.client, 'get_guardian_website_policy', return_value=result):
        widget = manager.Manager()
        yield widget, result
        widget.close()
    assert app is not None


def test_add_kids_remove_and_save_with_pin(window):
    widget, result = window
    widget.add_domain('youtubekids.com')
    widget.add_domain('YOUTUBEKIDS.COM.')
    assert widget.domains.count() == 2
    widget.domains.item(0).setSelected(True)
    widget.remove_domain()
    assert widget.domains.item(0).text() == 'youtubekids.com'
    widget.enabled.setChecked(True)
    with (patch.object(manager, 'prompt_pin', return_value='1234'),
          patch.object(manager.client, 'replace_guardian_websites', return_value=result) as save,
          patch.object(QMessageBox, 'information')):
        widget.save()
    save.assert_called_once_with(['youtubekids.com'], True, guardian_pin='1234')


def test_cancelled_pin_does_not_change_saved_state(window):
    widget, _ = window
    with (patch.object(manager, 'prompt_pin', return_value=None),
          patch.object(manager.client, 'replace_guardian_websites') as save):
        widget.save()
    save.assert_not_called()


def test_invalid_domain_is_not_added(window):
    widget, _ = window
    with patch.object(QMessageBox, 'warning') as warning:
        widget.add_domain('https://example.org/path')
    assert widget.domains.count() == 1
    warning.assert_called_once()


def test_pin_denial_retains_unsaved_changes(window):
    widget, _ = window
    widget.add_domain('youtubekids.com')
    with (patch.object(manager, 'prompt_pin', return_value='9999'),
          patch.object(manager.client, 'replace_guardian_websites',
                       side_effect=manager.client.SentineldError('Guardian authentication failed.')),
          patch.object(QMessageBox, 'warning') as warning):
        widget.save()
    assert widget.domains.count() == 2
    warning.assert_called_once()


def test_pending_status_is_visible(window):
    widget, result = window
    result['browser_application'] = {'status': 'pending', 'error': 'disk unavailable'}
    widget.refresh()
    assert 'pending' in widget.status.text()
    assert 'disk unavailable' in widget.status.text()


def test_old_website_launcher_opens_portal_instead_of_standalone_manager():
    with patch.object(manager, 'open_portal', return_value=0) as portal, \
         patch.object(manager, 'Manager') as standalone:
        assert manager.main() == 0
    portal.assert_called_once_with(['sentinel-portal', '--guardian'])
    standalone.assert_not_called()
