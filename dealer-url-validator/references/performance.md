# Efficient checks and reusable evidence

Use the least expensive evidence that answers the current question:

1. Re-rank captured evidence with current rules.
2. Reuse fresh successful captures and exact observed routes.
3. Check unresolved candidates over HTTP, sharing identical URL requests across records.
4. Use a bounded browser investigation when HTTP lacks page or filter evidence.

Keep the full input when selecting rows so shared-site and brand/location scope remain correct. Match prior resolved homepages by dealer ID and original homepage. Avoid reloading known homepages solely to test a pending SRP when navigation and destination evidence are sufficient.

## Reduce work before requests

Reject model/trim/year/fuel/body/price filters, active encoded JSON filters, composite make/model values such as `BMW--X1`, and search keywords such as `New Civic-TypeR`. Preserve legitimate new-condition and brand selectors. Recognize terminal `/cars/new` and `/inventory/new` paths independently of query strings.

Official navigation outranks provider guesses, but ineligible links cannot consume visit budgets. A verified fallback outranks a blocked stronger candidate. Stop weaker paths when preferred scope is established; retain comparable alternatives only when they could change the choice.

## Reuse parsing and transport

`Transport` coalesces concurrent identical URL requests, bounds its response cache, and respects rate-limit cooldowns. Fresh usable browser captures avoid HTTP requests; blocked and head-only captures cannot act as successful evidence.

Merged learned routes retain an ordered `browser_evidence_dirs` list, newest first. Both HTTP checks and browser batches search it for usable fresh captures. A partial newer capture does not hide older usable evidence. An explicit `--browser-evidence` or `--evidence-from` overrides that list, supporting a homepage-only cache for an authorized fresh investigation. Do not copy large capture directories merely to merge route hints.

`parse_page` caches 16 exact HTML/destination pairs and returns independent page objects. Repeated transport/content/scope checks reuse parsing; changed HTML or destination receives a new parse. Cache observations, never dealer-specific judgments. Retained raw HTML is bounded to roughly 64 MB at the transport's 4 MB response limit, plus parsed page fields.

## Avoid repeated browsers

`browser_batch.py` journals attempted navigations before loading and automatically saves visit history on completion. Successes and failures receive a 24-hour cooldown. Saving history does not invalidate resume identity. Cached-only and history-skipped runs do not renew the cooldown because they contain no fresh attempts.

For one-pass investigations, share one homepage and one inventory load per hostname. Incomplete inventory is a content miss, not permission for another navigation. After automatic security verification clears, wait for body content and inventory inside the same load. Preserve timeouts and warnings. Direct `browser_probe.py` checks serve explicit fresh single-URL requests; preserve their history too.

## Lessons from the batch

Dealer.com may expose full new inventory through a matching `CollectionPage` with VIN-backed vehicles and `NewCondition`, even when titles omit New Inventory. Dealer Inspire may expose `vin`, `make`, and `type` in card `data-vehicle` JSON. Parse these forms before rendering; mixed used stock and active subsets remain ineligible.

Raleigh's portal linked separate Jaguar and Land Rover sites. Galleria's full-new `/cars/new/` verified over HTTP after a model-filtered route consumed its browser budget. Santa Barbara verified after corrected post-challenge body readiness produced listings and VINs. Twin City's Civic Type R search was replaced by verified unfiltered `/new-inventory/`. Exact routes and successful captures are retained in learned references.

## Measure offline

Run behavior tests and replay saved captures against current judgments. Benchmark repeated parsing on those captures; report CPU parsing performance separately from end-to-end network speed. Count HTTP calls, browser loads, reuse, and unresolved records separately. A repeat investigation suppressed by fresh history should make zero browser navigations.
# Manual evidence reuse

Reuse explicit human-confirmed full-new SRPs for 24 hours, with `manual_review` provenance and no synthetic HTTP status. This removes all inventory requests for those records while homepage verification remains independent. The October 8 feedback covers 23 records and 21 unique SRP URLs. No dealership loads were made while importing it. After expiry, prioritize the exact corrected route before weaker guesses; keep each site's documented path/authority rather than shortening `/new-inventory/index.htm` or globally rewriting hostnames.
