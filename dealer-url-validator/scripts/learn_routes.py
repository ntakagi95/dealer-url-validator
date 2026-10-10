#!/usr/bin/env python3
"""Save browser-confirmed inventory routes as reusable hints for HTTP checks."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
from url_rules import site_key, url_rejection


def run(args):
    source, evidence = Path(args.run_dir), Path(args.browser_evidence)
    with (source / 'results.csv').open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    records = [json.loads(line) for line in (source / 'candidates.jsonl').read_text().splitlines()]
    if len(rows) != len(records):
        raise ValueError('Result and evidence counts differ')
    sites = {}
    learned = 0
    for row, record in zip(rows, records):
        if row['homepage_selection_status'] != 'verified' or row['srp_selection_status'] != 'verified':
            continue
        url = row['recommended_new_srp_url']
        candidate = next((x for x in record.get('candidates', []) if x['status'] == 'verified'
                          and x.get('fetch', {}).get('final_url') == url and x['fetch'].get('via') == 'browser'), None)
        if not candidate or url_rejection(url):
            continue
        path = evidence / (hashlib.sha256(candidate['fetch']['requested_url'].encode()).hexdigest() + '.json')
        if not path.exists():
            continue
        observation = json.loads(path.read_text())
        if hashlib.sha256(observation['text'].encode()).hexdigest() != candidate['fetch'].get('html_sha256'):
            continue
        hint = {'homepage_url': row['recommended_homepage_url'], 'srp_url': url,
                'expected_brand': row['expected_brand'], 'inventory_scope': row['srp_inventory_scope'],
                'observed_at': observation['captured_at']}
        for home in (row['recommended_homepage_url'], row.get('homepage_url', '')):
            key = site_key(home)
            if key and hint not in sites.setdefault(key, []):
                sites[key].append(hint)
        learned += 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    directories = [str(evidence.resolve())]
    if getattr(args, 'merge', False) and out.exists():
        previous_metadata = json.loads(out.read_text())
        directories += previous_metadata.get('browser_evidence_dirs', [])
        if previous_metadata.get('browser_evidence'):
            directories.append(previous_metadata['browser_evidence'])
        existing = previous_metadata.get('sites', {})
        for key, hints in sites.items():
            for hint in hints:
                previous = existing.setdefault(key, [])
                previous[:] = [item for item in previous if not (item['srp_url'] == hint['srp_url'] and item['expected_brand'] == hint['expected_brand'])]
                previous.append(hint)
        sites = existing
    sites = {key: [hint for hint in hints if not url_rejection(hint['srp_url'])] for key, hints in sites.items()}
    sites = {key: hints for key, hints in sites.items() if hints}
    temp = out.with_suffix('.json.tmp')
    directories = list(dict.fromkeys(str(Path(directory).resolve()) for directory in directories if directory and directory != 'None'))
    temp.write_text(json.dumps({'version': 1, 'source_run': str(source.resolve()), 'browser_evidence': str(evidence.resolve()), 'browser_evidence_dirs': directories, 'sites': sites}, indent=2) + '\n')
    temp.replace(out)
    print(json.dumps({'browser_confirmed_rows_learned': learned, 'site_keys': len(sites), 'out': str(out)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--browser-evidence', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--merge', action='store_true', help='Preserve existing site hints and refresh matching route/brand observations')
    run(parser.parse_args())
