"""Reuse explicit human confirmation without inventing HTTP/browser evidence."""
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from url_rules import brand_key, normalize_url, url_rejection

DEFAULT_PATH = Path(__file__).resolve().parents[1] / 'references/manual-reviews.json'


def load_reviews(path=DEFAULT_PATH):
    return json.loads(Path(path).read_text()).get('reviews', []) if path and Path(path).exists() else []


def matching_review(reviews, dealer_id, homepage, brand=''):
    for item in reversed(reviews):
        if (dealer_id and item['dealer_id'] == dealer_id
                and normalize_url(item['source_homepage_url']) == normalize_url(homepage)
                and (not brand or brand_key(item.get('expected_brand', '')) == brand_key(brand))):
            return item
    return None


def recent(item, days):
    if not item:
        return False
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(item['confirmed_at'])).total_seconds()
    return 0 <= age <= days * 86400


def homepage_hint(item):
    return normalize_url(item.get('homepage_hint_url', '')) if recent(item, 30) else ''


def route_hint(item):
    url = normalize_url(item.get('srp_url', '')) if recent(item, 30) else ''
    if url and not url_rejection(url):
        return [{'url': url, 'sources': ['learned'],
                 'labels': ['Exact user-confirmed route; live verification required after 24 hours']}]
    return []


def apply_confirmation(result, evidence, item):
    url = normalize_url(item.get('srp_url', '')) if recent(item, 1) else ''
    if (not url or item.get('confirmation') != 'opened_full_new_inventory'
            or item.get('inventory_scope') != 'all_new' or url_rejection(url)
            or urlsplit(url).port is not None):
        return False
    result.update(recommended_new_srp_url=url, srp_selection_status='verified',
                  srp_inventory_scope='all_new', srp_selection_source='manual_review',
                  srp_selection_reason='User opened and confirmed full new-inventory page at ' + item['confirmed_at']
                                       + '; specific brand/location scope not independently confirmed',
                  best_unverified_srp_url='',
                  review_reason=result.get('homepage_selection_reason', '')
                                if result.get('homepage_selection_status') != 'verified' else '')
    evidence['manual_srp_confirmation'] = dict(item)
    return True
