#!/usr/bin/env python3
"""Validate a CSV or a single dealership and rank verified inventory destinations."""
import argparse
import csv
import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from transport import Transport
from learned_routes import load_routes, route_hints
from manual_reviews import DEFAULT_PATH, apply_confirmation, homepage_hint, load_reviews, matching_review, route_hint
from url_rules import (DATA, RANKS, brand_form_candidates, brand_key, candidates, detect_provider,
                       domain, is_homepage, judge_srp, make_selector, normalize_url, parse_page, select_unverified, select_verified, site_key, source_priority, url_rejection)

RESULT_FIELDS = ['expected_brand', 'brand_source', 'resolved_homepage_url',
                 'recommended_homepage_url', 'homepage_selection_status', 'homepage_selection_reason',
                 'homepage_domain_changed', 'recommended_new_srp_url', 'srp_selection_status',
                 'srp_inventory_scope', 'srp_selection_source', 'srp_selection_reason', 'best_unverified_srp_url',
                 'review_reason', 'detected_website_provider', 'checked_at']
ALIASES = {
    'homepage': ['homepage_url', 'homepage url', 'website_url', 'website url', 'dealer website', 'url'],
    'srp': ['new_srp_url', 'new srp url', 'new_srp', 'srp_url', 'srp url'],
    'provider': ['website_provider', 'website provider', 'provider', 'oem_provider'],
    'brand': ['brand', 'dealer_brand', 'make', 'oem'],
    'dealer_id': ['dealer_id', 'dealer id'],
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def homepage_hint_key(dealer_id, homepage):
    return json.dumps([dealer_id, normalize_url(homepage)])


def load_homepage_hints(path):
    hints = {}
    if not path:
        return hints
    with Path(path).open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        if not {'homepage_url', 'resolved_homepage_url'}.issubset(reader.fieldnames or []):
            raise ValueError('Resolved-homepage CSV needs homepage_url and resolved_homepage_url columns')
        for row in reader:
            resolved = normalize_url(row['resolved_homepage_url'])
            if not resolved:
                continue
            key = homepage_hint_key(row.get('dealer_id', ''), row['homepage_url'])
            if key in hints and hints[key] != resolved:
                raise ValueError('Conflicting resolved homepages for the same dealer and source homepage')
            hints[key] = resolved
    return hints


def blocked(fetch, page=None):
    return fetch.status in (403, 429) or (page is not None and page.error == 'Bot challenge')


def resolve_home(url, transport, deadline):
    observations = []
    if not url:
        return '', None, 'invalid', 'Homepage is empty or malformed', observations
    first = transport.fetch(url, deadline)
    observations.append(first.evidence())
    resolved = normalize_url(first.final_url or url)
    page = parse_page(first.text, resolved) if first.status == 200 else None
    if blocked(first, page):
        return resolved, page, 'unverified_blocked', 'Homepage is blocked; browser investigation required', observations
    if first.status == 200 and page and is_homepage(resolved, page):
        return resolved, page, 'verified', 'Usable homepage at final redirect destination', observations
    if first.status == 200 or first.status in (404, 410):
        roots = (page.home_links if page else []) + [urljoin(resolved, '/')]
        seen = {url}
        for candidate in roots[:5]:
            if candidate in seen or domain(candidate) != domain(resolved):
                continue
            seen.add(candidate)
            result = transport.fetch(candidate, deadline)
            observations.append(result.evidence())
            result = replace(result, final_url=normalize_url(result.final_url))
            candidate_page = parse_page(result.text, result.final_url) if result.status == 200 else None
            if candidate_page and is_homepage(result.final_url, candidate_page):
                return result.final_url, candidate_page, 'verified', 'Verified home link/root replaces supplied department, inventory, or error page', observations
    reason = first.error or f'Homepage HTTP {first.status}; usable homepage content not established'
    return resolved, page, 'unverified_content', reason, observations


def analyze_row(row, col_map, transport, settings, shared):
    deadline = time.monotonic() + settings['row_timeout']
    hp_input = row.get(col_map['homepage'], '')
    hp = normalize_url(hp_input)
    srp_input = row.get(col_map.get('srp'), '')
    supplied_srp = normalize_url(srp_input)
    provider = row.get(col_map.get('provider'), '')
    explicit_brand = row.get(col_map.get('brand'), '').strip() or settings.get('brand', '')
    dealer_id = row.get(col_map.get('dealer_id'), '')
    brand = explicit_brand
    brand_source = 'input_brand' if brand else ''
    if not brand and settings['am_dealer_ids']:
        brand = DATA['brand_prefixes'].get(dealer_id[:2].upper(), '')
        brand_source = 'am_dealer_id' if brand else ''
    result = {key: '' for key in RESULT_FIELDS}
    result.update(expected_brand=brand, brand_source=brand_source, checked_at=utc_now())
    manual = matching_review(settings.get('_manual_reviews', []), dealer_id, hp, brand)
    start_home = homepage_hint(manual) or settings.get('_resolved_homepages', {}).get(homepage_hint_key(dealer_id, hp), hp)
    home, page, hp_status, hp_reason, observations = resolve_home(start_home, transport, deadline)
    result.update(resolved_homepage_url=home,
                  recommended_homepage_url=home if hp_status == 'verified' else '',
                  homepage_selection_status=hp_status, homepage_selection_reason=hp_reason,
                  homepage_domain_changed=bool(hp and home and domain(hp) != domain(home)))
    detected, provider_evidence = detect_provider(page.html) if page else ('Unknown', [])
    result['detected_website_provider'] = detected
    evidence = {'input': row, 'homepage_observations': observations, 'provider_evidence': provider_evidence,
                'homepage_title': page.title if page else '', 'candidates': [], 'budget_exhausted': False}
    if apply_confirmation(result, evidence, manual):
        return result, evidence
    if not home:
        result['srp_selection_status'] = 'not_found'
        result['review_reason'] = hp_reason
        return result, evidence
    group = shared.get(site_key(start_home), {})
    shared_brands = {brand_key(x) for x in group.get('brands', []) if x}
    multi_brand = len(shared_brands) > 1
    same_brand_records = group.get('brand_counts', {}).get(brand_key(brand), 0) > 1
    routes = settings.get('_learned_routes', {})
    learned = route_hint(manual) + route_hints(routes, home, brand)
    if site_key(hp) != site_key(home):
        learned += route_hints(routes, hp, brand)
    items = candidates(home, page if hp_status == 'verified' else None, supplied_srp, detected, provider, brand, learned)
    if supplied_srp and hp and home and domain(hp) != domain(home) and domain(supplied_srp) == domain(hp):
        old = urlsplit(supplied_srp)
        rebased = normalize_url(urljoin(home, old.path) + ('?' + old.query if old.query else ''))
        existing = next((x for x in items if x['url'] == rebased), None)
        if existing:
            existing['sources'].append('repair')
        else:
            items.append({'url': rebased, 'sources': ['repair'], 'labels': ['Supplied inventory path on redirected homepage domain']})
        items.sort(key=lambda x: min(RANKS[s] for s in x['sources']))
    # Repaired URLs use the original authority, even when its homepage redirects.
    if hp_input and srp_input.startswith(hp_input) and srp_input != hp_input:
        suffix = srp_input[len(hp_input):].lstrip('/')
        if suffix and not suffix.startswith(('?', '#')) and any(suffix.lower().startswith(path.lstrip('/').split('?')[0].lower()) for path in DATA['generic_paths']):
            repaired = normalize_url(urljoin(home, '/' + suffix))
            if repaired:
                existing = next((x for x in items if x['url'] == repaired), None)
                if existing:
                    if 'repair' not in existing['sources']:
                        existing['sources'].append('repair')
                else:
                    items.append({'url': repaired, 'sources': ['repair'], 'labels': []})
                items.sort(key=lambda x: min(RANKS[s] for s in x['sources']))
    eligible = []
    attempted = 0
    best_priority = None
    for position, item in enumerate(items):
        rank = min(RANKS[x] for x in item['sources'])
        priority = source_priority(item['sources'])
        if best_priority is not None and priority > best_priority:
            break
        entry = dict(item, rank=rank)
        reason = url_rejection(item['url'])
        if domain(item['url']) != domain(home) and not any(x in item['sources'] for x in ('navigation', 'homepage_link', 'learned')):
            reason = 'External SRP without an official homepage link establishing its relationship'
        if reason:
            entry.update(status='rejected', reason=reason, evidence=[])
            evidence['candidates'].append(entry)
            continue
        if attempted >= settings['max_candidates'] or time.monotonic() >= deadline:
            evidence['budget_exhausted'] = True
            break
        attempted += 1
        response = transport.fetch(item['url'], deadline)
        entry['fetch'] = response.evidence()
        response = replace(response, final_url=normalize_url(response.final_url))
        entry['fetch']['final_url'] = response.final_url
        target_page = parse_page(response.text, response.final_url) if response.status == 200 else None
        if blocked(response, target_page):
            status, reason, signals = 'unverified_blocked', 'Blocked response; page existence/purpose unverified', []
        elif response.status != 200:
            status = 'unverified_transport' if response.error or response.status >= 500 else 'rejected'
            reason, signals = response.error or f'HTTP {response.status}', []
        else:
            status, reason, signals = judge_srp(response.final_url, target_page, brand, multi_brand, home)
            if status == 'verified' and same_brand_records:
                if not make_selector(response.final_url) and not target_page.selected_makes:
                    reason = 'Verified all-new inventory; individual dealer/location scope is unconfirmed'
                    signals.append('scope=all_new')
                else:
                    status, reason = 'scope_review', 'Multiple same-brand records share the site; brand-filtered page lacks dealer/location context'
        entry.update(status=status, reason=reason, evidence=signals,
                     title=target_page.title if target_page else '', heading=target_page.heading if target_page else '',
                     canonical_url=target_page.canonical_url if target_page else '',
                     inventory_scope='all_new' if 'scope=all_new' in signals else 'brand_specific')
        evidence['candidates'].append(entry)
        if status == 'scope_review' and target_page and not same_brand_records:
            refinements = [x for x in brand_form_candidates(target_page, response.final_url, brand)
                           if not any(prior['url'] == x for prior in items)]
            for refined in reversed(refinements):
                items.insert(position + 1, {'url': refined, 'sources': list(item['sources']),
                                            'labels': ['Observed inventory make selector: ' + brand]})
        if status == 'verified':
            eligible.append(entry)
            if entry['inventory_scope'] == 'brand_specific' or not brand:
                best_priority = priority if best_priority is None else min(best_priority, priority)
        # A blocked homepage plus blocked known candidate is a bot wall, not a reason to spray fallback paths.
        if hp_status == 'unverified_blocked' and status == 'unverified_blocked':
            break
    if eligible:
        chosen, ambiguous = select_verified(eligible, multi_brand, home, brand, detected if detected != 'Unknown' else provider)
        if ambiguous:
            result['srp_selection_status'] = 'ambiguous'
            result['best_unverified_srp_url'] = chosen['fetch']['final_url']
            result['review_reason'] = 'Multiple distinct verified inventory destinations share the highest source tier; inspect scope/canonical behavior'
        else:
            result.update(recommended_new_srp_url=chosen['fetch']['final_url'], srp_selection_status='verified',
                          srp_inventory_scope=chosen['inventory_scope'],
                          srp_selection_source=min(chosen['sources'], key=RANKS.get),
                          srp_selection_reason=chosen['reason'] + f'; strongest supporting source tier {chosen["rank"]}')
    else:
        best = select_unverified(evidence['candidates'], home, brand, detected if detected != 'Unknown' else provider)
        if best:
            result.update(srp_selection_status=best['status'], best_unverified_srp_url=best.get('fetch', {}).get('final_url') or best['url'],
                          review_reason=best['reason'])
        else:
            result.update(srp_selection_status='not_found', review_reason='No candidate met inventory and dealer/brand rules')
        if evidence['budget_exhausted']:
            result['review_reason'] += '; bounded candidate/time budget reached'
    if hp_status != 'verified':
        result['review_reason'] = '; '.join(filter(None, [result['review_reason'], hp_reason]))
    return result, evidence


def pick_columns(fields, args):
    lookup = {x.strip().lower(): x for x in fields}
    mapping = {}
    for key, aliases in ALIASES.items():
        explicit = getattr(args, key + '_column', None)
        if explicit:
            if explicit not in fields:
                raise ValueError(f'Column {explicit!r} is absent')
            mapping[key] = explicit
        else:
            found = [lookup[x] for x in aliases if x in lookup]
            if len(found) > 1:
                raise ValueError(f'Ambiguous {key} columns: {found}. Specify --{key.replace("_", "-")}-column')
            mapping[key] = found[0] if found else None
    if not mapping['homepage']:
        raise ValueError('A homepage column is required')
    return mapping


def atomic_csv(path, fields, rows):
    temp = path.with_suffix('.csv.tmp')
    with temp.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp, path)


def run(args):
    if args.input:
        source = Path(args.input).resolve()
        with source.open('r', newline='', encoding=args.encoding) as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames
            if not fields or len(fields) != len(set(fields)):
                raise ValueError('CSV needs unique column names')
            rows = list(reader)
        if any(None in row or any(value is None for value in row.values()) for row in rows):
            raise ValueError('CSV contains rows with missing or extra cells')
        input_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    else:
        source = None
        rows = [dict(homepage_url=args.url, new_srp_url=args.srp or '', website_provider=args.provider or '',
                     brand=args.brand or '', dealer_id=args.dealer_id or '')]
        fields = list(rows[0])
        input_hash = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    if set(fields) & set(RESULT_FIELDS):
        raise ValueError('Input contains result columns; use the original source CSV or rename conflicting columns')
    all_rows = rows
    indices_path = getattr(args, 'indices_file', None)
    if indices_path:
        indices = json.loads(Path(indices_path).read_text())
        if not isinstance(indices, list) or len(set(indices)) != len(indices) or any(type(i) is not int or not 0 <= i < len(all_rows) for i in indices):
            raise ValueError('Indices file must contain unique zero-based source row indices')
        if args.limit:
            raise ValueError('Use --indices-file or --limit, not both')
        rows = [all_rows[i] for i in indices]
    else:
        rows = rows[:args.limit] if args.limit else rows
    col_map = pick_columns(fields, args)
    settings = {'am_dealer_ids': args.am_dealer_ids, 'row_timeout': args.row_timeout,
                'max_candidates': args.max_candidates, 'columns': col_map,
                'limit': args.limit, 'timeout': args.timeout, 'brand': args.brand or ''}
    settings['indices_sha256'] = hashlib.sha256(Path(indices_path).read_bytes()).hexdigest() if indices_path else None
    route_path = Path(args.learned_routes) if args.learned_routes else Path(__file__).resolve().parents[1] / 'references/learned-routes.json'
    settings['learned_routes_sha256'] = hashlib.sha256(route_path.read_bytes()).hexdigest() if route_path.exists() else None
    homepage_path = getattr(args, 'resolved_homepages', None)
    settings['resolved_homepages_sha256'] = hashlib.sha256(Path(homepage_path).read_bytes()).hexdigest() if homepage_path else None
    manual_path = Path(getattr(args, 'manual_reviews', None) or DEFAULT_PATH)
    settings['manual_reviews_sha256'] = hashlib.sha256(manual_path.read_bytes()).hexdigest() if manual_path.exists() else None
    runtime_settings = dict(settings, _learned_routes=load_routes(route_path), _resolved_homepages=load_homepage_hints(homepage_path),
                            _manual_reviews=load_reviews(manual_path))
    scripts = Path(__file__).resolve().parent
    code_hash = hashlib.sha256(b''.join(path.read_bytes() for path in sorted(scripts.glob('*.py')))
                               + (scripts.parent / 'references/providers.json').read_bytes()).hexdigest()
    identity = {'input_sha256': input_hash, 'rules_sha256': code_hash, 'settings': settings}
    out = Path(args.out).resolve() if args.out else Path.cwd() / ('dealer-url-run-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    if source and source in [out / 'results.csv', out / 'review.csv']:
        raise ValueError('Output would replace the source input')
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / 'manifest.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest.get('identity') != identity:
            raise ValueError('Run folder input/config/rules differ. Choose a new --out folder to prevent stale resume')
    else:
        if any(out.iterdir()):
            raise ValueError('Output folder is nonempty and lacks a run manifest; choose a new folder')
        manifest = {'identity': identity, 'started_at': utc_now(), 'input': str(source) if source else 'single',
                    'input_rows': len(all_rows), 'selected_rows': len(rows), 'preview': bool(args.limit)}
        if indices_path:
            manifest['selected_source_indices'] = indices
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    shared = defaultdict(lambda: {'brands': set(), 'brand_counts': Counter()})
    for row in all_rows:
        brand = row.get(col_map.get('brand'), '') or args.brand or (DATA['brand_prefixes'].get(row.get(col_map.get('dealer_id'), '')[:2].upper(), '') if args.am_dealer_ids else '')
        hp = normalize_url(row.get(col_map['homepage'], ''))
        dealer_id = row.get(col_map.get('dealer_id'), '')
        manual = matching_review(runtime_settings['_manual_reviews'], dealer_id, hp, brand)
        start_home = homepage_hint(manual) or runtime_settings['_resolved_homepages'].get(homepage_hint_key(dealer_id, hp), hp)
        group = shared[site_key(start_home)]
        group['brands'].add(brand)
        group['brand_counts'][brand_key(brand)] += 1
    journal = out / 'checkpoint.jsonl'
    completed = {}
    if journal.exists():
        for line in journal.read_text().splitlines():
            try:
                record = json.loads(line)
                completed[record['index']] = record
            except (ValueError, KeyError):
                continue
    if args.retry_review:
        completed = {i: x for i, x in completed.items() if x['result']['srp_selection_status'] == 'verified' and x['result']['homepage_selection_status'] == 'verified'}
    learned_meta = json.loads(route_path.read_text()) if route_path.exists() else {}
    evidence_dir = args.browser_evidence or learned_meta.get('browser_evidence_dirs') or learned_meta.get('browser_evidence')
    transport = Transport(args.concurrency, args.timeout, evidence_dir)
    print(json.dumps({'event': 'start', 'run_dir': str(out), 'rows': len(rows), 'resume_rows': len(completed)}), flush=True)
    with journal.open('a', encoding='utf-8') as handle, ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(analyze_row, row, col_map, transport, runtime_settings, shared): i
                   for i, row in enumerate(rows) if i not in completed}
        for future in as_completed(futures):
            index = futures[future]
            try:
                result, evidence = future.result()
            except Exception as exc:
                result = {key: '' for key in RESULT_FIELDS}
                result.update(homepage_selection_status='worker_error', srp_selection_status='worker_error',
                              review_reason=type(exc).__name__ + ': ' + str(exc), checked_at=utc_now())
                evidence = {'input': rows[index], 'error': result['review_reason']}
            record = {'index': index, 'result': result, 'evidence': evidence}
            handle.write(json.dumps(record) + '\n')
            handle.flush()
            completed[index] = record
            if len(completed) % 20 == 0 or len(completed) == len(rows):
                print(json.dumps({'event': 'progress', 'completed': len(completed), 'total': len(rows),
                                  'srp_states': dict(Counter(x['result']['srp_selection_status'] for x in completed.values()))}), flush=True)
    output = [dict(row, **completed[i]['result']) for i, row in enumerate(rows)]
    atomic_csv(out / 'results.csv', fields + RESULT_FIELDS, output)
    review = [x for x in output if x['srp_selection_status'] != 'verified' or x['homepage_selection_status'] != 'verified']
    atomic_csv(out / 'review.csv', fields + RESULT_FIELDS, review)
    with (out / 'candidates.jsonl').open('w') as handle:
        for index in range(len(rows)):
            handle.write(json.dumps({'index': index, **completed[index]['evidence']}) + '\n')
    summary = {'finished_at': utc_now(), 'rows': len(rows), 'review_rows': len(review),
               'homepage_states': dict(Counter(x['homepage_selection_status'] for x in output)),
               'srp_states': dict(Counter(x['srp_selection_status'] for x in output)),
               'srp_scopes': dict(Counter(x['srp_inventory_scope'] for x in output if x['srp_selection_status'] == 'verified')), 'run_dir': str(out)}
    (out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps({'event': 'complete', **summary}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--input', help='Source CSV; original cells and row order are preserved')
    inputs.add_argument('--url', help='Homepage for an individual check')
    parser.add_argument('--srp')
    parser.add_argument('--provider')
    parser.add_argument('--brand')
    parser.add_argument('--dealer-id')
    parser.add_argument('--am-dealer-ids', action='store_true', help='Enable confirmed AM dealer-ID prefix brand mapping')
    parser.add_argument('--out', help='New output folder, or matching prior run to resume')
    parser.add_argument('--limit', type=int, help='Preview first N rows; shared-site context uses the entire file')
    parser.add_argument('--indices-file', help='JSON list of zero-based source row indices to check; shared-site context still uses the entire input')
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--concurrency', type=int, default=3)
    parser.add_argument('--timeout', type=float, default=8)
    parser.add_argument('--row-timeout', type=float, default=45)
    parser.add_argument('--max-candidates', type=int, default=10)
    parser.add_argument('--encoding', default='utf-8-sig')
    parser.add_argument('--retry-review', action='store_true', help='Reprocess unresolved rows in a matching run')
    parser.add_argument('--browser-evidence', help='Directory of fresh browser response JSON files from browser_probe.py')
    parser.add_argument('--learned-routes', help='Previously observed route hints; default uses the skill reference file if present. All hints require live verification')
    parser.add_argument('--resolved-homepages', help='Analysis/results CSV with prior resolved_homepage_url values. Match dealer ID and source homepage, skip the old redirect chain, and verify the destination')
    parser.add_argument('--manual-reviews', help='Explicit user confirmations; default uses references/manual-reviews.json. Reuse for 24 hours, then treat routes as hints')
    for key in ALIASES:
        parser.add_argument('--' + key.replace('_', '-') + '-column', dest=key + '_column')
    args = parser.parse_args()
    if any(value <= 0 for value in (args.workers, args.concurrency, args.timeout, args.row_timeout, args.max_candidates)) or (args.limit is not None and args.limit <= 0):
        parser.error('Worker, request, time, candidate and preview limits must be positive')
    try:
        run(args)
    except (OSError, ValueError, csv.Error) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
