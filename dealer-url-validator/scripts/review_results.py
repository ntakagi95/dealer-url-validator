#!/usr/bin/env python3
"""Re-rank captured findings with current exclusions, scope policy, and fresh browser evidence."""
import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from check_urls import RESULT_FIELDS, atomic_csv, utc_now
from transport import Fetch
from manual_reviews import DEFAULT_PATH, apply_confirmation, load_reviews, matching_review, route_hint
from url_rules import (RANKS, brand_key, domain, heading_rejection, is_homepage, judge_srp,
                       make_selector, parse_page, select_unverified, select_verified, site_key, url_rejection)


def browser_record(directory, url):
    if not directory or not url:
        return None
    path = directory / (hashlib.sha256(url.encode()).hexdigest() + '.json')
    if not path.exists():
        return None
    record = json.loads(path.read_text())
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(record['captured_at'])).total_seconds()
    if record.get('requested_url') == url and record.get('via') == 'browser' and 0 <= age <= 86400 and record.get('status') == 200 and not record.get('error') and not record.get('ssl_bypassed'):
        return record
    return None


def original_manifest(manifest):
    while 'identity' not in manifest and 'source_manifest' in manifest:
        manifest = manifest['source_manifest']
    return manifest


def run(args):
    source, out = Path(args.run_dir).resolve(), Path(args.out).resolve()
    if source == out or out.exists():
        raise ValueError('Use a new review output folder; preserve the original run')
    with (source / 'results.csv').open(newline='', encoding='utf-8-sig') as handle:
        rows = list(csv.DictReader(handle))
    records = [json.loads(x) for x in (source / 'candidates.jsonl').read_text().splitlines()]
    manifest = json.loads((source / 'manifest.json').read_text())
    columns = original_manifest(manifest)['identity']['settings']['columns']
    hp_col, id_col = columns['homepage'], columns.get('dealer_id') or 'dealer_id'
    if len(rows) != len(records):
        raise ValueError('Run results and candidate evidence counts differ')
    fields = list(records[0]['input']) if records else []
    brands = defaultdict(set)
    same_brand = Counter()
    for row, record in zip(rows, records):
        key = site_key(record['input'][hp_col])
        brands[key].add(brand_key(row['expected_brand']))
        same_brand[(key, brand_key(row['expected_brand']))] += 1
    browser_dir = Path(args.browser_evidence).resolve() if args.browser_evidence else None
    coverage = {}
    if args.browser_coverage:
        coverage = {x['index']: x for x in map(json.loads, Path(args.browser_coverage).read_text().splitlines())}
        browser_manifest = Path(args.browser_coverage).parent / 'manifest.json'
        if browser_manifest.exists():
            source_hash = json.loads(browser_manifest.read_text()).get('source_sha256')
            actual_hash = hashlib.sha256(b''.join((source / name).read_bytes() for name in ('results.csv', 'candidates.jsonl', 'manifest.json'))).hexdigest()
            if source_hash and source_hash != actual_hash:
                raise ValueError('Browser coverage source does not match this run')
    changes = []
    manual_path = Path(getattr(args, 'manual_reviews', None) or DEFAULT_PATH)
    manual_reviews = load_reviews(manual_path)
    for index, (row, record) in enumerate(zip(rows, records)):
        before = {key: row.get(key, '') for key in RESULT_FIELDS}
        home = row['resolved_homepage_url']
        investigation = coverage.get(index, {})
        if investigation and investigation.get('dealer_id') != row.get(id_col, ''):
            raise ValueError('Browser coverage dealer ID does not match its source row')
        observations = list(record.get('homepage_observations', []))
        if row['homepage_selection_status'] == 'verified' and observations and observations[-1].get('ssl_bypassed'):
            row.update(recommended_homepage_url='', homepage_selection_status='unverified_transport',
                       homepage_selection_reason='Historical capture bypassed TLS verification; fresh secure verification required')
        if investigation.get('homepage_url'):
            observations.append({'requested_url': investigation['homepage_url']})
        for observation in observations:
            browser = browser_record(browser_dir, observation.get('requested_url')) or browser_record(browser_dir, observation.get('final_url'))
            if browser:
                page = parse_page(browser['text'], browser['final_url'])
                if is_homepage(browser['final_url'], page):
                    row.update(recommended_homepage_url=browser['final_url'], resolved_homepage_url=browser['final_url'],
                               homepage_selection_status='verified', homepage_selection_reason='Verified rendered homepage at final redirect destination')
                    row['homepage_domain_changed'] = str(domain(record['input'][hp_col]) != domain(browser['final_url']))
                    home = browser['final_url']
                    record['browser_homepage_evidence'] = Fetch(**{k: v for k, v in browser.items() if k in Fetch.__dataclass_fields__}).evidence()
                    break
                elif row['homepage_selection_status'] != 'verified':
                    row.update(resolved_homepage_url=browser['final_url'],
                               homepage_selection_status='unverified_blocked' if page.error == 'Bot challenge' else 'unverified_content',
                               homepage_selection_reason=page.error or 'Rendered destination is not established as a homepage')
        # Unvisited official links remain predictions; only fetched evidence can verify them.
        for item in investigation.get('discovered_candidates', []):
            matching = next((x for x in record.get('candidates', []) if x['url'] == item['url']), None)
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row['checked_at'])).total_seconds()
            if item['url'] not in investigation.get('visited_urls', []) and not (matching and matching['status'] == 'verified' and 0 <= age <= 86400):
                if not matching and not url_rejection(item['url']):
                    record.setdefault('candidates', []).append(dict(item, rank=min(RANKS[s] for s in item['sources']),
                        status='unverified_content', reason='Observed official inventory link; not fetched within this investigation budget', evidence=[], fetch={}))
                continue
            if matching:
                matching['sources'] = list(dict.fromkeys(matching['sources'] + item['sources']))
                matching['rank'] = min(RANKS[s] for s in matching['sources'])
            elif browser_record(browser_dir, item['url']):
                record.setdefault('candidates', []).append(dict(item, rank=min(RANKS[s] for s in item['sources']),
                    status='unverified_content', reason='New rendered navigation discovery', evidence=[], fetch={}))
        key = site_key(record['input'][hp_col])
        multi = len(brands[key] - {''}) > 1
        for candidate in record.get('candidates', []):
            url = candidate.get('fetch', {}).get('final_url') or candidate['url']
            exclusion = url_rejection(candidate['url']) or url_rejection(url) or heading_rejection(candidate.get('title', ''), candidate.get('heading', ''))
            named = [x for x in make_selector(url) if not x.isdigit()]
            if named and row['expected_brand'] and any(brand_key(x) != brand_key(row['expected_brand']) for x in named):
                exclusion = 'URL explicitly selects another brand'
            if exclusion:
                candidate.update(status='rejected', reason=exclusion)
                continue
            browser = browser_record(browser_dir, candidate['url']) or browser_record(browser_dir, url)
            if browser:
                page = parse_page(browser['text'], browser['final_url'])
                if page.error == 'Bot challenge':
                    continue
                status, reason, signals = judge_srp(browser['final_url'], page, row['expected_brand'], multi, home)
                if status == 'verified' and same_brand[(key, brand_key(row['expected_brand']))] > 1:
                    if not make_selector(browser['final_url']) and not page.selected_makes:
                        reason = 'Verified all-new inventory; individual dealer/location scope is unconfirmed'
                        signals.append('scope=all_new')
                    else:
                        status, reason = 'scope_review', 'Brand-filtered page lacks individual dealer/location context'
                candidate.update(status=status, reason=reason, evidence=signals, title=page.title, heading=page.heading,
                                 canonical_url=page.canonical_url,
                                 inventory_scope='all_new' if 'scope=all_new' in signals else 'brand_specific',
                                 fetch=Fetch(**{k: v for k, v in browser.items() if k in Fetch.__dataclass_fields__}).evidence())
            elif candidate.get('fetch', {}).get('ssl_bypassed'):
                candidate.update(status='unverified_transport', reason='Historical capture bypassed TLS verification; fresh secure verification required')
            elif candidate['status'] == 'scope_review' and not make_selector(url) and ('Shared/multi-brand' in candidate['reason'] or 'Requested brand is not established' in candidate['reason'] or 'location/dealer metadata is required' in candidate['reason']):
                # The original judgment had already established new-inventory purpose;
                # the user's updated policy permits an unfiltered site-wide fallback.
                candidate.update(status='verified', inventory_scope='all_new',
                                 reason='Verified all-new inventory; specific dealer/brand scope remains unconfirmed')
                candidate['evidence'].append('scope=all_new')
            elif candidate['status'] == 'verified':
                candidate['inventory_scope'] = candidate.get('inventory_scope') or 'brand_specific'
            if candidate['status'] == 'verified' and candidate.get('inventory_scope') == 'all_new' and make_selector(url):
                candidate.update(status='scope_review', reason='Brand-filtered route cannot establish an unfiltered all-new fallback or individual dealer/location scope')
        provider = row.get('detected_website_provider', '')
        if not provider or provider == 'Unknown':
            provider = record['input'].get(columns.get('provider'), '')
        chosen, ambiguous = select_verified(record.get('candidates', []), multi, home, row['expected_brand'], provider)
        row.update(recommended_new_srp_url='', srp_inventory_scope='', srp_selection_source='', srp_selection_reason='')
        if chosen and not ambiguous:
            row.update(recommended_new_srp_url=chosen['fetch']['final_url'], srp_selection_status='verified',
                       srp_inventory_scope=chosen.get('inventory_scope', 'brand_specific'),
                       srp_selection_source=min(chosen['sources'], key=RANKS.get),
                       srp_selection_reason=chosen['reason'] + f'; strongest supporting source tier {chosen["rank"]}',
                       best_unverified_srp_url='', review_reason='')
        elif chosen:
            row.update(srp_selection_status='ambiguous', best_unverified_srp_url=chosen['fetch']['final_url'],
                       review_reason='Multiple distinct eligible destinations have equal scope/source support')
        else:
            best = select_unverified(record.get('candidates', []), home, row['expected_brand'], provider)
            if best:
                row.update(srp_selection_status=best['status'], best_unverified_srp_url=best.get('fetch', {}).get('final_url') or best['url'], review_reason=best['reason'])
            else:
                row.update(srp_selection_status='not_found', best_unverified_srp_url='', review_reason='No eligible candidate in the captured evidence; further investigation may be needed')
        if row['homepage_selection_status'] != 'verified':
            row['review_reason'] = '; '.join(filter(None, [row['review_reason'], row['homepage_selection_reason']]))
        manual = matching_review(manual_reviews, row.get(id_col, ''), record['input'][hp_col], row['expected_brand'])
        record.pop('manual_srp_confirmation', None)
        if not apply_confirmation(row, record, manual) and before.get('srp_selection_source') == 'manual_review' and row['srp_selection_status'] != 'verified':
            hints = route_hint(manual)
            if hints:
                row.update(best_unverified_srp_url=hints[0]['url'], srp_selection_status='unverified_content',
                           review_reason='Human confirmation expired; exact route requires fresh verification; ' + row['review_reason'])
        after = {key: row.get(key, '') for key in RESULT_FIELDS}
        if before != after:
            row['checked_at'] = utc_now()
            after['checked_at'] = row['checked_at']
            changes.append({'index': index, 'before': before, 'after': after})
    out.mkdir(parents=True)
    atomic_csv(out / 'results.csv', fields + RESULT_FIELDS, rows)
    review = [x for x in rows if x['srp_selection_status'] != 'verified' or x['homepage_selection_status'] != 'verified']
    atomic_csv(out / 'review.csv', fields + RESULT_FIELDS, review)
    (out / 'candidates.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in records))
    (out / 'review-decisions.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in changes))
    scripts = Path(__file__).resolve().parent
    rules_hash = hashlib.sha256(b''.join(x.read_bytes() for x in sorted(scripts.glob('*.py'))) + (scripts.parent / 'references/providers.json').read_bytes()).hexdigest()
    summary = {'finished_at': utc_now(), 'rows': len(rows), 'review_rows': len(review), 'reviewed_from': str(source),
               'homepage_states': dict(Counter(x['homepage_selection_status'] for x in rows)),
               'srp_states': dict(Counter(x['srp_selection_status'] for x in rows)),
               'srp_scopes': dict(Counter(x['srp_inventory_scope'] for x in rows if x['srp_selection_status'] == 'verified')),
               'records_with_updated_decisions': len(changes), 'rules_sha256': rules_hash, 'run_dir': str(out)}
    (out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    (out / 'manifest.json').write_text(json.dumps({'source_run': str(source), 'source_manifest': manifest,
                                                   'review_rules_sha256': rules_hash, 'browser_evidence': str(browser_dir),
                                                   'manual_reviews_sha256': hashlib.sha256(manual_path.read_bytes()).hexdigest() if manual_path.exists() else None}, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--browser-evidence')
    parser.add_argument('--manual-reviews', help='Explicit user confirmations; reused for 24 hours')
    parser.add_argument('--browser-coverage', help='Per-record browser_batch.py coverage journal')
    args = parser.parse_args()
    try:
        run(args)
    except (OSError, ValueError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
