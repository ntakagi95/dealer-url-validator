import asyncio
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'dealer-url-validator/scripts'
sys.path.insert(0, str(SCRIPTS))
from browser_probe import challenge_document, probe
from browser_batch import review_jobs
from review_results import original_manifest
from url_rules import is_homepage, parse_page, url_rejection


class BrowserReview(unittest.IsolatedAsyncioTestCase):
    async def test_cleared_challenge_waits_for_inventory_without_second_navigation(self):
        page = Mock()
        page.url = 'https://dealer.example/new-vehicles/'
        response = Mock(status=200)
        response.request.redirected_from = None
        page.goto = AsyncMock(return_value=response)
        page.wait_for_load_state = AsyncMock()
        page.wait_for_function = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.evaluate = AsyncMock()
        page.content = AsyncMock(side_effect=['<title>Just a moment...</title>', '<title>New Vehicles</title>', '<h1>New Vehicles</h1><div class="vehicle-card">New stock</div>'])
        page.screenshot = AsyncMock()
        page.close = AsyncMock()
        context = Mock(new_page=AsyncMock(return_value=page))
        with tempfile.TemporaryDirectory() as directory:
            await probe(context, page.url, Path(directory), asyncio.Semaphore(1), 1, 0, inventory_wait=8)
        page.goto.assert_awaited_once()
        self.assertEqual(page.wait_for_function.await_count, 2)

    def test_challenge_detection_does_not_match_marketing_copy(self):
        self.assertTrue(challenge_document('<title>Just a moment...</title>'))
        self.assertTrue(challenge_document('<form id="challenge-form"></form>'))
        self.assertFalse(challenge_document('<title>New Vehicles</title><p>Our security check protects you.</p>'))

    def test_inventory_destination_is_not_verified_as_homepage(self):
        html = '<title>Dealer Inventory</title><h1>New and Used Vehicles</h1><div class="vehicle-card">' + 'Listing information ' * 20 + '</div>'
        url = 'https://dealer.example/inventory/'
        self.assertFalse(is_homepage(url, parse_page(html, url)))
        html = html.replace('New and Used Vehicles', 'Welcome to Example Acura')
        self.assertTrue(is_homepage('https://dealer.example/', parse_page(html, 'https://dealer.example/')))
        html = html.replace('<h1>Welcome to Example Acura</h1>', '<h2>New Vehicles</h2>')
        self.assertTrue(is_homepage('https://dealer.example/', parse_page(html, 'https://dealer.example/')))
        html += '<h2>Schedule Service</h2>'
        self.assertTrue(is_homepage('https://dealer.example/', parse_page(html, 'https://dealer.example/')))
        self.assertFalse(is_homepage('https://dealer.example/service/', parse_page(html, 'https://dealer.example/service/')))

    async def test_screenshot_failure_preserves_verified_document(self):
        page = Mock()
        page.url = 'https://dealer.example/new-vehicles/'
        response = Mock(status=200)
        response.request.redirected_from = None
        page.goto = AsyncMock(return_value=response)
        page.wait_for_load_state = AsyncMock()
        page.wait_for_function = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.evaluate = AsyncMock()
        page.content = AsyncMock(return_value='<h1>New Vehicles</h1><div class="vehicle-card">Car</div>')
        page.screenshot = AsyncMock(side_effect=TimeoutError('screenshot'))
        page.close = AsyncMock()
        context = Mock(new_page=AsyncMock(return_value=page))
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            await probe(context, page.url, out, asyncio.Semaphore(1), 1, 0)
            result = json.loads(next(out.glob('*.json')).read_text())
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['error'], '')
        self.assertTrue(result['text'])
        self.assertTrue(result['warnings'])
        page.close.assert_awaited_once()

    async def test_navigation_timeout_keeps_received_response_and_dom(self):
        page = Mock()
        page.url = 'https://dealer.example/'
        handlers = {}
        page.on.side_effect = lambda event, fn: handlers.update({event: fn})
        response = Mock(status=200, frame=page.main_frame)
        response.request.is_navigation_request.return_value = True
        async def navigate(*args, **kwargs):
            handlers['response'](response)
            raise TimeoutError('DOMContentLoaded pending')
        page.goto = navigate
        page.wait_for_load_state = AsyncMock()
        page.wait_for_function = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.evaluate = AsyncMock()
        page.content = AsyncMock(return_value='<h1>Welcome to our dealership</h1>')
        page.screenshot = AsyncMock()
        page.close = AsyncMock()
        context = Mock(new_page=AsyncMock(return_value=page))
        with tempfile.TemporaryDirectory() as directory:
            await probe(context, page.url, Path(directory), asyncio.Semaphore(1), 1, 0)
            result = json.loads(next(Path(directory).glob('*.json')).read_text())
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['error'], '')
        self.assertTrue(result['warnings'])

    def test_nested_audits_retain_original_column_metadata(self):
        identity = {'identity': {'settings': {'columns': {'homepage': 'homepage_url'}}}}
        self.assertEqual(original_manifest({'source_manifest': {'source_manifest': identity}}), identity)

    def test_named_model_child_routes_are_excluded(self):
        for path in ('new-vehicles/rdx/', 'new-vehicles/adx/', 'new-inventory/tiguan/'):
            self.assertTrue(url_rejection('https://dealer.example/' + path))
        for path in ('new-vehicles/acura/', 'new-inventory/index.htm', 'new-vehicles/'):
            self.assertFalse(url_rejection('https://dealer.example/' + path))

    def test_rendered_navigation_subset_routes_are_excluded(self):
        for path in ('high-mpg-new-vehicles/', 'new-vehicles/?_dFR%5Bintransit_filter%5D%5B0%5D=In-Transit'):
            self.assertTrue(url_rejection('https://dealer.example/' + path))

    def test_queue_includes_homepage_only_and_missing_srp_records(self):
        import csv
        rows = [dict(dealer_id='A', homepage_selection_status='unverified_blocked', srp_selection_status='verified'),
                dict(dealer_id='B', homepage_selection_status='verified', srp_selection_status='not_found'),
                dict(dealer_id='C', homepage_selection_status='verified', srp_selection_status='verified')]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            with (source / 'results.csv').open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            (source / 'candidates.jsonl').write_text('{}\n' * 3)
            self.assertEqual([x[0] for x in review_jobs(source)], [0, 1])
