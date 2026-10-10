#!/usr/bin/env python3
"""Export verified destinations without modifying the analysis or source file."""
import argparse
import csv
from pathlib import Path
from urllib.parse import urlsplit

from url_rules import normalize_url


FIELDS = ['dealer_id', 'account_id', 'website_provider', 'homepage_url', 'new_srp_url']


def verified_url(row, field, status_field):
    if row.get(status_field) != 'verified':
        return ''
    url = normalize_url(row.get(field, ''))
    # Nondefault ports identify another origin; dropping them would need a new check.
    if urlsplit(url).port is not None:
        return ''
    return url


def export_row(row):
    return dict(dealer_id=row.get('dealer_id', ''), account_id=row.get('account_id', ''),
                website_provider=row.get('website_provider', ''),
                homepage_url=verified_url(row, 'recommended_homepage_url', 'homepage_selection_status'),
                new_srp_url=verified_url(row, 'recommended_new_srp_url', 'srp_selection_status'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, help='Validator results.csv or exported Analysis tab')
    parser.add_argument('--out', required=True, help='New five-column CSV; existing files are not overwritten')
    args = parser.parse_args()
    with Path(args.input).open(newline='', encoding='utf-8-sig') as source:
        reader = csv.DictReader(source)
        required = {'recommended_homepage_url', 'recommended_new_srp_url', 'homepage_selection_status', 'srp_selection_status'}
        if not required.issubset(reader.fieldnames or []):
            parser.error('Input must contain homepage/SRP recommendations and verification statuses')
        with Path(args.out).open('x', newline='', encoding='utf-8') as output:
            writer = csv.DictWriter(output, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(export_row(row) for row in reader)


if __name__ == '__main__':
    main()
