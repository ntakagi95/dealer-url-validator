import argparse
import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dealer-url-validator/scripts'))
from check_urls import analyze_row
from manual_reviews import apply_confirmation, homepage_hint, matching_review, route_hint
from review_results import run as review_run
from test_efficiency import CountingTransport, HOME, SRP_HTML
from transport import Fetch


class ManualReviews(unittest.TestCase):
    def setUp(self):
        self.url = HOME + 'new-inventory/index.htm'
        self.item = dict(dealer_id='AC0001', source_homepage_url=HOME,
                         expected_brand='Acura', homepage_hint_url=HOME,
                         srp_url=self.url, inventory_scope='all_new',
                         confirmation='opened_full_new_inventory',
                         confirmed_at=datetime.now(timezone.utc).isoformat())
        self.row = dict(dealer_id='AC0001', homepage_url=HOME, brand='Acura',
                        new_srp_url=HOME + 'new-inventory/')
        self.columns = dict(dealer_id='dealer_id', homepage='homepage_url', brand='brand', srp='new_srp_url')
        self.settings = dict(row_timeout=20, max_candidates=10, am_dealer_ids=False,
                             _manual_reviews=[self.item])

    def test_confirmation_skips_inventory_fetch_and_preserves_failed_home_evidence(self):
        transport = CountingTransport({HOME: Fetch(HOME, HOME, 403)})
        result, evidence = analyze_row(self.row, self.columns, transport, self.settings, {})
        self.assertEqual(transport.calls, [HOME])
        self.assertEqual(result['recommended_new_srp_url'], self.url)
        self.assertEqual(result['srp_selection_source'], 'manual_review')
        self.assertEqual(result['srp_inventory_scope'], 'all_new')
        self.assertEqual(result['homepage_selection_status'], 'unverified_blocked')
        self.assertEqual(evidence['homepage_observations'][0]['status'], 403)
        self.assertEqual(evidence['candidates'], [])

    def test_identity_and_brand_mismatch_do_not_reuse_confirmation(self):
        for dealer, home, brand in [('AC0002', HOME, 'Acura'), ('AC0001', 'https://other.example/', 'Acura'), ('AC0001', HOME, 'Honda'), ('', HOME, 'Acura')]:
            self.assertIsNone(matching_review([self.item], dealer, home, brand))

    def test_expired_confirmation_prioritizes_document_route_without_verifying_block(self):
        self.item['confirmed_at'] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        transport = CountingTransport({HOME: Fetch(HOME, HOME, 403), self.url: Fetch(self.url, self.url, 403)})
        result, _ = analyze_row(self.row, self.columns, transport, self.settings, {})
        self.assertEqual(transport.calls, [HOME, self.url])
        self.assertEqual(result['srp_selection_status'], 'unverified_blocked')
        self.assertEqual(result['recommended_new_srp_url'], '')
        self.item['confirmed_at'] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        self.assertEqual(route_hint(self.item), [])
        self.assertEqual(homepage_hint(self.item), '')

    def test_homepage_only_correction_guides_request_and_does_not_confirm_srp(self):
        replacement = 'https://replacement.example/'
        self.item.update(homepage_hint_url=replacement, srp_url='', confirmation='homepage_correction_only')
        transport = CountingTransport({replacement: Fetch(replacement, replacement, 403)})
        result, _ = analyze_row(self.row, self.columns, transport, self.settings, {})
        self.assertEqual(transport.calls[0], replacement)
        self.assertTrue(result['homepage_domain_changed'])
        self.assertEqual(result['recommended_new_srp_url'], '')

    def test_candidates_subsets_and_ports_cannot_be_manual_confirmation(self):
        for changes in [dict(confirmation='candidate'), dict(srp_url=self.url+'?search=New+Civic-TypeR'), dict(srp_url='https://dealer.example:8443/new-vehicles/')]:
            self.assertFalse(apply_confirmation({}, {}, dict(self.item, **changes)))

    def test_offline_review_reuses_confirmation_then_expires_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / 'source'; source.mkdir()
            result, evidence = analyze_row(self.row, self.columns, CountingTransport({HOME: Fetch(HOME, HOME, 403)}), self.settings, {})
            row = dict(self.row, **result)
            with (source/'results.csv').open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row)); writer.writeheader(); writer.writerow(row)
            (source/'candidates.jsonl').write_text(json.dumps(dict(index=0, **evidence))+'\n')
            (source/'manifest.json').write_text(json.dumps({'identity': {'settings': {'columns': self.columns}}}))
            registry = root/'manual.json'; registry.write_text(json.dumps({'reviews': [self.item]}))
            args = argparse.Namespace(run_dir=str(source), out=str(root/'fresh'), browser_evidence=None,
                                      browser_coverage=None, manual_reviews=str(registry))
            review_run(args)
            with (root/'fresh/results.csv').open() as handle:
                fresh = next(csv.DictReader(handle))
            self.assertEqual(fresh['srp_selection_source'], 'manual_review')
            self.item['confirmed_at'] = (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
            registry.write_text(json.dumps({'reviews': [self.item]}))
            args.run_dir=str(root/'fresh'); args.out=str(root/'expired'); review_run(args)
            with (root/'expired/results.csv').open() as handle:
                expired = next(csv.DictReader(handle))
            self.assertNotEqual(expired['srp_selection_status'], 'verified')
            self.assertNotIn('manual_srp_confirmation', json.loads((root/'expired/candidates.jsonl').read_text()))
