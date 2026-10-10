"""Bounded HTTP fetching with shared URL cache and per-host cooldowns."""
import hashlib
import json
import threading
import time
from concurrent.futures import Future
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from curl_cffi import requests


@dataclass
class Fetch:
    requested_url: str
    final_url: str = ''
    status: int = 0
    text: str = ''
    redirects: list = field(default_factory=list)
    error: str = ''
    ssl_bypassed: bool = False
    via: str = 'http'

    def evidence(self):
        result = asdict(self)
        result['html_sha256'] = hashlib.sha256(result.pop('text').encode()).hexdigest()
        return result


class Transport:
    def __init__(self, concurrency=3, timeout=10, evidence_dir=None):
        self.semaphore = threading.Semaphore(concurrency)
        self.timeout = timeout
        self.local = threading.local()
        self.lock = threading.Lock()
        self.cache = OrderedDict()
        self.cooldown = {}
        directories = evidence_dir if isinstance(evidence_dir, (list, tuple)) else [evidence_dir]
        self.evidence_dirs = [Path(directory) for directory in directories if directory]

    def fetch(self, url, deadline=None):
        with self.lock:
            future = self.cache.get(url)
            owner = future is None
            if owner:
                while len(self.cache) >= 128:
                    completed_key = next((key for key, value in self.cache.items() if value.done()), None)
                    if completed_key is None:
                        break
                    self.cache.pop(completed_key)
                future = Future()
                self.cache[url] = future
            else:
                self.cache.move_to_end(url)
        if not owner:
            try:
                return future.result(timeout=max(0.1, deadline - time.monotonic()) if deadline else None)
            except TimeoutError:
                return Fetch(url, error='Row deadline reached while waiting for shared fetch')
        try:
            result = self._fetch(url, deadline)
        except Exception as exc:
            result = Fetch(url, error=type(exc).__name__ + ': ' + str(exc))
        future.set_result(result)
        return result

    def _fetch(self, url, deadline):
        for directory in self.evidence_dirs:
            path = directory / (hashlib.sha256(url.encode()).hexdigest() + '.json')
            if path.exists():
                try:
                    record = json.loads(path.read_text())
                    captured = datetime.fromisoformat(record.get('captured_at', '1970-01-01T00:00:00+00:00'))
                    age = (datetime.now(timezone.utc) - captured).total_seconds()
                    if record.get('requested_url') == url and record.get('via') == 'browser' and 0 <= age <= 86400 and record.get('status') == 200 and not record.get('error') and not record.get('ssl_bypassed'):
                        from url_rules import parse_page
                        page = parse_page(record.get('text', ''), record.get('final_url', url))
                        if not page.error and (len(page.text) >= 100 or page.inventory_evidence):
                            return Fetch(**{k: v for k, v in record.items() if k in Fetch.__dataclass_fields__})
                except (ValueError, OSError, TypeError):
                    pass
        host = urlsplit(url).netloc
        with self.lock:
            until = self.cooldown.get(host, 0)
        if until > time.monotonic():
            return Fetch(url, error='Host rate-limit cooldown; further investigation required')
        if not hasattr(self.local, 'session'):
            self.local.session = requests.Session(impersonate='chrome')
        last = Fetch(url)
        for attempt in range(2):
            remaining = deadline - time.monotonic() if deadline else self.timeout * 2
            if remaining <= 0:
                return Fetch(url, error='Row request deadline reached')
            acquired = self.semaphore.acquire(timeout=min(self.timeout, remaining))
            if not acquired:
                return Fetch(url, error='Row request deadline reached waiting for request slot')
            try:
                try:
                    remaining = deadline - time.monotonic() if deadline else self.timeout
                    if remaining <= 0:
                        return Fetch(url, error='Row request deadline reached')
                    response = self.local.session.get(url, timeout=min(self.timeout, remaining),
                                                      allow_redirects=True, max_redirects=10)
                except requests.exceptions.SSLError:
                    return Fetch(url, error='TLS certificate/connection failure; verification remains enabled')
                last = Fetch(url, response.url, response.status_code, response.text[:4_000_000],
                             [{'url': x.url, 'status': x.status_code} for x in response.history],
                             ssl_bypassed=False)
            except requests.exceptions.RequestException as exc:
                last = Fetch(url, error=type(exc).__name__ + ': ' + str(exc))
            finally:
                self.semaphore.release()
            if last.status == 429:
                retry_after = response.headers.get('Retry-After', '')
                seconds = min(300, max(30, int(retry_after))) if retry_after.isdigit() else 60
                with self.lock:
                    self.cooldown[host] = time.monotonic() + seconds
                return last
            if last.status not in (500, 502, 503, 504) and not last.error:
                return last
            if attempt == 0:
                time.sleep(min(1.0, max(0, deadline - time.monotonic())) if deadline else 1.0)
        return last
