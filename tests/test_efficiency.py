import argparse
import asyncio
import csv
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'dealer-url-validator/scripts'
sys.path.insert(0, str(SCRIPTS))
import browser_batch
from check_urls import analyze_row, homepage_hint_key, load_homepage_hints
from learned_routes import route_hints
from transport import Fetch, Transport
from url_rules import candidates, detect_provider, parse_page, provider_paths

HOME = 'https://dealer.example/'
HOME_HTML = '<title>Example Acura</title><h1>Welcome to Example Acura</h1><p>' + 'Dealer contact information. ' * 10 + '</p><nav><a href="/new-vehicles/">All New Vehicles</a></nav>'
SRP_HTML = '<title>New Acura Inventory</title><h1>New Acura Vehicles</h1><div class="vehicle-card">New Acura Integra</div>'


class CountingTransport:
    def __init__(self, values):
        self.values, self.calls = values, []

    def fetch(self, url, deadline=None):
        self.calls.append(url)
        return self.values.get(url, Fetch(url, url, 404))


class Efficiency(unittest.TestCase):
    def test_merged_evidence_reuses_older_usable_capture_when_new_capture_is_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); newer=root/'new'; older=root/'old';newer.mkdir();older.mkdir()
            url=HOME+'new-vehicles/'
            name=hashlib.sha256(url.encode()).hexdigest()+'.json'
            record={'requested_url':url,'final_url':url,'status':200,'text':SRP_HTML,'error':'','via':'browser','captured_at':datetime.now(timezone.utc).isoformat()}
            (older/name).write_text(json.dumps(record))
            (newer/name).write_text(json.dumps(dict(record,text='<title>New Vehicles</title>')))
            with patch('transport.requests.Session',side_effect=AssertionError('Unexpected HTTP request')):
                response=Transport(evidence_dir=[newer,older]).fetch(url)
            self.assertEqual(response.via,'browser')
            self.assertEqual(response.text,SRP_HTML)
            self.assertEqual(browser_batch.reusable_record([newer,older],url)['text'],SRP_HTML)

    def test_visit_history_covers_failed_attempts_and_redirect_aliases_without_overwriting_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root/'navigation.jsonl'
            timestamp = datetime.now(timezone.utc).isoformat()
            journal.write_text(json.dumps({'site':'dealer.example','url':HOME+'new-vehicles/','started_at':timestamp,'status':403})+'\n')
            jobs = [(0, {'resolved_homepage_url':HOME}, {'input':{'homepage_url':'https://old.example/'}})]
            history = root/'history.json'
            browser_batch.save_browser_history(history,journal,jobs,root/'evidence',root/'run','homepage_url')
            self.assertEqual(browser_batch.recently_investigated_sites(history), {'dealer.example','old.example'})
            saved = json.loads(history.read_text())
            saved['sites']['dealer.example']['outcome'] = 'Reviewed as blocked'
            history.write_text(json.dumps(saved))
            browser_batch.save_browser_history(history,journal,jobs,root/'evidence',root/'run','homepage_url')
            self.assertEqual(json.loads(history.read_text())['sites']['dealer.example']['outcome'],'Reviewed as blocked')

    def test_route_learning_merge_removes_old_model_hints_without_removing_full_new_routes(self):
        import learn_routes
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'source';source.mkdir(); evidence=root/'evidence';evidence.mkdir()
            (source/'results.csv').write_text('homepage_selection_status,srp_selection_status\n')
            (source/'candidates.jsonl').write_text('')
            output=root/'routes.json'
            output.write_text(json.dumps({'browser_evidence':str(root/'older-evidence'),'sites':{'dealer.example':[{'srp_url':HOME+'new-inventory/?search=New+Civic-TypeR','expected_brand':'Honda'}, {'srp_url':HOME+'new-inventory/','expected_brand':'Honda'}]}}))
            learn_routes.run(argparse.Namespace(run_dir=str(source),browser_evidence=str(evidence),out=str(output),merge=True))
            self.assertEqual([r['srp_url'] for r in json.loads(output.read_text())['sites']['dealer.example']], [HOME+'new-inventory/'])
            self.assertEqual(json.loads(output.read_text())['browser_evidence_dirs'],[str(evidence.resolve()),str((root/'older-evidence').resolve())])

    def test_repeated_html_is_parsed_once_without_sharing_mutable_page_state(self):
        import url_rules
        url_rules._parse_page.cache_clear()
        with patch.object(url_rules, 'BeautifulSoup', wraps=url_rules.BeautifulSoup) as parser:
            first = parse_page(HOME_HTML, HOME)
            first.inventory_links.clear()
            second = parse_page(HOME_HTML, HOME)
            self.assertTrue(second.inventory_links)
            self.assertEqual(parser.call_count, 1)
            self.assertTrue(parse_page(HOME_HTML, 'https://another.example/').inventory_links[0]['url'].startswith('https://another.example/'))
            self.assertEqual(parser.call_count, 2)

    def test_recent_browser_history_cools_down_successes_and_failures_only_for_one_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            path.write_text(json.dumps({'sites': {'dealer.example': {'investigated_at': datetime.now(timezone.utc).isoformat(), 'outcome': 'blocked'}, 'old.example': {'investigated_at': (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()}}}))
            self.assertEqual(browser_batch.recently_investigated_sites(path), {'dealer.example'})

    def test_resolved_homepage_skips_old_redirect_and_uses_destination_routes(self):
        destination = 'https://replacement.example/'
        url = destination + 'new-vehicles/'
        transport = CountingTransport({destination: Fetch(destination, destination, 200, HOME_HTML), url: Fetch(url, url, 200, SRP_HTML)})
        settings = {'row_timeout': 20, 'max_candidates': 10, 'am_dealer_ids': False,
                    '_resolved_homepages': {homepage_hint_key('AC0001', HOME): destination}}
        row = {'dealer_id': 'AC0001', 'homepage_url': HOME, 'brand': 'Acura'}
        result, _ = analyze_row(row, {'homepage': 'homepage_url', 'brand': 'brand', 'dealer_id': 'dealer_id'}, transport, settings, {})
        self.assertEqual(result['recommended_new_srp_url'], url)
        self.assertTrue(result['homepage_domain_changed'])
        self.assertEqual(transport.calls, [destination, url])
        self.assertEqual(row['homepage_url'], HOME)

    def test_homepage_hints_match_dealer_and_original_url_and_reject_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'analysis.csv'
            path.write_text('dealer_id,homepage_url,resolved_homepage_url\nAC0001,https://dealer.example/,https://replacement.example/\n')
            hints = load_homepage_hints(path)
            self.assertEqual(hints[homepage_hint_key('AC0001', HOME)], 'https://replacement.example/')
            self.assertNotIn(homepage_hint_key('AC0002', HOME), hints)
            path.write_text(path.read_text() + 'AC0001,https://dealer.example/,https://another.example/\n')
            with self.assertRaises(ValueError):
                load_homepage_hints(path)

    def test_dealer_eprocess_brand_route_uses_expected_brand_and_provider(self):
        route = '/search/new-acura/?tp=new'
        self.assertIn(route, provider_paths('Dealer eProcess', 'Acura'))
        self.assertNotIn(route, provider_paths('DealerOn', 'Acura'))
        self.assertFalse(any('{brand}' in path or '/new-/' in path
                             for path in provider_paths('Dealer eProcess')))
        self.assertIn('/search/new-mercedes-benz/?tp=new',
                      provider_paths('Dealer eProcess', 'Mercedes'))
        self.assertEqual(candidates(HOME, None, '', 'Dealer eProcess', '', 'Acura')[0]['url'], HOME + route.lstrip('/'))
        self.assertEqual(detect_provider('<script src="https://cdn.dealereprocess.org/app.js"></script>')[0], 'Dealer eProcess')

    def test_dealer_eprocess_route_is_verified_without_browser(self):
        url = HOME + 'search/new-acura/?tp=new'
        home_html = HOME_HTML.replace('<nav>', '<div>').replace('</nav>', '</div>').replace('<a href="/new-vehicles/">All New Vehicles</a>', '')
        transport = CountingTransport({HOME: Fetch(HOME, HOME, 200, home_html), url: Fetch(url, url, 200, SRP_HTML)})
        result, _ = analyze_row({'homepage_url': HOME, 'brand': 'Acura', 'website_provider': 'Dealer eProcess'},
                                {'homepage': 'homepage_url', 'brand': 'brand', 'provider': 'website_provider'}, transport,
                                {'row_timeout': 20, 'max_candidates': 10, 'am_dealer_ids': False}, {})
        self.assertEqual(result['recommended_new_srp_url'], url)
        self.assertEqual(result['srp_inventory_scope'], 'brand_specific')
        # Compare equal-rank provider routes for ambiguity, then stop generic guesses.
        self.assertEqual(transport.calls, [HOME, url, HOME + 'new-vehicles/', HOME + 'searchnew.aspx'])

    def test_learned_route_does_not_turn_blocked_http_into_verified_inventory(self):
        url = HOME + 'search/new-acura/?tp=new'
        hint = dict(srp_url=url, expected_brand='Acura', inventory_scope='brand_specific', observed_at=datetime.now(timezone.utc).isoformat())
        transport = CountingTransport({HOME: Fetch(HOME, HOME, 403), url: Fetch(url, url, 403)})
        result, _ = analyze_row({'homepage_url': HOME, 'brand': 'Acura'},
                                {'homepage': 'homepage_url', 'brand': 'brand'}, transport,
                                {'row_timeout': 20, 'max_candidates': 10, 'am_dealer_ids': False, '_learned_routes': {'dealer.example': [hint]}}, {})
        self.assertEqual(result['recommended_new_srp_url'], '')
        self.assertEqual(result['srp_selection_status'], 'unverified_blocked')
        self.assertEqual(transport.calls, [HOME, url])

    def test_learned_route_is_verified_by_http_and_skips_weaker_paths(self):
        url = HOME + 'new-vehicles/'
        hint = dict(srp_url=url, expected_brand='Acura', inventory_scope='brand_specific', observed_at=datetime.now(timezone.utc).isoformat())
        settings = {'row_timeout': 20, 'max_candidates': 10, 'am_dealer_ids': False, '_learned_routes': {'dealer.example': [hint]}}
        transport = CountingTransport({HOME: Fetch(HOME, HOME, 403), url: Fetch(url, url, 200, SRP_HTML)})
        result, _ = analyze_row({'homepage_url': HOME, 'brand': 'Acura', 'new_srp_url': HOME + 'inventory/new'},
                                {'homepage': 'homepage_url', 'brand': 'brand', 'srp': 'new_srp_url'}, transport, settings, {})
        self.assertEqual(result['recommended_new_srp_url'], url)
        self.assertEqual(result['srp_selection_source'], 'learned')
        self.assertEqual(transport.calls, [HOME, url])

    def test_unknown_brand_all_new_result_stops_lower_rank_search(self):
        url = HOME + 'new-vehicles/'
        transport = CountingTransport({HOME: Fetch(HOME, HOME, 200, HOME_HTML), url: Fetch(url, url, 200, SRP_HTML)})
        result, _ = analyze_row({'homepage_url': HOME}, {'homepage': 'homepage_url'}, transport,
                                {'row_timeout': 20, 'max_candidates': 10, 'am_dealer_ids': False}, {})
        self.assertEqual(result['srp_inventory_scope'], 'all_new')
        self.assertEqual(transport.calls, [HOME, url])

    def test_old_or_other_brand_scoped_hints_are_not_used(self):
        old = dict(srp_url=HOME + 'new-vehicles/', expected_brand='Acura', inventory_scope='brand_specific', observed_at=(datetime.now(timezone.utc)-timedelta(days=31)).isoformat())
        other = dict(old, expected_brand='Buick', observed_at=datetime.now(timezone.utc).isoformat())
        self.assertEqual(route_hints({'dealer.example': [old, other]}, HOME, 'Acura'), [])

    def test_ambiguity_is_not_automatically_a_browser_task(self):
        row = {'homepage_selection_status': 'verified', 'srp_selection_status': 'ambiguous'}
        self.assertFalse(browser_batch.browser_needed(row))
        self.assertTrue(browser_batch.browser_needed(row, include_ambiguity=True))

    def test_incomplete_browser_head_does_not_suppress_http(self):
        class Handler(BaseHTTPRequestHandler):
            calls = 0
            def do_GET(self):
                Handler.calls += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(SRP_HTML.encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                url = f'http://127.0.0.1:{server.server_port}/new-vehicles/'
                record = dict(requested_url=url, final_url=url, status=200, text='<html><head><title>New Acura</title></head></html>', error='', via='browser', captured_at=datetime.now(timezone.utc).isoformat())
                (Path(directory)/(hashlib.sha256(url.encode()).hexdigest()+'.json')).write_text(json.dumps(record))
                response = Transport(evidence_dir=directory).fetch(url)
                self.assertEqual(response.via, 'http')
                self.assertEqual(Handler.calls, 1)
        finally:
            server.shutdown()
            server.server_close()


class BrowserReuse(unittest.IsolatedAsyncioTestCase):
    async def test_once_per_site_shares_one_inventory_navigation_across_duplicate_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source=root/'source'; source.mkdir(); cache=root/'cache'; cache.mkdir()
            urls = [HOME+'new-vehicles/', HOME+'inventory/new/', HOME+'new-inventory/index.htm']
            rows = [dict(dealer_id=f'AC000{i}', homepage_url=HOME, resolved_homepage_url=HOME, expected_brand='Acura', homepage_selection_status='verified', srp_selection_status='unverified_blocked', best_unverified_srp_url=url) for i,url in enumerate(urls)]
            with (source/'results.csv').open('w',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
            records=[{'input':{'homepage_url':HOME},'candidates':[{'url':url,'sources':['supplied'],'rank':3,'status':'unverified_blocked'}]} for url in urls]
            (source/'candidates.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
            (source/'manifest.json').write_text(json.dumps({'identity':{'settings':{'columns':{'homepage':'homepage_url','dealer_id':'dealer_id'}}}}))
            html=HOME_HTML.replace('<nav><a href="/new-vehicles/">All New Vehicles</a></nav>','')
            record=dict(requested_url=HOME,final_url=HOME,status=200,text=html,error='',via='browser',captured_at=datetime.now(timezone.utc).isoformat())
            (cache/(hashlib.sha256(HOME.encode()).hexdigest()+'.json')).write_text(json.dumps(record))
            args=argparse.Namespace(run_dir=str(source),out=str(root/'out'),limit=0,indices_file=None,concurrency=1,max_candidates=1,timeout=1,settle=0,challenge_wait=0,channel='chrome',headed=False,skip_consent_blocker=False,include_ambiguity=False,evidence_from=str(cache),once_per_site=True,browser_history=str(root/'history.json'))
            context=Mock();context.close=AsyncMock()
            browser=Mock();browser.new_context=AsyncMock(return_value=context);browser.close=AsyncMock()
            playwright=Mock();playwright.chromium.launch=AsyncMock(return_value=browser)
            manager=Mock();manager.__aenter__=AsyncMock(return_value=playwright);manager.__aexit__=AsyncMock(return_value=False)
            calls=[]
            async def capture(context,url,out,*args,**kwargs):
                calls.append(url)
                record=dict(requested_url=url,final_url=url,status=200,text=SRP_HTML,error='',via='browser',captured_at=datetime.now(timezone.utc).isoformat())
                (out/(hashlib.sha256(url.encode()).hexdigest()+'.json')).write_text(json.dumps(record))
            with patch.object(browser_batch,'async_playwright',return_value=manager), patch.object(browser_batch,'probe',side_effect=capture):
                await browser_batch.run(args)
            self.assertEqual(calls,[urls[0]])
            summary=json.loads((root/'out/summary.json').read_text())
            self.assertEqual(summary['completed_records'],3)
            self.assertEqual(browser_batch.recently_investigated_sites(root/'history.json'), {'dealer.example'})
            # Saving operational history must not prevent resuming the same run.
            with patch.object(browser_batch,'async_playwright',return_value=manager), patch.object(browser_batch,'probe',side_effect=capture):
                await browser_batch.run(args)
            self.assertEqual(calls, [urls[0]])
            args.out = str(root/'repeat')
            with patch.object(browser_batch,'async_playwright',return_value=manager), patch.object(browser_batch,'probe',side_effect=capture):
                await browser_batch.run(args)
            repeated = json.loads((root/'repeat/summary.json').read_text())
            self.assertEqual(repeated['history_skipped_records'], 3)
            self.assertEqual(repeated['browser_navigations'], 0)
            self.assertEqual(summary['browser_navigations'],1)

    async def test_fresh_evidence_resolves_queue_without_launching_chrome(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source=root/'source'; source.mkdir(); cache=root/'cache'; cache.mkdir()
            row = dict(dealer_id='AC0001', homepage_url=HOME, resolved_homepage_url=HOME, expected_brand='Acura', homepage_selection_status='unverified_blocked', srp_selection_status='unverified_blocked', best_unverified_srp_url=HOME+'new-vehicles/')
            with (source/'results.csv').open('w', newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=list(row));writer.writeheader();writer.writerow(row)
            candidate=dict(url=HOME+'new-vehicles/', sources=['navigation'], rank=1, status='unverified_blocked')
            weaker=dict(url=HOME+'inventory/new', sources=['supplied'],rank=3,status='unverified_content')
            (source/'candidates.jsonl').write_text(json.dumps({'input':{'homepage_url':HOME},'candidates':[candidate,weaker]})+'\n')
            (source/'manifest.json').write_text(json.dumps({'identity':{'settings':{'columns':{'homepage':'homepage_url','dealer_id':'dealer_id'}}}}))
            for url, html in ((HOME,HOME_HTML),(HOME+'new-vehicles/',SRP_HTML)):
                record=dict(requested_url=url,final_url=url,status=200,text=html,error='',via='browser',captured_at=datetime.now(timezone.utc).isoformat())
                (cache/(hashlib.sha256(url.encode()).hexdigest()+'.json')).write_text(json.dumps(record))
            args=argparse.Namespace(run_dir=str(source),out=str(root/'out'),limit=0,indices_file=None,concurrency=1,max_candidates=2,timeout=1,settle=0,challenge_wait=0,channel='chrome',headed=False,skip_consent_blocker=False,include_ambiguity=False,evidence_from=str(cache),browser_history=str(root/'history.json'))
            playwright=Mock();playwright.chromium.launch=AsyncMock()
            manager=Mock();manager.__aenter__=AsyncMock(return_value=playwright);manager.__aexit__=AsyncMock(return_value=False)
            with patch.object(browser_batch,'async_playwright',return_value=manager):
                await browser_batch.run(args)
            playwright.chromium.launch.assert_not_awaited()
            summary=json.loads((root/'out/summary.json').read_text())
            self.assertEqual(summary['completed_records'],1)
            self.assertEqual(summary['browser_navigations'],0)
            row['srp_selection_status']='verified'
            with (source/'results.csv').open('w', newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=list(row));writer.writeheader();writer.writerow(row)
            args.out=str(root/'home-only')
            with patch.object(browser_batch,'async_playwright',return_value=manager):
                await browser_batch.run(args)
            coverage=json.loads((root/'home-only/coverage.jsonl').read_text())
            self.assertEqual(coverage['visited_urls'],[HOME])
            playwright.chromium.launch.assert_not_awaited()
