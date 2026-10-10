"""URL and page judgments shared by supplied and discovered candidates."""
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
import tldextract

DATA = json.loads((Path(__file__).resolve().parents[1] / 'references/providers.json').read_text())
EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)
RANKS = {'navigation': 1, 'homepage_link': 2, 'supplied': 3, 'repair': 3,
         'learned': 3, 'detected_blueprint': 4, 'supplied_blueprint': 5, 'generic': 6}


def source_priority(sources):
    return min(RANKS[x] for x in sources), 'learned' not in sources


def normalize_url(value):
    value = (value or '').strip()
    if not value:
        return ''
    if '://' not in value:
        value = 'https://' + value
    p = urlsplit(value)
    if p.scheme.lower() not in ('http', 'https') or not p.hostname or p.username or p.password:
        return ''
    scheme, authority = p.scheme.lower(), p.netloc.lower()
    default_port = ':443' if scheme == 'https' else ':80'
    if authority.endswith(default_port):
        authority = authority[:-len(default_port)]
    return urlunsplit((scheme, authority, p.path or '/', p.query, ''))


def domain(url):
    return EXTRACT(url).top_domain_under_public_suffix or (urlsplit(url).hostname or '')


def site_key(url):
    return (urlsplit(url).hostname or '').lower().removeprefix('www.')


def provider_key(value):
    return re.sub(r'[^a-z0-9]', '', (value or '').lower()).replace('jazelauto', 'jazel')


def provider_paths(value, brand=''):
    key = provider_key(value)
    canonical_brand = next((name for name in DATA['brand_prefixes'].values()
                            if brand_key(name) == brand_key(brand)), brand)
    slug = re.sub(r'[^a-z0-9]+', '-', canonical_brand.lower()).strip('-')
    for name, paths in DATA['blueprints'].items():
        if provider_key(name) in key:
            return [path.replace('{brand}', slug) for path in paths
                    if '{brand}' not in path or slug]
    return []


def detect_provider(html):
    signatures = ' '.join(re.findall(r'<script\b[^>]*>.*?</script>|<(?:link|meta)\b[^>]*>', html, re.I | re.S)).lower()
    lower_html = html.lower()
    matches = []
    for provider in DATA['providers']:
        evidence = [token for token in provider['footprints'] if token in lower_html]
        if evidence:
            strong = any(token in signatures for token in evidence)
            matches.append({'provider': provider['name'], 'evidence': evidence,
                            'strength': 'asset' if strong else 'html'})
    # Finance/embed vendors are not primary website-platform evidence.
    primary = [x for x in matches if x['provider'] not in ('AutoFi', 'Dealer Masters')]
    strong = [x for x in primary if x['strength'] == 'asset']
    choices = strong or primary
    return (choices[0]['provider'] if len(choices) == 1 else 'Unknown'), matches


def brand_key(value):
    key = re.sub(r'[^a-z0-9]', '', (value or '').lower())
    return {'mercedes': 'mercedesbenz', 'chevy': 'chevrolet', 'vw': 'volkswagen'}.get(key, key)


def contains_brand(text, brand):
    key = brand_key(brand)
    aliases = {'mercedesbenz': ['mercedes benz', 'mercedes-benz', 'mercedes'],
               'chevrolet': ['chevrolet', 'chevy'], 'volkswagen': ['volkswagen', 'vw'],
               'landrover': ['land rover', 'land-rover']}.get(key, [brand.lower()])
    return bool(key) and any(re.search(r'(?<![a-z])' + re.escape(x) + r'(?![a-z])', text.lower()) for x in aliases)


def new_link(label, url):
    text = label.lower()
    p = urlsplit(url)
    return bool(re.search(r'\bnew\b.*\b(inventory|vehicles|cars|stock)\b|\b(shop|view|browse|search|all)\s+(all\s+)?new\b', text)
                or text.strip() == 'new'
                or re.search(r'/new(?:/|$)', p.path.lower())
                or re.search(r'new[-/]?(vehicles|inventory|cars)|searchnew|/new(?:/|$)|search=new|condition=new',
                             p.path.lower() + '?' + p.query.lower()))


def url_rejection(url):
    p = urlsplit(url)
    path = p.path.lower()
    tokens = set(re.split(r'[^a-z0-9]+', path))
    if any(x in path for x in ('service', 'appointment', 'maintenance', 'oil-change', 'auto-repair')):
        return 'Service/department page'
    if any(x in path for x in ('used-', 'used/', 'pre-owned', 'preowned', 'certified-pre', 'cpo')):
        return 'Used/CPO inventory'
    if any(x in path for x in ('special', 'offer', 'promotion', 'incentive', 'clearance')):
        return 'Specials/offers subset'
    if tokens & {'featured', 'demo', 'demos', 'loaner', 'loaners'}:
        return 'Featured/demo/loaner subset'
    if tokens & {'commercial', 'intransit', 'remaining'} or 'in-transit' in path:
        return 'Commercial/in-transit/remaining-year subset'
    if re.search(r'under[-_]?\$?\d|new[-_]?(?:suv|truck|sedan|coupe|convertible|crossover)s?[-_]inventory', path):
        return 'Price/body subset'
    segments = [x for x in path.split('/') if x]
    # Inventory child routes are eligible only for a brand or the listing document.
    # This excludes named models such as /new-vehicles/rdx/ even without a model query.
    if segments and segments[0] in ('new-vehicles', 'new-inventory') and len(segments) > 1:
        tail = segments[1:]
        brand_names = {brand_key(b) for b in DATA['brand_prefixes'].values()}
        if any(x not in ('index.htm', 'index.html', 'index', 'search.htm', 'all') and brand_key(x) not in brand_names for x in tail):
            return 'Inventory child model/subset route'
    if segments[:2] == ['inventory', 'new'] and len(segments) > 2:
        tail = segments[2:]
        brand_names = {brand_key(b) for b in DATA['brand_prefixes'].values()}
        if any(x not in ('index.htm', 'index.html', 'all') and brand_key(x) not in brand_names for x in tail):
            return 'Inventory child model/subset route'
    if segments and segments[0] in ('research', 'finance', 'about', 'about-us', 'contact', 'blog', 'news', 'video', 'video-gallery'):
        return 'Non-inventory content page'
    if tokens & {'ev', 'evs', 'electric', 'hybrid', 'hybrids', 'phev', 'bev'} or any(x in path for x in ('high-mpg', 'fuel-efficient')):
        return 'Fuel subset'
    if tokens & {'truck', 'trucks', 'suv', 'suvs', 'sedan', 'sedans', 'coupe', 'coupes', 'convertible', 'convertibles', 'crossover', 'crossovers'}:
        return 'Body subset'
    inventory_doc = re.fullmatch(r'new-(.+)-inventory\.html?', segments[-1]) if segments else None
    brand_names = {brand_key(b) for b in DATA['brand_prefixes'].values()}
    for brand in set(DATA['brand_prefixes'].values()):
        slug = re.sub(r'[^a-z0-9]+', '-', brand.lower()).strip('-')
        model_sale = re.fullmatch(r'new-' + re.escape(slug) + r'-(.+)-for-sale(?:-.+)?\.html?', segments[-1]) if segments else None
        if model_sale and model_sale[1] not in ('vehicle', 'vehicles', 'car', 'cars', 'inventory', 'all'):
            return 'Model/subset sale document'
    if inventory_doc and brand_key(inventory_doc[1]) not in brand_names | {'vehicle', 'vehicles', 'car', 'cars'}:
        return 'Model/subset inventory document'
    if len(segments) > 1 and segments[0] == 'new' and segments[-1].endswith(('.htm', '.html')):
        stem = segments[-1].rsplit('.', 1)[0]
        if stem not in ('index', 'all', 'search') and brand_key(stem) not in brand_names:
            return 'Individual model/marketing inventory document'
    if any(re.fullmatch(r'[a-z]{1,3}\d{1,4}[a-z]{0,3}', token) for token in tokens):
        return 'Model-code path subset'
    if tokens & {'model', 'models', 'trim'} or re.search(r'/new-(?:vehicles|inventory)/[^/]+', path):
        # Brand paths are considered below, rather than universally excluded here.
        if tokens & {'model', 'models', 'trim'}:
            return 'Model/trim page'
    if re.search(r'/vehicle(?:details|-details)/|/new/[^/]+/\d|/[A-HJ-NPR-Z0-9]{17}(?:/|$)', p.path, re.I):
        return 'Individual vehicle detail page'
    for key, val in parse_qsl(p.query, keep_blank_values=True):
        k = key.lower()
        v = val.lower()
        if k in ('make', 'brand') and '--' in val:
            components = [brand_key(x) for x in val.split('--') if x]
            if any(x not in brand_names for x in components):
                return 'Composite make/model subset selector'
        if k == 'filters':
            try:
                applied = json.loads(val).get('appliedFilters', {})
            except (ValueError, AttributeError):
                applied = {}
            if not isinstance(applied, dict):
                applied = {}
            for name, selection in applied.items():
                value = selection.get('value', selection) if isinstance(selection, dict) else selection
                selected_values = [key for key, active in value.items() if active] if isinstance(value, dict) else value
                if re.search(r'model|trim|fuel|body|year|price|mileage', name, re.I) and selected_values:
                    return 'Encoded JSON model/trim/year/fuel/body subset'
                if name.lower() in ('type', 'car_condition', 'condition') and re.search(r'\b(?:used|cpo|demo|loaner|ctp)\b', json.dumps(selected_values), re.I):
                    return 'Encoded JSON used/demo/loaner subset'
        if ('intransit' in k.replace('_', '').replace('-', '') and 'intransit' in v.replace('-', '').replace(' ', '')) or ('commercial' in k and v not in ('', 'false', '0', 'all')):
            return 'Commercial/in-transit subset selector'
        if any(x in k for x in ('model', 'trim', 'fueltype', 'fuel_type', 'powertrain', 'bodystyle', 'bodytype', 'year', 'price', 'mileage')) and v:
            return 'Model/trim/year/fuel/body subset'
        if k in ('body', 'body_style', 'transmission', 'drivetrain', 'doors') and v:
            return 'Body/transmission/drivetrain subset'
        if k in ('q', 'search', 'keyword', 'keywords', 'searchterm', 'search_term') and v.strip() and v.strip() not in ('new', 'all') and brand_key(v) not in {brand_key(b) for b in DATA['brand_prefixes'].values()}:
            return 'Keyword/model subset'
        if (k in ('condition', 'type', 'tp', 'search', 'compositetype') or re.search(r'\[(?:condition|type)\]', k)) and v in ('used', 'pre-owned', 'cpo', 'special', 'specials'):
            return 'Used/CPO/specials selector'
        if (k in ('type', 'condition', 'tp') or re.search(r'\[(?:type|condition)\]', k)) and v in ('demo', 'demos', 'loaner', 'loaners', 'ctp'):
            return 'Demo/loaner/courtesy subset selector'
    return ''


def heading_rejection(title, heading):
    if re.match(r'\s*(?:electric(?:\s*/\s*hybrid)?|hybrid|plug[- ]in hybrid|phev|bev)\b', heading, re.I):
        return 'Fuel subset heading'
    if re.search(r'\bunder\s*\$?\s*\d|\b(?:in[- ]transit|commercial|remaining)\b', heading, re.I):
        return 'Heading describes a price/commercial/in-transit/year subset'
    for brand in DATA['brand_prefixes'].values():
        # A named model followed by a body class, rather than a full-inventory heading.
        pattern = r'\bnew\s+' + re.escape(brand) + r'\s+([a-z0-9-]+)\s+(?:sedans|coupes|convertibles)\b'
        match = re.search(pattern, heading, re.I)
        if match and match.group(1).lower() not in ('luxury', 'passenger', 'all', 'new'):
            return 'Heading describes a model/body subset'
    return ''


def soft_error(soup, url):
    heading = ' '.join(x.get_text(' ', strip=True) for x in soup.find_all(['title', 'h1', 'h2']))
    if re.search(r'page (?:not found|cannot be found)|404\s*[-:]?\s*(?:error|not found)|error\s*404|this page (?:does not exist|is no longer available)', heading, re.I):
        return 'Soft 404'
    if soup.select('.error-page, #error-page, .page-not-found'):
        return 'Soft 404 container'
    if re.search(r'/(?:404|not-found|page-not-found)(?:/|$)|[?&](?:q|error)=404(?:&|$)', url, re.I):
        return 'Error destination'
    title = soup.title.get_text(' ', strip=True).lower() if soup.title else ''
    if re.search(r'just a moment|access denied|attention required|verify.*human|security check', title):
        return 'Bot challenge'
    if soup.select('#challenge-running, #cf-challenge-running, #px-captcha, form#challenge-form'):
        return 'Bot challenge'
    return ''


@dataclass
class Page:
    title: str
    heading: str
    text: str
    home_links: list = field(default_factory=list)
    inventory_links: list = field(default_factory=list)
    vehicle_makes: list = field(default_factory=list)
    selected_makes: list = field(default_factory=list)
    inventory_evidence: list = field(default_factory=list)
    error: str = ''
    html: str = ''
    selected_subsets: list = field(default_factory=list)
    canonical_url: str = ''
    make_options: list = field(default_factory=list)
    vehicle_conditions: list = field(default_factory=list)
    primary_heading_is_h1: bool = False
    primary_heading: str = ''


def parse_page(html, final_url):
    # Share expensive HTML parsing, but never share mutable judgments across dealers.
    return deepcopy(_parse_page(html, final_url))


@lru_cache(maxsize=16)
def _parse_page(html, final_url):
    soup = BeautifulSoup(html, 'html.parser')
    error = soft_error(soup, final_url)
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    canonical = soup.find('link', rel='canonical', href=True)
    canonical_url = normalize_url(urljoin(final_url, canonical['href'])) if canonical else ''
    primary = soup.find_all('h1') or soup.find_all('h2')
    headings = ' '.join(x.get_text(' ', strip=True) for x in primary)
    makes = []
    conditions = []
    new_collections = []

    def walk(value):
        if isinstance(value, dict):
            types = value.get('@type', [])
            types = [types] if isinstance(types, str) else types
            if not isinstance(types, list):
                types = []
            if ('CollectionPage' in types and normalize_url(value.get('url', '')) == normalize_url(final_url)
                    and re.search(r'\bnew\b.{0,50}\binventory\b', str(value.get('name', '')), re.I)):
                new_collections.append(value)
            if any(x in ('Vehicle', 'Car') for x in types) or ('Product' in types and value.get('vehicleIdentificationNumber')):
                b = value.get('brand') or value.get('manufacturer')
                if isinstance(b, dict):
                    b = b.get('name')
                if isinstance(b, str):
                    makes.append(b)
                vin = str(value.get('vehicleIdentificationNumber', ''))
                condition = value.get('itemCondition')
                if isinstance(condition, str) and re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', vin, re.I):
                    name = urlsplit(condition).path.rstrip('/').rsplit('/', 1)[-1].lower()
                    conditions.append({'newcondition': 'new', 'usedcondition': 'used', 'refurbishedcondition': 'used'}.get(name, 'unknown'))
            for x in value.values():
                walk(x)
        elif isinstance(value, list):
            for x in value:
                walk(x)

    for script in soup.find_all('script', type='application/ld+json'):
        try:
            walk(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            pass
    for card in soup.select('[data-make][data-vin]'):
        vin = str(card.get('data-vin', ''))
        make = str(card.get('data-make', '')).strip()
        if re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', vin, re.I) and make and '{{' not in make:
            makes.append(make)
            condition = card.get('data-vehicletype') or card.get('data-condition') or card.get('data-dotagging-item-condition')
            if condition:
                conditions.append(str(condition).lower())
    for card in soup.select('[data-vehicle]'):
        try:
            vehicle = json.loads(card['data-vehicle'])
        except (ValueError, TypeError):
            continue
        if not isinstance(vehicle, dict):
            continue
        vin = str(vehicle.get('vin', ''))
        if re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', vin, re.I) and vehicle.get('make'):
            makes.append(str(vehicle['make']))
            condition = str(vehicle.get('type') or vehicle.get('condition') or 'unknown').lower()
            conditions.append(condition)
    selected = []
    subsets = []
    make_options = []
    for select in soup.find_all('select'):
        label = str(select.get('name', '')) + ' ' + str(select.get('id', ''))
        if 'make' in label.lower() or 'brand' in label.lower():
            for option in select.find_all('option', selected=True):
                if str(option.get('value', '')).lower() not in ('', '*', 'all', 'any', '0'):
                    selected.append(option.get_text(' ', strip=True))
            form = select.find_parent('form')
            if form and str(form.get('method', 'get')).lower() == 'get' and select.get('name'):
                for option in select.find_all('option'):
                    value = str(option.get('value', '')).strip()
                    if value.lower() not in ('', '*', 'all', 'any', '0'):
                        make_options.append({'name': select['name'], 'value': value,
                                             'label': option.get_text(' ', strip=True),
                                             'action': urljoin(final_url, form.get('action') or final_url),
                                             'hidden': [(x['name'], x.get('value', '')) for x in form.find_all('input', type='hidden', attrs={'name': True})]})
        if re.search(r'model|trim|fuel|year|body', label, re.I):
            for option in select.find_all('option', selected=True):
                value = str(option.get('value', '')).strip().lower()
                if value and value not in ('all', 'any', '0', '*'):
                    subsets.append(label + '=' + value)
        if re.search(r'condition|compositetype', label, re.I):
            for option in select.find_all('option', selected=True):
                if str(option.get('value', '')).lower() in ('used', 'pre-owned', 'cpo'):
                    subsets.append('Used/CPO condition selector')
    links = []
    home_links = []
    for link in soup.find_all('a', href=True):
        href = link['href'].strip()
        if href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue
        url = normalize_url(urljoin(final_url, href))
        if not url:
            continue
        text = link.get_text(' ', strip=True) or link.get('aria-label', '')
        if text.lower().strip() in ('home', 'homepage', 'back to home') or link.find('img', alt=re.compile('logo', re.I)):
            home_links.append(url)
        if new_link(text, url):
            nav = any(parent.name in ('nav', 'header') or re.search('nav|menu|header',
                      ' '.join(parent.get('class', [])) + ' ' + parent.get('id', ''), re.I)
                      for parent in link.parents if getattr(parent, 'attrs', None) is not None)
            links.append({'url': url, 'source': 'navigation' if nav else 'homepage_link', 'label': text})
    controls = len(soup.select('select, input[type=checkbox], [class*=inventory-filter], [class*=inventoryFilter]'))
    cards = len(soup.select('[class*=vehicle-card], [class*=vehicleCard], [class*=vehicle-listing], [data-vin], [data-vehicle-id]'))
    for el in soup.find_all(['script', 'style', 'noscript']):
        el.decompose()
    text = soup.get_text(' ', strip=True)
    signals = []
    if makes:
        signals.append('Vehicle structured data')
    if new_collections:
        signals.append('New-inventory collection structured data')
    if cards:
        signals.append(f'{cards} vehicle/card elements')
    if re.search(r'\bvin\s*[:#]?\s*[A-HJ-NPR-Z0-9]{17}\b', text, re.I):
        signals.append('Vehicle VIN listing')
    if controls >= 2:
        signals.append('Inventory/filter controls')
    if re.search(r'\b(?:0|zero|no)\s+(?:matching\s+|new\s+)?(?:vehicles|results|cars)\b', text, re.I):
        signals.append('Explicit zero inventory/results state')
    return Page(title, headings, text, list(dict.fromkeys(home_links)), links,
                makes, selected, signals, error, html, subsets, canonical_url, make_options, conditions, bool(soup.find('h1')),
                primary[0].get_text(' ', strip=True) if primary else '')


def is_homepage(url, page):
    if not normalize_url(url):
        return False
    p = urlsplit(url)
    if page.error:
        return False
    if url_rejection(url):
        return False
    if re.search(r'(?:^|/)(?:about(?:-us)?|contact(?:-us)?|(?:[a-z0-9]+-)?premier-dealer-program)(?:/|$)', p.path, re.I):
        return False
    if new_link('', url) or page.primary_heading_is_h1 and re.match(r'\s*(?:schedule\s+service|service\s+(?:center|department)|book\s+(?:service|appointment))\b', page.heading, re.I):
        return False
    if page.inventory_evidence and (re.search(r'^/(?:inventory|vehicles|search)(?:/|$)', p.path, re.I)
                                    or page.primary_heading_is_h1 and re.match(r'\s*(?:new|used|pre[- ]owned)\b.{0,100}\b(?:inventory|vehicles|cars|sale)\b', page.heading, re.I)):
        return False
    return len(page.text) >= 100 and bool(page.title) and not re.search(r'domain (?:for sale|parked)|buy this domain', page.text, re.I)


def make_selector(url):
    values = []
    path = urlsplit(url).path.lower().strip('/')
    for brand in set(DATA['brand_prefixes'].values()):
        slug = re.sub(r'[^a-z0-9]+', '-', brand.lower()).strip('-')
        if path in (f'new-vehicles/{slug}', f'new-inventory/{slug}', f'inventory/new/{slug}', f'search/new-{slug}'):
            values.append(brand)
    for k, v in parse_qsl(urlsplit(url).query):
        if re.search(r'(?:^|\[)(make|brand)(?:$|\[|\])', k, re.I):
            values.extend(re.split(r'[,|]', v))
        elif k.lower() == 'q' and brand_key(v) in {brand_key(b) for b in DATA['brand_prefixes'].values()}:
            values.append(v)
    return values


def judge_srp(url, page, brand, multi_brand, home_url):
    if not normalize_url(url):
        return 'rejected', 'Invalid inventory destination URL', []
    reason = url_rejection(url) or page.error or heading_rejection(page.title, page.heading)
    if reason:
        return 'rejected', reason, []
    if page.selected_subsets:
        return 'rejected', 'Active model/trim/year/fuel/body subset: ' + ', '.join(page.selected_subsets), []
    for prefix in ('/new-vehicles/', '/new-inventory/', '/inventory/new/'):
        path = urlsplit(url).path.lower()
        idx = path.find(prefix)
        if idx >= 0:
            tail = path[idx + len(prefix):].strip('/')
            allowed = ('index.htm', 'index.html', 'all')
            if tail and tail not in allowed and brand_key(tail) != brand_key(brand):
                return 'rejected', 'Inventory subpage is not the requested brand root', []
    p, h = urlsplit(url), urlsplit(home_url)
    if p.netloc == h.netloc and p.path.rstrip('/') == h.path.rstrip('/') and p.query == h.query:
        return 'rejected', 'Homepage destination', []
    new_heading = bool(re.search(r'\bnew\b.{0,100}\b(inventory|vehicles|cars|stock|sale)\b', page.title + ' ' + page.heading, re.I))
    if re.search(r'\bnew\s*(?:and|&|/)\s*used\b', page.primary_heading or page.heading, re.I):
        return 'rejected', 'Combined new and used inventory heading', []
    if any(x in ('used', 'pre-owned', 'cpo') for x in page.vehicle_conditions):
        return 'rejected', 'Rendered vehicle data includes used/CPO inventory', page.inventory_evidence
    new_listings = (bool(page.vehicle_conditions) and set(page.vehicle_conditions) == {'new'}
                    and ('Inventory/filter controls' in page.inventory_evidence
                         or ('New-inventory collection structured data' in page.inventory_evidence and len(page.vehicle_conditions) >= 2))
                    and any('vehicle/card elements' in x or x == 'Vehicle VIN listing' for x in page.inventory_evidence)
                    and (new_link('', url) or bool(re.search(r'\bnew\b', page.title + ' ' + page.heading, re.I))))
    if not (new_heading or new_listings) or not page.inventory_evidence:
        return 'unverified_content', 'Full new-inventory interface not established by fetched HTML', page.inventory_evidence

    def all_new_fallback(reason):
        if not make_selector(url) and not page.selected_makes:
            return 'verified', 'Verified all-new inventory; ' + reason, page.inventory_evidence + ['scope=all_new']
        return 'scope_review', reason, page.inventory_evidence

    if not brand:
        return all_new_fallback('brand is unknown')
    selected = make_selector(url)
    named_selectors = [x for x in selected if not x.isdigit()]
    if named_selectors and any(brand_key(x) != brand_key(brand) for x in named_selectors):
        return 'rejected', 'URL selects another brand', page.inventory_evidence
    if page.selected_makes and any(brand_key(x) != brand_key(brand) for x in page.selected_makes):
        return 'rejected', 'Active make control selects another brand', page.inventory_evidence
    known_brands = set(DATA['brand_prefixes'].values())
    heading_brands = {brand_key(x) for x in known_brands if contains_brand(page.heading, x)}
    listed_brands = {brand_key(x) for x in page.vehicle_makes}
    evidence = list(page.inventory_evidence)
    scoped_heading = heading_brands == {brand_key(brand)}
    scoped_list = listed_brands == {brand_key(brand)}
    active_filter = bool(page.selected_makes) and all(brand_key(x) == brand_key(brand) for x in page.selected_makes)
    if listed_brands and brand_key(brand) not in listed_brands:
        return all_new_fallback('expected brand is not established in the listings')
    if len(listed_brands) > 1:
        return all_new_fallback('listings include multiple brands')
    if multi_brand or len(heading_brands) > 1 or len(listed_brands) > 1:
        if not (active_filter or scoped_heading or (selected and scoped_list)):
            return all_new_fallback('specific dealer/brand scope is unconfirmed on the shared site')
    elif not (contains_brand(page.title + ' ' + page.heading, brand) or scoped_list or active_filter):
        return all_new_fallback('requested brand is not established on the inventory page')
    if selected and not (active_filter or scoped_heading or scoped_list):
        return 'scope_review', 'Make query parameter has no corroborating page evidence', evidence
    evidence.append('Brand-specific heading/control/listings' if scoped_heading or active_filter or scoped_list else 'Single-brand inventory heading/title')
    return 'verified', 'Verified new inventory for ' + brand, evidence


def brand_form_candidates(page, url, brand):
    result = []
    for option in page.make_options:
        if brand_key(option['label']) != brand_key(brand) or domain(option['action']) != domain(url):
            continue
        p = urlsplit(option['action'])
        pairs = list(parse_qsl(p.query)) + option['hidden']
        pairs = [(k, v) for k, v in pairs if k != option['name']]
        pairs.append((option['name'], option['value']))
        result.append(urlunsplit((p.scheme, p.netloc, p.path, urlencode(pairs), '')))
    return list(dict.fromkeys(result))


def select_verified(entries, multi_brand, home_url='', brand='', provider=''):
    eligible = [x for x in entries if x['status'] == 'verified']
    if not eligible:
        return None, False
    targets = {x.get('canonical_url') for x in eligible if x.get('canonical_url')}
    blueprints = [normalize_url(urljoin(home_url, path)) for path in provider_paths(provider, brand)] if home_url else []

    def preference(entry):
        url = normalize_url(entry['fetch']['final_url'])
        query = parse_qsl(urlsplit(url).query)
        optional = sum(k.lower() not in ('condition', 'search', 'type', 'tp', 'compositetype', 'locale', 'language', 'dealer', 'dealerid', 'location')
                       and not (multi_brand and (k.lower() in ('make', 'brand') or k.lower() == 'q' and brand_key(v) in {brand_key(b) for b in DATA['brand_prefixes'].values()})) for k, v in query)
        explicit_brand = bool(brand) and any(brand_key(value) == brand_key(brand) for value in make_selector(url))
        blueprint_rank = blueprints.index(url) if url in blueprints else len(blueprints)
        return (entry.get('inventory_scope', 'brand_specific') == 'all_new', entry['rank'], 'learned' not in entry.get('sources', []),
                bool(home_url) and site_key(url) != site_key(home_url), url not in targets,
                not explicit_brand if entry.get('inventory_scope') == 'brand_specific' else bool(make_selector(url)), blueprint_rank,
                entry['fetch']['ssl_bypassed'], len(entry['fetch']['redirects']), optional)

    eligible.sort(key=preference)
    chosen = eligible[0]
    distinct = {normalize_url(x['fetch']['final_url']) for x in eligible if preference(x) == preference(chosen)}
    return chosen, len(distinct) > 1


def select_unverified(entries, home_url, brand='', provider=''):
    pending = [x for x in entries if (x['status'].startswith('unverified') or x['status'] == 'scope_review')
               and not url_rejection(x.get('fetch', {}).get('final_url') or x['url'])]
    blueprints = [normalize_url(urljoin(home_url, path)) for path in provider_paths(provider, brand)]

    def preference(entry):
        url = normalize_url(entry.get('fetch', {}).get('final_url') or entry['url'])
        return (*source_priority(entry['sources']), site_key(url) != site_key(home_url),
                bool(brand) and not contains_brand(url, brand),
                blueprints.index(url) if url in blueprints else len(blueprints), len(url))

    return min(pending, key=preference) if pending else None


def candidates(home_url, page, supplied, detected_provider, csv_provider, brand, learned=()):
    items = {}

    def add(url, source, label=''):
        url = normalize_url(url)
        if not url:
            return
        item = items.setdefault(url, {'url': url, 'sources': [], 'labels': []})
        if source not in item['sources']:
            item['sources'].append(source)
        if label and label not in item['labels']:
            item['labels'].append(label)

    if page:
        for item in page.inventory_links:
            add(item['url'], item['source'], item['label'])
    if supplied:
        # Repair only a literal homepage prefix followed by a known inventory suffix.
        hp = home_url.rstrip('/')
        for path in DATA['generic_paths']:
            suffix = path.lstrip('/')
            if supplied.lower() == (hp + suffix).lower():
                add(urljoin(home_url, path), 'repair')
        add(supplied, 'supplied')
    for hint in learned:
        add(hint['url'], 'learned', 'Previously observed route; current HTTP verification required')
    for provider, source in ((detected_provider, 'detected_blueprint'), (csv_provider, 'supplied_blueprint')):
        for path in provider_paths(provider, brand):
            add(urljoin(home_url, path), source)
    for path in DATA['generic_paths']:
        add(urljoin(home_url, path), 'generic')

    def rank(item):
        label = ' '.join(item['labels'])
        other = any(contains_brand(label, x) and brand_key(x) != brand_key(brand) for x in DATA['brand_prefixes'].values())
        return (*source_priority(item['sources']), other,
                not contains_brand(label + ' ' + item['url'], brand) if brand else False,
                bool(url_rejection(item['url'])), len(item['url']))
    return sorted(items.values(), key=rank)
