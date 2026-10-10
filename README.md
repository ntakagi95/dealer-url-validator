# Dealer URL Validator

A Codex skill and Python checker for dealership homepages and full new-inventory search result pages. Supports CSV batches and individual URLs, prefers confirmed brand/dealer inventory, and permits verified all-new inventory as a fallback.

Copy `dealer-url-validator/` into `~/.codex/skills/` to install the skill. Python 3.10+ is required.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r dealer-url-validator/requirements.txt
.venv/bin/python dealer-url-validator/scripts/check_urls.py --url https://dealer.example/ --brand Honda --out ./runs/example
```

For browser fallback, install `dealer-url-validator/requirements-browser.txt`. The default browser channel uses locally installed Chrome. Use the skill's bounded browser workflow only when HTTP and existing evidence cannot settle the result.

Run tests after installing `requirements-dev.txt`:

```sh
.venv/bin/python -m unittest discover -s tests -q
```

HTTP requests are shared across duplicate URLs, parsing is cached, candidate checks have time/count budgets, and browser history prevents repeated site visits. Exact learned routes are hints; 403/429 responses and persistent challenges stay unverified. Human confirmations have separate provenance and expire after 24 hours.

The release starts with empty evidence/history files. Source CSVs, dealer/account identifiers, spreadsheet links, captured HTML/screenshots and local confirmations are excluded. Keep generated run data private. The scripts are intended for locally operated checks of selected websites; they are not hardened as a public service accepting untrusted URLs. Browsers run in a fresh context and TLS verification stays enabled.

See `SECURITY.md` for the review scope and limitations. No GitHub publication is performed by this package.
