#!/usr/bin/env python3
"""Review every unresolved dealer with homepage-first, rendered navigation discovery."""
import argparse
import asyncio
import csv
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright
from browser_probe import probe
from review_results import original_manifest
from url_rules import RANKS, brand_form_candidates, candidates, is_homepage, judge_srp, make_selector, normalize_url, parse_page, site_key, source_priority, url_rejection


def evidence_path(out, url):
    return out / (hashlib.sha256(url.encode()).hexdigest() + '.json')


def fresh_record(out, url):
    path = evidence_path(out, url)
    if path.exists():
        record = json.loads(path.read_text())
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(record['captured_at'])).total_seconds()
        if 0 <= age < 86400:
            return record
    return None


def review_jobs(source):
    with (source / 'results.csv').open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    records = [json.loads(x) for x in (source / 'candidates.jsonl').read_text().splitlines()]
    if len(rows) != len(records):
        raise ValueError('Results and evidence row counts differ')
    return [(i, row, record) for i, (row, record) in enumerate(zip(rows, records))
            if row['homepage_selection_status'] != 'verified' or row['srp_selection_status'] != 'verified']


def browser_needed(row, include_ambiguity=False):
    return (row['homepage_selection_status'] != 'verified'
            or row['srp_selection_status'].startswith('unverified')
            or row['srp_selection_status'] == 'not_found'
            or include_ambiguity and row['srp_selection_status'] in ('ambiguous', 'scope_review'))


def reusable_record(directory, url):
    if isinstance(directory, (list, tuple)):
        return next((record for path in directory if (record := reusable_record(Path(path), url))), None)
    record = fresh_record(directory, url) if directory else None
    if record and record['status'] == 200 and not record.get('error') and not record.get('ssl_bypassed'):
        page = parse_page(record['text'], record['final_url'])
        if not page.error and (len(page.text) >= 100 or page.inventory_evidence):
            return record
    return None


def recently_investigated_sites(path):
    if not path or not Path(path).exists():
        return set()
    recent = set()
    for key, item in json.loads(Path(path).read_text()).get('sites', {}).items():
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(item['investigated_at'])).total_seconds()
        if 0 <= age <= 86400:
            recent.add(key)
    return recent


def save_browser_history(path, journal, jobs, evidence, source, homepage_column):
    if not journal.exists():
        return
    attempts = [json.loads(line) for line in journal.read_text().splitlines()]
    history = json.loads(Path(path).read_text()) if Path(path).exists() else {'version': 1, 'sites': {}}
    observed = defaultdict(list)
    for attempt in attempts:
        aliases = {attempt['site']}
        for _, row, record in jobs:
            if site_key(row['resolved_homepage_url']) == attempt['site']:
                aliases.add(site_key(record['input'].get(homepage_column, '')))
        for key in aliases - {''}:
            observed[key].append(attempt)
    for key, visits in observed.items():
        investigated_at = max(x['started_at'] for x in visits)
        previous = history['sites'].get(key, {})
        if previous.get('investigated_at') and datetime.fromisoformat(previous['investigated_at']) >= datetime.fromisoformat(investigated_at):
            continue
        history['sites'][key] = {'investigated_at': investigated_at,
                                'source_run': str(source), 'browser_evidence': str(evidence),
                                'outcome': 'investigated; verification is recorded in reviewed results',
                                'visited_urls': list(dict.fromkeys(x['url'] for x in visits))}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(history, indent=2) + '\n')
    temporary.replace(path)


async def run(args):
    source, out = Path(args.run_dir).resolve(), Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    evidence = out / 'browser-evidence'
    evidence.mkdir(exist_ok=True)
    identity = {'source_sha256': hashlib.sha256(b''.join((source / name).read_bytes() for name in ('results.csv', 'candidates.jsonl', 'manifest.json'))).hexdigest(),
                'selection_settings': {key: getattr(args, key) for key in ('limit', 'max_candidates', 'timeout', 'settle', 'challenge_wait', 'channel', 'headed', 'skip_consent_blocker', 'include_ambiguity', 'evidence_from')}}
    indices = set(json.loads(Path(args.indices_file).read_text())) if args.indices_file else None
    identity['record_indices'] = sorted(indices) if indices is not None else None
    once_per_site = getattr(args, 'once_per_site', False)
    identity['selection_settings']['once_per_site'] = once_per_site
    history_path = getattr(args, 'browser_history', None) or Path(__file__).resolve().parents[1] / 'references/browser-history.json'
    # History is mutable operational state, including this run's own output.
    identity['selection_settings']['browser_history_path'] = str(Path(history_path).resolve())
    identity['selection_settings']['refresh_browser_history'] = getattr(args, 'refresh_browser_history', False)
    identity['selection_settings']['inventory_wait'] = getattr(args, 'inventory_wait', 8)
    manifest = out / 'manifest.json'
    if manifest.exists() and json.loads(manifest.read_text()) != identity:
        raise ValueError('Browser source/settings changed; choose a new output folder')
    manifest.write_text(json.dumps(identity, indent=2) + '\n')
    jobs = review_jobs(source)
    if indices is not None:
        if indices - {x[0] for x in jobs}:
            raise ValueError('Requested indices are outside the review queue')
        jobs = [x for x in jobs if x[0] in indices]
    jobs = [x for x in jobs if browser_needed(x[1], args.include_ambiguity)]
    columns = original_manifest(json.loads((source / 'manifest.json').read_text()))['identity']['settings']['columns']
    if args.limit:
        jobs = jobs[:args.limit]
    requested_jobs = list(jobs)
    coverage = out / 'coverage.jsonl'
    completed = {json.loads(x)['index'] for x in coverage.read_text().splitlines()} if coverage.exists() else set()
    recent_sites = set() if getattr(args, 'refresh_browser_history', False) else recently_investigated_sites(history_path)
    history_skipped = 0
    for index, row, original in jobs:
        if index not in completed and site_key(row['resolved_homepage_url']) in recent_sites:
            result = {'index': index, 'dealer_id': row.get(columns.get('dealer_id') or 'dealer_id', ''),
                      'homepage_url': row['resolved_homepage_url'], 'visited_urls': [], 'discovered_candidates': [],
                      'investigation_skipped': 'Site already investigated within 24 hours; reuse evidence before explicitly refreshing',
                      'captured_at': datetime.now(timezone.utc).isoformat()}
            with coverage.open('a') as handle:
                handle.write(json.dumps(result) + '\n')
            completed.add(index)
            history_skipped += 1
    groups = defaultdict(list)
    for job in jobs:
        if job[0] not in completed:
            groups[site_key(job[1]['resolved_homepage_url'] or job[2]['input'].get(columns['homepage'], ''))].append(job)
    locks = defaultdict(asyncio.Lock)
    slots = asyncio.Semaphore(args.concurrency)
    page_slots = asyncio.Semaphore(args.concurrency)
    source_manifest = json.loads((source / 'manifest.json').read_text())
    previous = args.evidence_from or source_manifest.get('browser_evidence')
    if not previous or previous == 'None':
        routes_file = Path(__file__).resolve().parents[1] / 'references/learned-routes.json'
        routes = json.loads(routes_file.read_text()) if routes_file.exists() else {}
        previous = routes.get('browser_evidence_dirs') or routes.get('browser_evidence')
    previous = [Path(x) for x in previous] if isinstance(previous, list) else Path(previous) if previous else None
    browser_navigations = 0
    navigation_journal = out / 'navigation-journal.jsonl'
    site_navigations = defaultdict(int)
    inventory_sites = set()
    attempted_urls = set()
    if navigation_journal.exists():
        for line in navigation_journal.read_text().splitlines():
            attempt = json.loads(line)
            site_navigations[attempt['site']] += 1
            attempted_urls.add(attempt['url'])
            if attempt.get('role') == 'inventory':
                inventory_sites.add(attempt['site'])
    async with async_playwright() as playwright:
        browser = None
        launch_lock = asyncio.Lock()
        async def visit(get_context, url, role='inventory'):
            nonlocal browser_navigations
            async with locks[url]:
                record = fresh_record(evidence, url)
                if not record:
                    record = reusable_record(previous, url)
                    if record:
                        origin = next((p for p in previous if reusable_record(p, url)), None) if isinstance(previous, list) else previous
                        record = dict(record, reused_from=str(evidence_path(origin, url)))
                        evidence_path(evidence, url).write_text(json.dumps(record) + '\n')
                if not record:
                    key = site_key(url)
                    if once_per_site and (site_navigations[key] >= 2 or url in attempted_urls or role == 'inventory' and key in inventory_sites):
                        return None
                    site_navigations[key] += 1
                    attempted_urls.add(url)
                    if role == 'inventory':
                        inventory_sites.add(key)
                    with navigation_journal.open('a') as handle:
                        handle.write(json.dumps({'site': key, 'url': url, 'role': role, 'started_at': datetime.now(timezone.utc).isoformat()}) + '\n')
                    context = await get_context()
                    browser_navigations += 1
                    await probe(context, url, evidence, page_slots, args.timeout, args.settle, args.challenge_wait, args.skip_consent_blocker,
                                inventory_wait=getattr(args, 'inventory_wait', 8) if role == 'inventory' else 0)
                    record = fresh_record(evidence, url)
                return record
        async def site_jobs(group):
            nonlocal browser
            async with slots:
                context = None
                async def get_context():
                    nonlocal context, browser
                    if context is None:
                        async with launch_lock:
                            if browser is None:
                                browser = await playwright.chromium.launch(channel=args.channel, headless=not args.headed)
                        context = await browser.new_context(viewport={'width': 1440, 'height': 1000}, locale='en-US')
                    return context
                try:
                    for index, row, original in group:
                        dealer_id = row.get(columns.get('dealer_id') or 'dealer_id', '')
                        visits, discovered, skipped = [], [], []
                        home = normalize_url(row['resolved_homepage_url'] or original['input'].get(columns['homepage'], ''))
                        if once_per_site and 'sell-your-car' in urlsplit(home).path:
                            p = urlsplit(home)
                            home = p.scheme + '://' + p.netloc + '/'
                        need_srp = row['srp_selection_status'] != 'verified' and (args.include_ambiguity or row['srp_selection_status'] not in ('ambiguous', 'scope_review'))
                        need_home = row['homepage_selection_status'] != 'verified' or row['srp_selection_status'] in ('unverified_blocked', 'not_found')
                        if home and (need_home or once_per_site):
                            hp = await visit(get_context, home, 'homepage')
                            (visits if hp else skipped).append(home)
                        else:
                            hp = None
                        page = parse_page(hp['text'], hp['final_url']) if hp and hp['status'] == 200 else None
                        if page and not page.error and not is_homepage(hp['final_url'], page) and not once_per_site:
                            parsed = urlsplit(hp['final_url'])
                            alternatives = page.home_links + [parsed.scheme + '://' + parsed.netloc + '/']
                            alternative = next((normalize_url(x) for x in alternatives if normalize_url(x) != home), None)
                            if alternative:
                                root = await visit(get_context, alternative)
                                visits.append(alternative)
                                root_page = parse_page(root['text'], root['final_url']) if root and root['status'] == 200 else None
                                if root_page and is_homepage(root['final_url'], root_page):
                                    home, hp, page = alternative, root, root_page
                        choices = []
                        if page and not page.error:
                            choices = [x for x in candidates(hp['final_url'], page, '', '', '', row['expected_brand'])
                                       if min(RANKS[s] for s in x['sources']) <= 2 and not url_rejection(x['url'])]
                            discovered.extend(choices)
                        prior = sorted(original.get('candidates', []), key=lambda x: (x['status'] != 'verified', x['rank']))
                        for candidate in prior:
                            if candidate['status'] != 'rejected' and not url_rejection(candidate['url']):
                                choices.append({'url': candidate['url'], 'sources': candidate['sources'], 'labels': candidate.get('labels', [])})
                        for url in (row.get('recommended_new_srp_url'), row.get('best_unverified_srp_url')):
                            if url and not url_rejection(url):
                                choices.append({'url': url, 'sources': ['supplied'], 'labels': []})
                        if not need_srp:
                            choices = []
                        choices.sort(key=lambda x: source_priority(x['sources']))
                        if once_per_site:
                            shared_brands = {job[1]['expected_brand'] for job in group}
                            if len(shared_brands) > 1:
                                choices.sort(key=lambda x: (*source_priority(x['sources']), bool(make_selector(x['url']))))
                            choices = [x for x in choices if site_key(x['url']) == site_key(hp['final_url'] if hp else home)]
                        seen = set()
                        best_priority = None
                        for position, choice in enumerate(choices):
                            if best_priority is not None and source_priority(choice['sources']) > best_priority:
                                break
                            url = normalize_url(choice['url'])
                            if not url or url in seen or len(seen) >= (1 if once_per_site else args.max_candidates):
                                continue
                            seen.add(url)
                            rendered = await visit(get_context, url)
                            (visits if rendered else skipped).append(url)
                            srp = None
                            if rendered and rendered['status'] == 200:
                                srp = parse_page(rendered['text'], rendered['final_url'])
                                status, _, signals = judge_srp(rendered['final_url'], srp, row['expected_brand'], False, home)
                                if status == 'verified' and ('scope=all_new' not in signals or not row['expected_brand']):
                                    best_priority = source_priority(choice['sources'])
                                for form in ([] if once_per_site else brand_form_candidates(srp, rendered['final_url'], row['expected_brand'])):
                                    item = {'url': form, 'sources': ['homepage_link'], 'labels': ['Rendered inventory make selector']}
                                    # The selector is discovered on inventory, so retain its actual provenance.
                                    item['sources'] = choice['sources']
                                    discovered.append(item)
                                    if form not in seen and not url_rejection(form):
                                        choices.insert(position + 1, item)
                            if rendered and (rendered['status'] in (403, 429) or srp and srp.error == 'Bot challenge') and len(seen) >= 2 and (not page or page.error):
                                break
                        result = {'index': index, 'dealer_id': dealer_id, 'homepage_url': home,
                                  'visited_urls': visits, 'discovered_candidates': discovered,
                                  'budget_skipped_urls': skipped,
                                  'captured_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
                        with coverage.open('a') as handle:
                            handle.write(json.dumps(result) + '\n')
                        completed.add(index)
                        print(json.dumps({'completed_records': len(completed), 'total_records': len(jobs), 'dealer_id': dealer_id, 'visited': len(visits)}), flush=True)
                finally:
                    if context:
                        await context.close()
        try:
            await asyncio.gather(*(site_jobs(group) for group in groups.values()))
        finally:
            if browser:
                await browser.close()
    save_browser_history(history_path, navigation_journal, requested_jobs, evidence, out, columns['homepage'])
    summary = {'source_run': str(source), 'requested_records': len(requested_jobs), 'completed_records': len(completed),
               'browser_navigations': browser_navigations, 'site_navigation_counts': dict(site_navigations),
               'requested_sites': len({site_key(job[1]['resolved_homepage_url']) for job in jobs}),
               'history_skipped_records': history_skipped,
               'settings': vars(args), 'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    (out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--limit', type=int, default=0, help='Dealer records; zero checks the entire review queue')
    parser.add_argument('--concurrency', type=int, default=3)
    parser.add_argument('--max-candidates', type=int, default=2)
    parser.add_argument('--timeout', type=int, default=25)
    parser.add_argument('--settle', type=float, default=2)
    parser.add_argument('--challenge-wait', type=float, default=8)
    parser.add_argument('--inventory-wait', type=float, default=8, help='Wait within the same page load for inventory elements, without reloading')
    parser.add_argument('--channel', default='chrome')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--indices-file', help='JSON list of source row indices for targeted follow-up')
    parser.add_argument('--skip-consent-blocker', action='store_true', help='Skip an observed ComplyAuto blocker.js parser stall and record the resource override')
    parser.add_argument('--evidence-from', help='Reuse fresh, usable evidence before launching Chrome')
    parser.add_argument('--include-ambiguity', action='store_true', help='Explicitly investigate ties/scope questions; default leaves these for local review')
    parser.add_argument('--once-per-site', action='store_true', help='One investigation per hostname: at most two new navigations total (homepage plus one SRP), shared across all records; no retries or filter followups')
    parser.add_argument('--browser-history', help='Recent per-site investigations; defaults to references/browser-history.json')
    parser.add_argument('--refresh-browser-history', action='store_true', help='Explicitly authorize a fresh investigation of sites already checked within 24 hours')
    args = parser.parse_args()
    if args.limit < 0 or min(args.concurrency, args.max_candidates, args.timeout) <= 0 or min(args.settle, args.challenge_wait, args.inventory_wait) < 0:
        parser.error('Invalid browser budget')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
