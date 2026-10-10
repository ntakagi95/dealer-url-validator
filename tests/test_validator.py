import csv
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlencode
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'dealer-url-validator/scripts'
sys.path.insert(0, str(SCRIPTS))
from check_urls import analyze_row, resolve_home
from transport import Fetch
from export_results import export_row
from url_rules import brand_form_candidates, candidates, is_homepage, judge_srp, normalize_url, parse_page, provider_paths, select_unverified, select_verified, site_key

HOME = 'https://dealer.example/'
HOME_HTML = '<title>Acura Dealer</title><h1>Welcome to Example Acura</h1><p>' + 'Dealer information and contact details. ' * 10 + '</p><div class="main-menu"><a href="/new-vehicles/">View All New Inventory</a></div>'
SRP_HTML = '<title>New Acura Inventory</title><h1>New Acura Vehicles</h1><div class="vehicle-card">2026 Acura Integra</div>'


class FakeTransport:
    def __init__(self, values):
        self.values = values

    def fetch(self, url, deadline=None):
        return self.values.get(url, Fetch(url, url, 404))


class Judgments(unittest.TestCase):
    def test_inventory_purpose_uses_primary_heading_without_faq_contamination(self):
        url = HOME + 'new-vehicles/'
        html = '<title>New Volkswagen Cars</title><h1>New Volkswagen Cars for Sale</h1><div class="vehicle-card"></div><h1>For new and used vehicles, what financing is available?</h1>'
        self.assertEqual(judge_srp(url, parse_page(html, url), 'Volkswagen', False, HOME)[0], 'verified')
        combined = html.replace('<h1>New Volkswagen Cars for Sale</h1>', '<h1>New and Used Volkswagen Cars for Sale</h1>')
        self.assertEqual(judge_srp(url, parse_page(combined, url), 'Volkswagen', False, HOME)[0], 'rejected')
        vehicle = {'vin': '1HGCM82633A004352', 'make': 'Volkswagen', 'type': 'Used'}
        mixed = html + '<div data-vehicle=\'' + json.dumps(vehicle) + '\'></div>'
        self.assertEqual(judge_srp(url, parse_page(mixed, url), 'Volkswagen', False, HOME)[0], 'rejected')

    def test_inventory_search_keyword_cannot_restrict_full_new_recommendation(self):
        page = parse_page('<title>New Honda Inventory</title><h1>New Inventory</h1><div class="vehicle-card"></div>', HOME + 'new-inventory/')
        subset = HOME + 'new-inventory/?new=true&fq=1&search=New+Civic-TypeR'
        self.assertEqual(judge_srp(subset, page, 'Honda', False, HOME)[0], 'rejected')
        self.assertEqual(judge_srp(HOME + 'new-inventory/?search=new', page, 'Honda', False, HOME)[0], 'verified')
        self.assertEqual(judge_srp(HOME + 'new-inventory/', page, 'Honda', False, HOME)[0], 'verified')

    def test_dealer_inspire_vehicle_json_proves_condition_and_rejects_mixed_stock(self):
        vehicle = {'vin': '1HGCM82633A004352', 'make': 'Honda', 'type': 'New'}
        html = '<title>Honda Cars For Sale</title><h1>Hondas For Sale</h1><select></select><select></select><div class="vehicle-card" data-vehicle=\'' + json.dumps(vehicle) + '\'></div>'
        page = parse_page(html, HOME + 'new-vehicles/')
        self.assertEqual(judge_srp(HOME + 'new-vehicles/', page, 'Honda', False, HOME)[0], 'verified')
        mixed = parse_page(html.replace('"New"', '"Used"'), HOME + 'new-vehicles/')
        self.assertEqual(judge_srp(HOME + 'new-vehicles/', mixed, 'Honda', False, HOME)[0], 'rejected')
        self.assertEqual(judge_srp(HOME + 'cars/new/?make=BMW--X1', page, 'BMW', False, HOME)[0], 'rejected')

    def test_homepage_discovery_keeps_terminal_new_routes_without_trailing_slash(self):
        html = '<title>Dealer</title><nav><a href="/inventory/new">Buy Online</a><a href="/cars/new">All New</a></nav>'
        page = parse_page(html, HOME)
        self.assertEqual({x['url'] for x in page.inventory_links}, {HOME + 'inventory/new', HOME + 'cars/new'})

    def test_competing_fuel_and_model_subsets_do_not_count_as_full_new_inventory(self):
        fuel = parse_page('<title>Electric/Hybrid - New Kia Cars</title><h1>Electric/Hybrid - New Kia Cars</h1><div class="vehicle-card"></div>', HOME + 'new.cfm/customtype/8/')
        self.assertEqual(judge_srp(HOME + 'new.cfm/customtype/8/', fuel, 'Kia', False, HOME)[0], 'rejected')
        model_url = HOME + 'new-volkswagen-atlas-for-sale-houston-tx.html'
        model = parse_page('<title>New Volkswagen Atlas for Sale</title><h1>Shop New Volkswagen Atlas Models</h1><div class="vehicle-card"></div>', model_url)
        self.assertEqual(judge_srp(model_url, model, 'Volkswagen', False, HOME)[0], 'rejected')
        full_url = HOME + 'new-volkswagen-vehicles-for-sale-houston-tx.html'
        full = parse_page('<title>New Volkswagen Vehicles for Sale</title><h1>New Volkswagen Vehicles</h1><div class="vehicle-card"></div>', full_url)
        self.assertEqual(judge_srp(full_url, full, 'Volkswagen', False, HOME)[0], 'verified')

    def test_vin_backed_new_collection_does_not_require_new_in_title(self):
        cars = [{'@type': ['Product', 'Car'], 'vehicleIdentificationNumber': vin, 'brand': 'Honda', 'itemCondition': 'https://schema.org/NewCondition'} for vin in ('1HGCM82633A004352', '1HGCM82633A004353')]
        data = {'@type': 'CollectionPage', 'name': 'New Inventory | Example Honda', 'url': HOME + 'new-inventory/index.htm', 'offers': {'itemOffered': cars}}
        def page():
            return parse_page('<title>Honda Cars for Sale</title><h1>Honda Cars</h1><div class="vehicle-card"></div><script type="application/ld+json">' + json.dumps(data) + '</script>', data['url'])
        self.assertEqual(judge_srp(data['url'], page(), 'Honda', False, HOME)[0], 'verified')
        cars[1]['itemCondition'] = 'https://schema.org/UsedCondition'
        self.assertEqual(judge_srp(data['url'], page(), 'Honda', False, HOME)[0], 'rejected')
        cars[1]['itemCondition'] = 'https://schema.org/NewCondition'
        data['@type'] = 'WebPage'
        self.assertEqual(judge_srp(data['url'], page(), 'Honda', False, HOME)[0], 'unverified_content')

    def test_new_stock_data_can_establish_inventory_when_title_omits_new(self):
        html = '<title>Honda Cars for Sale</title><h1>Honda Cars</h1><select></select><select></select><div class="vehicle-card" data-vin="1HGCM82633A004352" data-make="Honda" data-condition="new"></div>'
        page = parse_page(html, HOME + 'new-inventory/index.htm')
        self.assertEqual(judge_srp(HOME + 'new-inventory/index.htm', page, 'Honda', False, HOME)[0], 'verified')
        mixed = parse_page(html.replace('data-condition="new"', 'data-condition="used"'), HOME + 'new-inventory/index.htm')
        self.assertEqual(judge_srp(HOME + 'new-inventory/index.htm', mixed, 'Honda', False, HOME)[0], 'rejected')
        marketing = parse_page(html.replace('<select></select><select></select>', ''), HOME + 'new-inventory/index.htm')
        self.assertEqual(judge_srp(HOME + 'new-inventory/index.htm', marketing, 'Honda', False, HOME)[0], 'unverified_content')

    def test_encoded_inventory_filters_reject_models_but_allow_new_and_inactive_values(self):
        filters = {'appliedFilters': {'car_condition': {'value': {'New': True, 'Used': False}}, 'make': {'value': {'Acura': True}}}}
        url = HOME + 'inventory?' + urlencode({'filters': json.dumps(filters)})
        self.assertEqual(self.judge(url=url)[0], 'verified')
        filters['appliedFilters']['model'] = {'value': {'Integra': False}}
        self.assertEqual(self.judge(url=HOME + 'inventory?' + urlencode({'filters': json.dumps(filters)}))[0], 'verified')
        filters['appliedFilters']['model']['value']['Integra'] = True
        self.assertEqual(self.judge(url=HOME + 'inventory?' + urlencode({'filters': json.dumps(filters)}))[0], 'rejected')

    def test_pending_routes_use_provider_and_brand_but_remain_unverified(self):
        def entry(path, source='supplied_blueprint'):
            return {'url': HOME + path, 'sources': [source], 'rank': 5,
                    'status': 'unverified_blocked', 'fetch': {'final_url': HOME + path}}
        old = entry('searchnew.aspx')
        preferred = entry('search/new-acura/?tp=new')
        self.assertIs(select_unverified([old, preferred], HOME, 'Acura', 'Dealer eProcess'), preferred)
        self.assertEqual(preferred['status'], 'unverified_blocked')
        rejected = entry('new/defender.htm', 'navigation')
        self.assertIs(select_unverified([rejected, preferred], HOME, 'Acura', 'Dealer eProcess'), preferred)

    def test_model_body_and_fuel_documents_are_not_full_new_inventory(self):
        for path in ('new/defender.htm', 'new-tiguan-inventory.htm', 'new/new-chevrolet-trucks-avon-in.htm', 'new-cadillac/fuel-efficient-winston-salem.htm', 'new-vehicles/?_dFR%5Btype%5D%5B0%5D=Demo', 'inventory/new/mazda/cx-5'):
            self.assertEqual(self.judge(url=HOME + path)[0], 'rejected')
        self.assertEqual(self.judge(url=HOME + 'new-acura-inventory.htm')[0], 'verified')

    def test_brand_path_requires_page_corroboration(self):
        html = '<title>New Inventory</title><h1>New Vehicles</h1><div class="vehicle-card">New car</div>'
        self.assertEqual(self.judge(html=html, url=HOME + 'search/new-acura/?tp=new', multi=True)[0], 'scope_review')

    def test_verified_route_ties_use_scope_site_and_provider_without_new_requests(self):
        def entry(url):
            return {'status': 'verified', 'rank': 1, 'inventory_scope': 'brand_specific',
                    'sources': ['navigation'], 'canonical_url': url,
                    'fetch': {'final_url': url, 'ssl_bypassed': False, 'redirects': []}}
        root = entry(HOME + 'new-vehicles/')
        scoped = entry(HOME + 'new-vehicles/acura/')
        chosen, ambiguous = select_verified([root, scoped], False, HOME, 'Acura', 'Dealer Inspire')
        self.assertIs(chosen, scoped)
        self.assertFalse(ambiguous)
        foreign = entry('https://other-dealer.example/new-vehicles/')
        chosen, ambiguous = select_verified([foreign, root], False, HOME, 'Acura')
        self.assertIs(chosen, root)
        self.assertFalse(ambiguous)
        canonical = entry(HOME + 'new-inventory/index.htm')
        chosen, ambiguous = select_verified([root, canonical], False, HOME, 'Acura', 'Dealer.com')
        self.assertIs(chosen, canonical)
        self.assertFalse(ambiguous)

    def test_equal_supported_distinct_verified_routes_stay_ambiguous(self):
        entries = [{'status': 'verified', 'rank': 1, 'inventory_scope': 'brand_specific',
                    'fetch': {'final_url': HOME + path, 'ssl_bypassed': False, 'redirects': []}}
                   for path in ('inventory/new/', 'new-vehicles/')]
        self.assertTrue(select_verified(entries, False, HOME, 'Acura')[1])

    def test_internal_browser_error_url_cannot_be_verified_or_exported(self):
        url = 'chrome-error://chromewebdata/'
        self.assertFalse(is_homepage(url, parse_page(HOME_HTML, HOME)))
        self.assertEqual(self.judge(url=url)[0], 'rejected')
        self.assertEqual(export_row({'recommended_homepage_url': url, 'homepage_selection_status': 'verified'})['homepage_url'], '')

    def test_operational_export_adopts_verified_domain_changes_without_ports(self):
        row = {'dealer_id': 'AC0001', 'account_id': '0007493', 'website_provider': 'Dealer.com',
               'homepage_url': HOME, 'homepage_domain_changed': True,
               'recommended_homepage_url': 'https://replacement.example:443/', 'homepage_selection_status': 'verified',
               'recommended_new_srp_url': 'https://replacement.example:443/inventory?condition=new', 'srp_selection_status': 'verified'}
        exported = export_row(row)
        self.assertEqual(exported['homepage_url'], 'https://replacement.example/')
        self.assertEqual(exported['new_srp_url'], 'https://replacement.example/inventory?condition=new')
        self.assertEqual(exported['account_id'], '0007493')
        row.update(homepage_selection_status='unverified_blocked', recommended_new_srp_url='https://replacement.example:8443/inventory')
        self.assertEqual(export_row(row)['homepage_url'], '')
        self.assertEqual(export_row(row)['new_srp_url'], '')

    def test_default_ports_are_removed_without_changing_query_filters(self):
        query = '?filters=%7B%22car_condition%22%3A%22New%22%7D'
        self.assertEqual(normalize_url('https://dealer.example:443/inventory' + query), HOME + 'inventory' + query)
        self.assertEqual(normalize_url('http://dealer.example:80/'), 'http://dealer.example/')
        self.assertEqual(normalize_url('https://dealer.example:8443/'), 'https://dealer.example:8443/')
        self.assertEqual(normalize_url('https://[::1]:443/'), 'https://[::1]/')

    def test_informational_page_is_replaced_by_verified_home_link(self):
        url = HOME + 'porsche-premier-dealer-program/'
        html = '<title>Porsche Premier Dealer Program</title><h1>Porsche Premier Dealer Program</h1><p>' + 'Awards and dealership information. ' * 10 + '</p><a href="/">Home</a>'
        transport = FakeTransport({url: Fetch(url, url, 200, html), HOME: Fetch(HOME, HOME, 200, HOME_HTML)})
        home, _, status, _, observations = resolve_home(url, transport, None)
        self.assertEqual((home, status), (HOME, 'verified'))
        self.assertEqual([x['requested_url'] for x in observations], [url, HOME])

    def test_final_recommendations_remove_default_ports_and_preserve_input(self):
        raw_home = 'https://dealer.example:443/'
        url = HOME + 'new-vehicles/'
        transport = FakeTransport({HOME: Fetch(HOME, raw_home, 200, HOME_HTML), url: Fetch(url, raw_home + 'new-vehicles/', 200, SRP_HTML)})
        source_row = {'homepage_url': raw_home, 'brand': 'Acura'}
        result, _ = analyze_row(source_row,
                               {'homepage': 'homepage_url', 'brand': 'brand'}, transport,
                               {'row_timeout': 20, 'max_candidates': 10, 'am_dealer_ids': False}, {})
        self.assertEqual(source_row['homepage_url'], raw_home)
        self.assertEqual(result['recommended_homepage_url'], HOME)
        self.assertEqual(result['recommended_new_srp_url'], url)
        self.assertEqual(transport.values[HOME].final_url, raw_home)

    def judge(self, html=SRP_HTML, url=HOME + 'new-vehicles/', brand='Acura', multi=False):
        return judge_srp(url, parse_page(html, url), brand, multi, HOME)

    def test_inventory_is_verified_with_page_and_brand_evidence(self):
        self.assertEqual(self.judge()[0], 'verified')

    def test_redirected_homepage_is_rejected(self):
        self.assertEqual(self.judge(url=HOME)[0], 'rejected')

    def test_soft_error_page_and_error_class_are_rejected(self):
        for html in ('<title>Page not found</title>', '<title>Oops</title><div class="error-page">Oops</div>'):
            self.assertEqual(self.judge(html=html)[0], 'rejected')

    def test_unrelated_relocation_phrase_is_not_soft_404(self):
        self.assertEqual(self.judge(html=SRP_HTML + "<p>Our service team: we've moved to a new office.</p>")[0], 'verified')

    def test_used_redirect_and_model_subsets_are_rejected(self):
        for url in (HOME + 'used-vehicles/', HOME + 'new-vehicles/?model=Integra', HOME + 'new-vehicles/integra/', HOME + 'new-vehicles/?make=Acura&body=Sedan', HOME + 'featured-vehicles/new.htm', HOME + 'new-inventory/?compositeType=used'):
            self.assertEqual(self.judge(url=url)[0], 'rejected')

    def test_http_200_shell_needs_more_evidence(self):
        self.assertEqual(self.judge(html='<title>New Acura Inventory</title><div id="app"></div>')[0], 'unverified_content')

    def test_wrong_make_selector_is_rejected(self):
        self.assertEqual(self.judge(url=HOME + 'new-vehicles/?make=Buick')[0], 'rejected')

    def test_brand_filter_needs_page_corroboration(self):
        html = '<title>New Inventory</title><h1>New Vehicles</h1><div class="vehicle-card">Car</div>'
        self.assertEqual(self.judge(html=html, url=HOME + 'new-vehicles/?make=Acura', multi=True)[0], 'scope_review')

    def test_active_brand_filter_on_shared_site_is_allowed(self):
        html = '<title>New Buick GMC Inventory</title><h1>New Vehicles</h1><div class="vehicle-card">Buick</div><select name="make"><option value="Buick" selected>Buick</option></select>'
        self.assertEqual(self.judge(html=html, url=HOME + 'new-vehicles/?make=Buick', brand='Buick', multi=True)[0], 'verified')

    def test_unfiltered_multibrand_listing_is_labeled_all_new_fallback(self):
        html = '<title>New Buick GMC Inventory</title><h1>New Buick and GMC Vehicles</h1><div class="vehicle-card">New Buick</div>'
        result = self.judge(html=html, brand='Buick', multi=True)
        self.assertEqual(result[0], 'verified')
        self.assertIn('scope=all_new', result[2])

    def test_mixed_vehicle_data_produces_all_new_scope(self):
        html = SRP_HTML + '<script type="application/ld+json">[{"@type":"Car","brand":"Acura"},{"@type":"Car","brand":"Buick"}]</script>'
        self.assertIn('scope=all_new', self.judge(html=html, multi=True)[2])

    def test_rendered_brand_keyword_page_can_confirm_brand_scope(self):
        html = '<title>New Inventory</title><h1>New Vehicles</h1><div class="vehicle-card" data-make="Buick" data-vin="1GYTEFKL4SU102478" data-vehicletype="new">New Buick</div>'
        result = self.judge(html=html, url=HOME + 'searchnew.aspx?q=buick', brand='Buick', multi=True)
        self.assertEqual(result[0], 'verified')
        self.assertNotIn('scope=all_new', result[2])

    def test_rendered_used_cards_cannot_be_all_new_fallback(self):
        html = SRP_HTML + '<div data-make="Acura" data-vin="1GYTEFKL4SU102478" data-vehicletype="used">Used Acura</div>'
        self.assertEqual(self.judge(html=html)[0], 'rejected')

    def test_unconfirmed_brand_filter_does_not_become_all_new_fallback(self):
        html = '<title>New Inventory</title><h1>New Vehicles</h1><div class="vehicle-card">New car</div>'
        self.assertEqual(self.judge(html=html, url=HOME + 'new-vehicles/?make=Acura', multi=True)[0], 'scope_review')

    def test_partial_commercial_intransit_price_and_remaining_pages_are_rejected(self):
        for path in ('new-commercial-inventory/index.htm', 'new-intransit-vehicles.htm', 'remaining.htm', 'shop-new-under-25k.html', 'new-truck-inventory.html'):
            self.assertEqual(self.judge(url=HOME + path)[0], 'rejected')

    def test_model_body_heading_is_rejected(self):
        html = '<title>New Chevrolet Cars</title><h1>Explore new Chevrolet Malibu sedans</h1><div class="vehicle-card">Malibu</div>'
        self.assertEqual(self.judge(html=html, brand='Chevrolet')[0], 'rejected')

    def test_brand_scope_precedes_source_rank(self):
        base = {'status': 'verified', 'canonical_url': '', 'fetch': {'final_url': HOME + 'new-vehicles/', 'ssl_bypassed': False, 'redirects': []}}
        broad = dict(base, rank=1, inventory_scope='all_new')
        brand = dict(base, rank=3, inventory_scope='brand_specific', fetch=dict(base['fetch'], final_url=HOME + 'new-vehicles/?make=Buick'))
        chosen, ambiguous = select_verified([broad, brand], True)
        self.assertIs(chosen, brand)
        self.assertFalse(ambiguous)

    def test_scope_candidate_uses_observed_get_form(self):
        html = '<form action="/inventory/new" method="get"><input type="hidden" name="condition" value="new"><select name="make"><option value="Buick">Buick</option><option value="GMC">GMC</option></select></form>'
        options = brand_form_candidates(parse_page(html, HOME), HOME, 'Buick')
        self.assertEqual(options, [HOME + 'inventory/new?condition=new&make=Buick'])

    def test_dealer_subdomains_are_distinct_sites(self):
        self.assertNotEqual(site_key('https://edison.porsche.com/en'), site_key('https://livermore.porsche.com/en'))

    def test_am_brand_inference_is_explicit_and_brand_column_wins(self):
        transport = FakeTransport({HOME: Fetch(HOME, HOME, 200, HOME_HTML), HOME + 'new-vehicles/': Fetch(HOME + 'new-vehicles/', HOME + 'new-vehicles/', 200, SRP_HTML)})
        columns = {'homepage': 'homepage_url', 'brand': 'brand', 'dealer_id': 'dealer_id'}
        settings = {'row_timeout': 20, 'max_candidates': 10, 'am_dealer_ids': False}
        row = {'homepage_url': HOME, 'dealer_id': 'AC0001'}
        result, _ = analyze_row(row, columns, transport, settings, {})
        self.assertEqual(result['expected_brand'], '')
        settings['am_dealer_ids'] = True
        result, _ = analyze_row(row, columns, transport, settings, {})
        self.assertEqual(result['expected_brand'], 'Acura')
        result, _ = analyze_row(dict(row, brand='BMW'), columns, transport, settings, {})
        self.assertEqual(result['expected_brand'], 'BMW')
        self.assertEqual(result['brand_source'], 'input_brand')

    def test_selected_model_filter_is_rejected(self):
        html = SRP_HTML + '<select name="model"><option value="Integra" selected>Integra</option></select>'
        self.assertEqual(self.judge(html=html)[0], 'rejected')

    def test_zero_inventory_with_brand_heading_is_still_an_srp(self):
        html = '<title>New Acura Inventory</title><h1>New Acura Vehicles</h1><p>0 matching vehicles</p>'
        self.assertEqual(self.judge(html=html)[0], 'verified')

    def test_navigation_and_provider_aliases(self):
        page = parse_page(HOME_HTML, HOME)
        self.assertEqual(page.inventory_links[0]['source'], 'navigation')
        self.assertEqual(provider_paths('TeamVelocity'), provider_paths('Team Velocity'))

    def test_same_candidate_merges_sources(self):
        items = candidates(HOME, parse_page(HOME_HTML, HOME), HOME + 'new-vehicles/', 'Unknown', 'Dealer Inspire', 'Acura')
        self.assertEqual(items[0]['sources'], ['navigation', 'supplied', 'supplied_blueprint', 'generic'])

    def test_blocked_never_becomes_valid(self):
        transport = FakeTransport({HOME: Fetch(HOME, HOME, 403), HOME + 'new-vehicles/': Fetch(HOME + 'new-vehicles/', HOME + 'new-vehicles/', 403)})
        result, _ = analyze_row({'homepage_url': HOME, 'brand': 'Acura'},
                               {'homepage': 'homepage_url', 'brand': 'brand'}, transport,
                               {'row_timeout': 20, 'max_candidates': 10, 'am_dealer_ids': False}, {})
        self.assertEqual(result['recommended_new_srp_url'], '')
        self.assertEqual(result['srp_selection_status'], 'unverified_blocked')

    def test_cross_domain_homepage_is_adopted_without_identity_gate(self):
        new_home = 'https://newdealer.example/'
        transport = FakeTransport({HOME: Fetch(HOME, new_home, 200, HOME_HTML, [{'url': HOME, 'status': 301}]),
                                   new_home + 'new-vehicles/': Fetch(new_home + 'new-vehicles/', new_home + 'new-vehicles/', 200, SRP_HTML)})
        result, _ = analyze_row({'homepage_url': HOME, 'brand': 'Acura'},
                               {'homepage': 'homepage_url', 'brand': 'brand'}, transport,
                               {'row_timeout': 20, 'max_candidates': 10, 'am_dealer_ids': False}, {})
        self.assertEqual(result['recommended_homepage_url'], new_home)
        self.assertEqual(result['srp_selection_status'], 'verified')


class CsvRun(unittest.TestCase):
    def test_cli_preserves_cells_and_rejects_stale_resume(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                html = HOME_HTML if self.path == '/' else SRP_HTML
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(html.encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                source = Path(temp) / 'input.csv'
                out = Path(temp) / 'out'
                hp = f'http://127.0.0.1:{server.server_port}/'
                original = [{'dealer_id': 'AC0001', 'account_id': '000001', 'homepage_url': hp, 'new_srp_url': '', 'brand': 'Acura', 'note': ' café, "quoted"\nnext line'}]
                with source.open('w', newline='') as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(original[0]))
                    writer.writeheader()
                    writer.writerows(original)
                command = [sys.executable, str(SCRIPTS / 'check_urls.py'), '--input', str(source), '--out', str(out)]
                process = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(process.returncode, 0, process.stderr)
                with (out / 'results.csv').open(newline='') as handle:
                    result = list(csv.DictReader(handle))[0]
                self.assertEqual({key: result[key] for key in original[0]}, original[0])
                self.assertEqual(result['srp_selection_status'], 'verified')
                again = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(again.returncode, 0, again.stderr)
                self.assertIn('"resume_rows": 1', again.stdout)
                source.write_text(source.read_text().replace('café', 'changed'))
                stale = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(stale.returncode, 2)
                self.assertIn('prevent stale resume', stale.stderr)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
