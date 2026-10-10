#!/usr/bin/env python3
"""Capture fresh rendered-page evidence for a bounded set of unresolved URLs."""
import argparse
import asyncio
import csv
import hashlib
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright
from url_rules import normalize_url


async def document_content(page):
    for attempt in range(3):
        try:
            return await page.content()
        except Exception:
            if attempt == 2:
                raise
            await page.wait_for_timeout(500)


def challenge_document(document):
    title = re.search(r'<title\b[^>]*>(.*?)</title\s*>', document, re.I | re.S)
    title = html.unescape(re.sub(r'<[^>]*>', '', title.group(1))) if title else ''
    return bool(re.search(r'just a moment|access denied|attention required|verify.*human|security check', title, re.I)
                or re.search(r'''\bid\s*=\s*["'](?:challenge-running|cf-challenge-running|px-captcha|challenge-form)["']''', document, re.I))


async def probe(context, url, out, semaphore, timeout, settle, challenge_wait=8, skip_consent_blocker=False, inventory_wait=0):
    async with semaphore:
        page = await context.new_page()
        record = {'requested_url': url, 'final_url': '', 'status': 0, 'text': '', 'redirects': [],
                  'error': '', 'ssl_bypassed': False, 'via': 'browser',
                  'captured_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        warnings = []
        record['blocked_resources'] = []
        if skip_consent_blocker:
            async def skip_blocker(route):
                record['blocked_resources'].append(route.request.url)
                await route.abort('blockedbyclient')
            await page.route(re.compile(r'^https://cdn\.complyauto\.com/cookiebanner/banner/[^/]+/blocker\.js(?:\?.*)?$'), skip_blocker)
        def navigation_response(response):
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                record['status'] = response.status
        page.on('response', navigation_response)
        try:
            try:
                response = await page.goto(url, wait_until='commit', timeout=timeout * 1000)
                try:
                    await page.wait_for_load_state('domcontentloaded', timeout=6000)
                except Exception as exc:
                    warnings.append('DOM readiness: ' + str(exc))
            except Exception as exc:
                warnings.append(type(exc).__name__ + ': ' + str(exc))
                response = None
            if response:
                record['status'] = response.status
                request = response.request.redirected_from
                chain = []
                while request:
                    prior = await request.response()
                    chain.append({'url': request.url, 'status': prior.status if prior else 0})
                    request = request.redirected_from
                record['redirects'] = list(reversed(chain))
            await page.wait_for_timeout(settle * 1000)
            # Give browser challenge redirects a bounded opportunity to complete.
            if challenge_document(await document_content(page)):
                await page.wait_for_timeout(challenge_wait * 1000)
            if record['status'] == 200 and not challenge_document(await document_content(page)):
                try:
                    await page.wait_for_function("document.body && document.body.innerText.trim().length >= 100", timeout=10000)
                except Exception as exc:
                    warnings.append('Body readiness: ' + str(exc))
                if inventory_wait:
                    try:
                        await page.wait_for_function("""() => {
                            const stock = document.querySelectorAll('[data-vin], [class*="vehicle-card"], [class*="vehicleCard"], [class*="vehicle-listing"], [data-vehicle-id]');
                            return stock.length > 0 || /\\b(?:0|zero|no)\\s+(?:matching\\s+|new\\s+)?(?:vehicles|results|cars)\\b/i.test(document.body?.innerText || '');
                        }""", timeout=inventory_wait * 1000)
                    except Exception as exc:
                        warnings.append('Inventory readiness: ' + str(exc))
            # Serialize the actual active state of native make/model selectors.
            await page.evaluate("""() => { for (const o of document.querySelectorAll('select option')) {
                if (o.selected) o.setAttribute('selected', ''); else o.removeAttribute('selected');
            } }""")
            record['final_url'] = page.url
            record['text'] = (await document_content(page))[:4_000_000]
            digest = hashlib.sha256(url.encode()).hexdigest()
            try:
                await page.screenshot(path=str(out / (digest + '.png')), full_page=False, timeout=2000, animations='disabled')
            except Exception as exc:
                warnings.append('Screenshot: ' + str(exc))
        except Exception as exc:
            record['error'] = type(exc).__name__ + ': ' + str(exc)
            record['final_url'] = page.url if page.url != 'about:blank' else ''
            # Without captured DOM the response is insufficient to verify a page.
            record['status'] = 0
        finally:
            await page.close()
        path = out / (hashlib.sha256(url.encode()).hexdigest() + '.json')
        record['warnings'] = warnings
        if not record['status'] and not record['error']:
            record['error'] = 'No main-document HTTP response was received'
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(record) + '\n')
        temporary.replace(path)
        print(json.dumps({k: record[k] for k in ('requested_url', 'final_url', 'status', 'error')}), flush=True)


async def run(args):
    urls = list(args.url or [])
    if args.run_dir:
        with (Path(args.run_dir) / 'review.csv').open(newline='', encoding='utf-8-sig') as handle:
            for row in csv.DictReader(handle):
                if row.get('homepage_selection_status') != 'verified':
                    urls.append(row.get('resolved_homepage_url') or row.get('homepage_url', ''))
                if row.get('best_unverified_srp_url'):
                    urls.append(row['best_unverified_srp_url'])
    urls = list(dict.fromkeys(filter(None, (normalize_url(x) for x in urls))))[:args.limit]
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel=args.channel, headless=not args.headed)
        context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
        try:
            semaphore = asyncio.Semaphore(args.concurrency)
            await asyncio.gather(*(probe(context, url, out, semaphore, args.timeout, args.settle,
                                        skip_consent_blocker=args.skip_consent_blocker,
                                        inventory_wait=args.inventory_wait) for url in urls))
        finally:
            await context.close()
            await browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', action='append')
    parser.add_argument('--run-dir', help='Read candidate/homepage URLs from review.csv')
    parser.add_argument('--out', required=True)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--concurrency', type=int, default=2)
    parser.add_argument('--timeout', type=int, default=20)
    parser.add_argument('--settle', type=float, default=3)
    parser.add_argument('--inventory-wait', type=float, default=8, help='Wait within this page load for inventory elements; never refresh')
    parser.add_argument('--channel', default='chrome', help='Installed browser channel; default uses Google Chrome')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--skip-consent-blocker', action='store_true', help='Skip the specific ComplyAuto blocker.js resource when it stalls document parsing; record the override')
    args = parser.parse_args()
    if not args.url and not args.run_dir:
        parser.error('Provide --url or --run-dir')
    if args.inventory_wait < 0:
        parser.error('Inventory wait cannot be negative')
    if args.limit <= 0 or args.concurrency <= 0 or args.timeout <= 0 or args.settle < 0:
        parser.error('Limits must be positive and settle time nonnegative')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
