# Dealer homepage and inventory decisions

The supplied URLs are historical inputs. The observed destination is where a request ended. The recommended URL is the verified page selected for use. Keep these separate in reports.

## Homepage

Automatically adopt the final homepage redirect destination, including a different domain, as requested by the user. Dealer identity confirmation is not an approval gate. Keep the redirect observations and domain-change field for visibility. A blocked destination remains unverified; record it in `resolved_homepage_url` without claiming page verification.

If the submitted homepage is service, inventory, or an error page, test the site's own home link and root. Preserve legitimate locale and dealership-section paths. Department and inventory URLs are not acceptable homepages.

Informational pages such as about/contact pages and dealer-program pages are not homepages. Verify their home link or root. Remove default `:443`/`:80` from resolved and recommended URLs without changing required query filters. Final operational exports contain no explicit ports; nondefault ports cannot be removed while retaining verification without checking the port-free destination.

## Dealer and brand scope

Prefer the intended dealer/brand's complete new inventory when that scope can be verified. If it cannot be confirmed, the user permits the site's verified all-new inventory SRP as a fallback. Label the chosen scope as `brand_specific` or `all_new`; the latter does not claim brand/dealer verification. Model, trim, year, fuel, body, price, commercial-only, in-transit-only, featured, offers, demo, and loaner subsets are ineligible for either scope.

An explicit brand column takes precedence. Enable `--am-dealer-ids` only for an AM file or records whose metadata establishes an AM dealer ID. The confirmed prefix map is in `providers.json`. For other inputs, obtain an explicit brand or authoritative metadata; unknown prefixes remain unresolved. The mapping must not be applied to unrelated IDs.

Brand-specific recommendations require evidence from the active selector, inventory heading, or scoped vehicle data. A make query parameter or brand-named path alone is not proof. On shared sites, prefer verified brand scope; otherwise use a verified unfiltered all-new page. An unconfirmed brand-filtered URL cannot be labeled all-new. If several same-brand dealer records share a hostname and location context is missing, a site-wide all-new fallback is permitted, without claiming that a specific location was verified.

Some brands use distinct dealer subdomains of a shared registered domain. Group shared websites by hostname, ignoring `www.`, rather than treating all `porsche.com` dealers as one website.

## Verification and ranking

Apply the same judgment to supplied and discovered candidates. Check requested/final URL semantics, HTTP response, soft errors/challenges, page purpose, and brand scope. HTTP 200 alone is insufficient. A working inventory interface with zero vehicles can still be valid.

Prefer verified brand-specific scope over a verified all-new fallback. Within the chosen scope and comparable evidence, prefer:

1. Current official homepage navigation for full new inventory.
2. Another current official homepage link for full new inventory.
3. Supplied SRP or its verified redirected/repaired replacement.
4. Verified detected-provider blueprint.
5. Verified supplied-provider blueprint.
6. Verified generic fallback.

Previously observed routes are learned hints within the supplied/repair tier. Prefer a recent observed-route hint within that tier, but verify its current page over HTTP. Its historical source does not become current official navigation. Hints expire after 30 days; reusable rendered evidence expires after 24 hours and must contain usable page content. Missing dealer/location metadata and equal-support ties are not automatic reasons for another browser visit.

Merge supporting sources when several routes resolve to the same candidate. Discovery order and final ranking are separate. A higher-ranked blocked candidate does not displace a verified lower-ranked candidate.

For comparable candidates, prefer an already-verified canonical destination, fewer redirects, and fewer unnecessary query parameters. Canonical metadata alone is not validation. Preserve required new-condition and brand/dealer selectors. Equal support for materially different destinations remains ambiguous.

Dealer eProcess may use `/search/new-{brand}/?tp=new`, as observed on `https://www.pohankaacura.com/search/new-acura/?tp=new`. Substitute a known expected brand only on detected or supplied Dealer eProcess sites; preserve `tp=new`. The brand path is a candidate, not scope proof: verify the inventory heading, active filters, and listings. Save exact observed site routes as learned hints so subsequent HTTP checks can try them without browser discovery. Official-site web retrieval can teach a route even when local HTTP is blocked; that does not turn a local 403 into a verified result.

Provider footprints and paths are hints; the CSV provider can be stale and embedded vendors can confuse HTML detection. Preserve the detection evidence and report conflicts as unknown.

Before launching browsers, re-rank existing eligible evidence. Within the same scope and source tier, prefer the resolved homepage's hostname, an already verified canonical target, an explicitly corroborated requested-brand route, and then the provider's ordered full-new inventory blueprints. This ranking applies only to already verified pages. A brand-filtered route cannot serve as an unfiltered `all_new` fallback when dealer/location scope remains unconfirmed. Preserve genuine equal-support conflicts rather than arbitrarily selecting a URL.

For `best_unverified_srp_url`, rank actual pending candidates by supporting source, learned-route freshness, resolved hostname, expected-brand relevance, and provider blueprint order. Exclude known rejected subsets and 404s. Provider predictions and blocked routes remain unverified; a route guess is not evidence of page existence. Prefer offline re-ranking and bounded HTTP discovery before new client-side rendering. The original checker's first-success blueprint approach informs route preferences, but its protected-page/403 assumptions do not establish verification.

## Investigation

HTTP 403/429 and HTTP 200 challenge pages remain unverified. Do not use a provider exception or a random fake-path response as proof. Use bounded browser inspection, inspect current navigation and active inventory filters, and capture evidence. Further blocks remain in the review queue with candidate URLs and reasons.

The checker deliberately bounds requests and candidate searches. A budget-limited miss is unfinished investigation, not evidence that the dealership lacks new inventory. Browser rendering can improve evidence but cannot establish dealer location from an ID with missing metadata.

## Outputs

`results.csv` preserves all original source cells and row order and adds homepage/SRP recommendations, inventory scope, selection states/reasons, expected brand/source, best unverified candidate, provider, and timestamp. `review.csv` contains unresolved records. `candidates.jsonl` retains candidate sources, response/redirect observations, title/headings, inventory evidence, canonical hint, and rejection reasons. `summary.json` counts verified brand-specific recommendations, all-new fallbacks, and unresolved results separately.

`manifest.json` binds the run to input bytes, column mapping, settings, and script/provider rules. The append-only checkpoint permits a matching interrupted run to resume. Changing the input/config/rules requires a new output folder. Browser evidence is supplemental and expires after 24 hours. Retry review results with the same settings and `--retry-review`; the original source file is never rewritten.

## Lessons from one-pass browser discovery

Dealer eProcess inventory URLs can include a brand and location slug (`/search/new-bmw-bloomfield-nj/?cy=07003&tp=new`) or required vendor parameters (`mk`, `s:md`). Learn exact official navigation destinations instead of guessing or deleting their selectors. `/search/new-{brand}/?tp=new` remains a candidate pattern rather than a universal route.

Motive inventory menus can encode filters in a `filters` JSON query. Inspect active `appliedFilters`, not merely the heading: model, trim, year, body, fuel, or price filters can make a page a subset even when its heading just says New. Keep a new-condition/make selector when required; inactive false filter values and sort-only settings do not imply a subset. A confirmed model/commercial subset cannot be chosen as the full new SRP.

Some real inventory pages omit New Inventory from their titles. Explicit new-condition data on VIN-backed vehicle cards can establish new stock when the page also has inventory filters, listing evidence, and a new-inventory URL or New heading. Reject mixed used/CPO stock and active subsets first. A guessed URL, marketing copy, or related vehicle card alone cannot establish a full new SRP.

Dealer.com HTTP responses may expose a `CollectionPage` named New Inventory at the exact requested/final URL, with VIN-backed `Car`/`Product` entries and `itemCondition` set to `schema.org/NewCondition`. A matching collection, multiple explicitly new vehicles, and actual listing elements can establish inventory purpose even when the visible title describes the dealership generally. Read conditions from structured vehicle data as well as rendered card attributes; reject used/refurbished conditions. This avoids browser rendering for sites whose HTTP responses already establish new-stock inventory.

Dealer Inspire listings may instead put `vin`, `make`, and `type` inside each card's `data-vehicle` JSON. Parse VIN-backed card objects to establish New versus Used stock. This can verify headings such as Hondas For Sale or Explore New Range Rover Models when the page has the full inventory interface; keep mixed-stock and model/fuel subset exclusions. Space.Auto's `make=BMW--X1` combines a make with a model, so exclude it before requests rather than treating it as a make-only filter.

Recheck competing candidates against current exclusions before declaring an ambiguity. An Electric/Hybrid heading is a fuel subset; `/new-{brand}-{model}-for-sale-...html` is a model sales page. Neither can tie with a verified full-new inventory destination merely because both appeared in navigation.

Keyword parameters such as `search=New+Civic-TypeR` can restrict inventory to one model even when the title and heading say New Inventory. Reject model keywords in `search`, `q`, and equivalent search-term parameters; preserve recognized full-new selectors such as `search=new`. Twin City Honda's unfiltered `/new-inventory/` was verified over HTTP as the replacement for its Civic Type R search URL.

Save each site investigation and its outcome. Fresh blocked observations are a reason to avoid repeated browser work for 24 hours, not a reason to label a predicted SRP valid. For a finite site list, report coverage by both unique hostname and matching dealer IDs, and preserve sites that remain blocked or budget-limited.

Check URL paths independently from query strings when discovering new-inventory links. Appending a question mark before applying an end-of-path pattern hides valid terminal routes such as `/inventory/new` and `/cars/new`. Recognize Shop New, Search New, and All New labels while retaining subset exclusions. When a verified group portal has no inventory itself, its official brand-site links can establish where to investigate next; record those links instead of guessing inventory paths on the portal.

When automatic security verification clears during the initial page load, wait for meaningful body content and inventory before serializing the page. A successful status and new-inventory title with a head-only document are insufficient. Apply readiness waits after the challenge has cleared as well as on initially unchallenged pages, without another navigation. Santa Barbara Honda's `/search/new-honda/?tp=new` remained unverified after such a partial capture; retain the candidate rather than claim that the inventory was verified.

A subsequent explicitly requested single load of the same Santa Barbara Honda URL, using the corrected post-challenge body wait, produced full Honda listings, VINs, and inventory filters with no warnings. The route was then verified. Keep this case as a readiness lesson, not a reason to repeat browser checks: use fresh successful rendered evidence for 24 hours and retain the exact route as a hint for later HTTP verification.

Use the first primary heading to identify combined new-and-used inventory. Later FAQ headings can mention both conditions without changing the inventory scope, as observed on Dick Hannah Volkswagen's full-new `/new-vehicles/` page. Continue rejecting actual combined-inventory primary headings and used/CPO vehicle data; reuse the saved capture when correcting this judgment instead of revisiting the site.
