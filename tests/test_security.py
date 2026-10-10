import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dealer-url-validator/scripts'))
from transport import Transport, requests
from browser_batch import reusable_record
from review_results import browser_record


class Security(unittest.TestCase):
    def test_tls_failure_never_retries_with_verification_disabled(self):
        session = Mock()
        session.get.side_effect = requests.exceptions.SSLError('invalid certificate')
        with patch('transport.requests.Session', return_value=session):
            response = Transport().fetch('https://dealer.example/')
        self.assertEqual(session.get.call_count, 1)
        self.assertNotEqual(session.get.call_args.kwargs.get('verify'), False)
        self.assertEqual(response.status, 0)
        self.assertFalse(response.ssl_bypassed)
        self.assertIn('TLS', response.error)

    def test_insecure_historical_capture_cannot_suppress_secure_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); url='https://dealer.example/new-vehicles/'
            record=dict(requested_url=url, final_url=url, via='browser', status=200, error='',
                        ssl_bypassed=True, captured_at=datetime.now(timezone.utc).isoformat(),
                        text='<h1>New Vehicles</h1><div class="vehicle-card">New Acura Integra</div>')
            (root/(hashlib.sha256(url.encode()).hexdigest()+'.json')).write_text(json.dumps(record))
            self.assertIsNone(reusable_record(root, url))
            self.assertIsNone(browser_record(root, url))
            session = Mock(); session.get.side_effect = requests.exceptions.SSLError('invalid certificate')
            with patch('transport.requests.Session', return_value=session):
                response = Transport(evidence_dir=root).fetch(url)
            self.assertEqual(session.get.call_count, 1)
            self.assertEqual(response.status, 0)
