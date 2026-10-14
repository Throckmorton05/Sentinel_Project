"""Optional real-vendor test, confined to temporary files and high loopback ports.

Set SENTINEL_TEST_ADGUARD_BINARY to the checksum-verified Linux amd64 binary.
Requires PyYAML; does not need root, a DNS network connection, or nft privileges.
"""
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from provision_install import adguard_document


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@unittest.skipUnless(os.environ.get('SENTINEL_TEST_ADGUARD_BINARY'), 'no verified AdGuard binary supplied')
class AdguardRuntimeTests(unittest.TestCase):
    def test_config_authenticated_api_and_local_filter(self):
        import yaml
        binary = Path(os.environ['SENTINEL_TEST_ADGUARD_BINARY']).resolve()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Disposable public test credential. Production hashes are generated per install.
            document = adguard_document('sentinel-test', '$2b$12$ioq7Oe5kqqNrzHfWxvBr0.d.DChn1ImcRmKBL2nfvrPEK7NCQSquS')
            self.assertEqual(document['filtering']['safe_fs_patterns'], ['/opt/AdGuardHome/userfilters/sentinel.txt'])
            document['filtering']['safe_fs_patterns'] = [str(root / 'sentinel.txt')]
            http_port, dns_port = port(), port()
            document['http']['address'] = f'127.0.0.1:{http_port}'
            document['dns'].update(bind_hosts=['127.0.0.1'], port=dns_port,
                                   upstream_dns=['127.0.0.1:9'], bootstrap_dns=[])
            config = root / 'AdGuardHome.yaml'
            config.write_text(yaml.safe_dump(document, sort_keys=False))
            check = subprocess.run([str(binary), '--check-config', '-c', str(config), '-w', str(root)], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)
            with (root / 'runtime.log').open('wb') as log:
                process = subprocess.Popen([str(binary), '-c', str(config), '-w', str(root), '--no-check-update'], stdout=log, stderr=log)
                try:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    auth = 'Basic ' + base64.b64encode(b'sentinel-test:iso-test-password').decode()
                    base = f'http://127.0.0.1:{http_port}/control/'
                    def request(endpoint, data=None, authorization=auth):
                        req = urllib.request.Request(base + endpoint, data=json.dumps(data).encode() if data is not None else None,
                            headers={'Authorization': authorization, 'Content-Type': 'application/json'})
                        with opener.open(req, timeout=2) as response:
                            payload = response.read()
                            return json.loads(payload) if response.headers.get_content_type() == 'application/json' else payload
                    deadline = time.monotonic() + 15
                    while True:
                        try:
                            status = request('status')
                            break
                        except (OSError, urllib.error.URLError):
                            if process.poll() is not None or time.monotonic() >= deadline:
                                self.fail('AdGuard did not start: ' + (root / 'runtime.log').read_text())
                            time.sleep(0.1)
                    self.assertTrue(status['running'])
                    self.assertTrue(status['protection_enabled'])
                    with self.assertRaises(urllib.error.HTTPError) as denied:
                        request('status', authorization='Basic ' + base64.b64encode(b'sentinel-test:wrong').decode())
                    self.assertIn(denied.exception.code, (401, 403))
                    denied.exception.close()
                    rules = root / 'sentinel.txt'
                    rules.write_text('||sentinel-blocked.example.invalid^\n')
                    try:
                        request('filtering/add_url', {'name': 'Sentinel Test', 'url': str(rules), 'whitelist': False})
                    except urllib.error.HTTPError as exc:
                        message = exc.read().decode()
                        exc.close()
                        self.fail('Local filter registration failed: ' + message)
                    loaded = request('filtering/status')['filters']
                    self.assertTrue(any(item['url'] == str(rules) and item['enabled'] and item['rules_count'] == 1 for item in loaded))
                    answer = request('filtering/check_host?name=sentinel-blocked.example.invalid')
                    self.assertEqual(answer['reason'], 'FilteredBlackList')
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == '__main__':
    unittest.main()
