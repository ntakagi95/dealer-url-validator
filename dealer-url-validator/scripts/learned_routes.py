"""Previously observed routes are HTTP candidate hints, never verification."""
import json
from datetime import datetime, timezone
from pathlib import Path
from url_rules import brand_key, normalize_url, site_key, url_rejection


def load_routes(path):
    return json.loads(Path(path).read_text()).get('sites', {}) if path and Path(path).exists() else {}


def route_hints(routes, home, brand):
    hints = []
    for item in routes.get(site_key(home), []):
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(item['observed_at'])).total_seconds()
        if not 0 <= age <= 30 * 86400:
            continue
        if brand_key(item['expected_brand']) != brand_key(brand) and item['inventory_scope'] != 'all_new':
            continue
        url = normalize_url(item['srp_url'])
        if url and not url_rejection(url):
            hints.append({'url': url, 'sources': ['learned'], 'labels': ['Previously observed inventory route; live verification required']})
    return hints
