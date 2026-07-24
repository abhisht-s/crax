# SeaView App Store Rejection Research — Methodology

## Document status

This document was created before any SeaView source discovery or source-page
fetching began. It records the prospective research design required by the
brief. It will be updated during collection with actual usage, reallocations,
saturation checks, exclusions, limitations, and the final page count.

- Research start date: 2026-07-17
- Date obtained from: execution environment (`Asia/Kolkata`)
- Research mode: exhaustive, two-pass public-web study
- Hard ceiling: 1,000 consumed page-equivalents
- Source policy: public, legitimately accessible material only
- Repository policy: research artifacts only; no source code, configuration,
  database, infrastructure, or production-system changes

## Research questions

1. What precise problems do iOS developers experience before, during, and after
   App Store Review?
2. What materially different rejection reasons do developers report?
3. What product, code, metadata, policy, operational, or reviewer-access root
   causes sit beneath Apple's surface-level classifications?
4. How many rejection or resubmission cycles are explicitly reported, and what
   correlates with repeated rejection?
5. Which unmet needs create a defensible market opportunity for SeaView,
   especially for inexperienced, solo, no-code, AI-assisted, and vibe-coding
   developers?

## Initial 1,000-page budget

The buckets are mutually exclusive for accounting. A Reddit thread in a
no-code community is charged to Reddit; the no-code/AI bucket covers
non-Reddit communities. Each fetched URL is charged once even if it yields
multiple independent rejection accounts.

| Source bucket | Initial ceiling | Pass 1 breadth target | Pass 2 / reserve target | Purpose |
|---|---:|---:|---:|---|
| Reddit | 600 | 390 | 210 | Discussion-rich first-hand rejection, remediation, appeal, and repeated-review accounts across diverse subreddits |
| Apple Developer Forums and traditional developer forums | 170 | 100 | 70 | Contextual technical diagnoses, Apple staff clarification, implementation-specific cases |
| Amateur, no-code, AI-coding, and vibe-coder communities outside Reddit | 130 | 85 | 45 | First-app and abstraction-layer bottlenecks in Expo, FlutterFlow, Bubble, AI tooling, templates, and adjacent communities |
| Independent developer blogs and detailed public case studies | 50 | 35 | 15 | Long-form, outcome-confirmed timelines and appeal/resubmission narratives |
| Adaptive reserve | 50 | 40 | 10 | Gap filling, primary-source follow-up, recent-pattern validation, and unusually information-dense sources |
| **Total** | **1,000** | **650 maximum** | **350 maximum** | |

Pass 1 will stop no later than 650 consumed page-equivalents even if a source
bucket has unused capacity. Pass 2 will begin only after a yield, recency, and
taxonomy-gap review. The research may stop below 1,000 if saturation is
demonstrated and documented; it may never exceed 1,000.

### Accounting rules

- One successfully fetched public discussion page, article, post, comment page,
  forum thread, or other content URL costs one page-equivalent.
- A failed, blocked, deleted, irrelevant, or duplicate fetch still costs one
  page-equivalent if a remote fetch was attempted.
- A search/discovery request is not assumed free. Each search-result request
  costs at least one page-equivalent unless Firecrawl explicitly reports a
  higher cost, in which case the higher reported cost is recorded.
- A search mode that also hydrates full content is charged for the discovery
  request plus every hydrated page unless the tool explicitly reports a
  different, auditable cost.
- Local parsing, local analysis, URL canonicalization, and reading already
  fetched output cost zero.
- Multiple independent accounts on one fetched page receive separate case
  records but do not add page cost.
- The same canonical URL will not be remotely fetched twice unless a documented
  follow-up fetch is essential (for example, a different public comment view
  that contains the resolution). Such a fetch is charged again.
- The live counter is the sum of `page_cost` in
  `SeaView_Crawl_Ledger.jsonl`, not a retrospective estimate.
- Collection stops before any operation that could take the total above 1,000.

## Two-pass collection design

### Pass 1 — breadth (maximum 650 page-equivalents)

Pass 1 builds coverage across:

- source platforms and communities;
- exact publication dates and all four recency buckets;
- native, cross-platform, no-code, template, wrapper, and AI-assisted stacks;
- first submissions and app updates;
- app categories, including social/UGC, dating, health, finance, commerce,
  content, utilities, education, and AI;
- surface-level Apple guidelines and materially distinct underlying causes;
- one-time rejections, repeated rejections, appeals, clarifications, and
  approval outcomes.

Discovery will use query families rather than random URL crawling. Candidate
pages will be prioritized using title/snippet evidence of an actual Apple
message, first-hand account, remediation, outcome, appeal, explicit rejection
count, or substantive comment discussion.

### Pass 2 — gap filling and validation (maximum remaining balance)

Pass 2 will:

- retrieve the strongest unresolved or outcome-bearing threads discovered in
  pass 1;
- raise the share of qualifying accounts from 2024-07-17 onward toward at least
  50% when the public evidence permits;
- target weak taxonomy subtypes and underrepresented stacks or developer groups;
- investigate repeated-rejection mechanisms and failed fixes;
- seek reversal-through-appeal or approval-through-clarification evidence;
- validate high-impact patterns with sources from more than one community or
  platform;
- follow primary links when they materially improve root-cause confidence.

No second broad crawl is permitted merely to consume the budget.

## Query-family plan

Queries will combine ordinary developer language, Apple terminology, guideline
numbers, source/community constraints, outcome terms, and recent date terms.
Exact query strings and yield will be appended to the live methodology log.

### 1. Core rejection and outcome language

- `"App Store rejection"`, `"App Review rejected"`, `"rejected under
  guideline"`, `"first app rejected"`
- `"finally approved"`, `resubmitted`, `"approved after appeal"`, `"resolution
  center"`
- `"rejected twice"`, `"rejected 3 times"`, `"rejected multiple times"`,
  `"keeps getting rejected"`, `"how many times can I resubmit"`
- `"I don't understand the rejection"`, `"Apple says my app has no value"`,
  `"reviewer is testing the wrong thing"`

### 2. High-yield guideline families

- `Guideline 2.1` / App Completeness
- `Guideline 4.2` / minimum functionality / web wrapper
- `Guideline 4.3` / spam / repetitive / template / copycat
- `Guideline 5.1.1` / privacy / data collection / account deletion
- `Guideline 3.1.1` / IAP / external payments
- Sign in with Apple and other guideline numbers discovered from sources

### 3. Completeness, stability, and reviewer environment

- crash, hang, dead end, broken link, placeholder, incomplete feature
- demo account, expired credentials, reviewer cannot log in, login required
- backend outage, empty state, location-gated content, feature flag, remote
  configuration, TestFlight-versus-production differences
- missing review notes, hidden feature, hardware dependency, iPad-only failure

### 4. Privacy, permissions, SDKs, and tracking

- privacy labels, data-collection mismatch, privacy policy URL
- account deletion and end-to-end backend deletion
- purpose strings and timing for location, camera, microphone, contacts, photos,
  calendars, Bluetooth, speech, health, and motion
- privacy manifest, required-reason APIs, SDK signature
- ATT, tracking consent, third-party SDK compliance

### 5. Metadata and App Store Connect

- rejected screenshots, previews, descriptions, keywords, age rating,
  categories, icons, support/privacy URLs, promotional claims
- metadata mismatch, misleading functionality, review notes, localization,
  geographic availability, export compliance, encryption declarations

### 6. Payments and business models

- IAP, incomplete IAP, subscription disclosures, restore purchases, pricing
  display, external checkout, reader apps, digital versus physical goods,
  donations, contests, crypto, finance, gambling

### 7. UGC, safety, and sensitive app categories

- report/block/moderation, child safety, anonymous/random chat, dating/social
  discovery, objectionable content
- health, finance, regulated services, licensing, location restrictions
- AI chatbot, AI-generated content, content licensing

### 8. Product duplication, design, and technical packaging

- minimum functionality, webview/wrapper, template/generated app, repetitive
  apps, copycat/IP
- design/UX, accessibility, device compatibility, iPad/orientation, performance
- entitlements, background modes, notifications, private/deprecated APIs,
  archive/binary/build configuration, App Clips, extensions, widgets

### 9. Stack and developer-cohort searches

- Swift, SwiftUI, Objective-C
- React Native, Expo, Flutter, FlutterFlow, Bubble, Adalo, Glide
- ChatGPT, Claude, Cursor, Windsurf, Replit, Lovable, Bolt
- `"vibe coded"`, `"AI generated app"`, first-time developer, solo developer,
  indie developer, no-code, purchased template, source-code template

### 10. Review variability and reversal

- accepted after appeal without code change
- previously approved update rejected
- different reviewer, inconsistent review, reviewer misunderstanding
- screen recording, licence, explanation, credentials, or review-note
  clarification leading to approval

## Source and community strategy

### Reddit

Named communities will be validated before substantial allocation:
`r/iOSProgramming`, `r/swift`, `r/SwiftUI`, `r/iOSDevelopment`, `r/AppStore`,
`r/appledevelopers`, `r/indiedev`, `r/SideProject`, `r/startups`, `r/SaaS`,
`r/nocode`, `r/lowcode`, `r/FlutterDev`, `r/reactnative`, `r/expo`,
`r/FlutterFlow`, `r/Bubble`, `r/ChatGPTCoding`, `r/ClaudeAI`, `r/cursor`, and
`r/vibecoding`. Discovery will not presume that every named community exists,
is active, is indexed, or contains high-quality evidence. No single subreddit
may dominate simply because it is large.

The preferred research unit is the public thread with post plus substantive
comments. Comment-only pagination will be fetched only when snippets or links
indicate a likely diagnosis, OP resolution, explicit cycle count, Apple reply,
or materially contradictory experience.

### Other sources

- Apple Developer Forums, recording whether a response is by Apple staff,
  community expert, or ordinary developer when determinable
- Stack Overflow and specialist native/cross-platform forums
- Expo, Flutter, FlutterFlow, Bubble, and other public platform communities
- Hacker News, Indie Hackers, DEV, Medium, GitHub issues/discussions, public
  community archives, and developer blogs
- Generic SEO restatements of Apple's rules are excluded unless they contain a
  detailed first-hand case or a distinct market pain point

## Candidate selection and exclusion

A source is high priority when the accessible discovery evidence indicates one
or more of:

- an Apple guideline or closely paraphrased rejection message;
- an observable reviewer failure;
- a remediation attempt and later outcome;
- an explicit appeal or Resolution Center exchange;
- an explicit rejection/resubmission count;
- multiple independent first-hand accounts in substantive comments;
- a recent or newly enforced technical requirement;
- a specific no-code, AI-assisted, wrapper, or cross-platform bottleneck.

A fetched source may be marked irrelevant when it has only generic advice,
marketing content, job listings, App Store consumer complaints, Apple ID/payment
support questions unrelated to developer review, unverifiable snippets, jokes,
or no material App Review evidence.

Deleted, private, paywalled, or login-restricted content will not be
circumvented. Search snippets from inaccessible pages may inform future
queries, but they will be labelled unverified and excluded from quantitative
analysis.

## URL and story deduplication

Before each fetch:

1. Normalize the URL: lowercase host, remove fragments and tracking parameters,
   normalize trailing slashes, convert known Reddit share/short/mobile URLs to a
   canonical comments URL when the post identifier is available, and preserve
   meaningful query parameters only.
2. Check the canonical URL against `SeaView_Crawl_Ledger.jsonl`.
3. For Reddit, also check the post identifier; for forums, check thread/topic
   identifier; for syndicated articles, check canonical metadata when exposed.
4. Compare candidate title, app description, distinctive rejection wording,
   publication date, developer narrative, and outbound links against existing
   cases.
5. If multiple pages describe the same app/incident, assign the same
   `duplicate_group_id`, retain the strongest primary account, and do not count
   the duplicates as separate cases in statistics.

One page may generate multiple case records when clearly different developers
describe independent rejection incidents in comments. Each account retains its
parent URL and a locator note such as `original post`, `OP follow-up`, or
`substantive commenter`; usernames will not be copied unless identity is
material, intentionally public, and necessary.

## Date categorization

Dates are evaluated against 2026-07-17 using exact rolling boundaries:

| Bucket | Inclusive publication-date range |
|---|---|
| Most recent | 2025-07-17 through 2026-07-17 |
| Recent | 2024-07-17 through 2025-07-16 |
| Current-era | 2022-07-17 through 2024-07-16 |
| Historical but still informative | Before 2022-07-17 |
| Unknown | No reliable public publication date |

Within each rejection subtype, evidence will be ordered newest first. Historical
technical claims will be marked potentially obsolete unless separately
validated against current official requirements. The target is for at least
50% of final analysed rejection accounts to be published from 2024-07-17
onward, if enough qualifying evidence is publicly accessible.

## Evidence-quality grading

- **A** — first-hand developer account with an Apple rejection message (quoted
  or closely paraphrased), a confirmed remediation, and an approval outcome.
- **B** — first-hand account with substantial rejection and implementation
  detail, but incomplete remediation or outcome evidence.
- **C** — first-hand rejection claim with limited supporting detail.
- **D** — second-hand account, general commentary, unverified snippet, or advice
  without a verifiable rejection case.

Grade D records may support language, anxiety, hypotheses, substitute-product
analysis, or research gaps. They will not be used for frequencies,
rejection-cycle statistics, or market-size conclusions.

## Root-cause confidence

- **Confirmed** — Apple, the developer's explicit diagnosis, or a successful
  causal resubmission directly establishes the root cause.
- **Strongly supported** — detailed evidence plus the resolution makes the
  interpretation highly credible.
- **Plausible** — evidence supports the diagnosis, but another explanation
  remains viable.
- `null` — the source does not support a responsible root-cause inference.

The Apple guideline or rejection message is stored separately from the
underlying root cause. A broad classification such as Guideline 2.1 is never
treated as the root cause by itself.

## Live crawl ledger

`SeaView_Crawl_Ledger.jsonl` will contain one JSON object per remote operation
with this working schema:

```json
{
  "operation_id": "OP-0001",
  "operation_type": "search_or_fetch",
  "pass": 1,
  "bucket": "reddit",
  "query": null,
  "requested_url": null,
  "canonical_url": null,
  "fetched_at": "2026-07-17T00:00:00+05:30",
  "page_cost": 1,
  "fetch_status": "success",
  "http_or_tool_status": null,
  "title": null,
  "published_at": null,
  "relevance": "pending",
  "exclusion_reason": null,
  "duplicate_status": "unique_or_duplicate_or_not_applicable",
  "duplicate_of": null,
  "case_ids": [],
  "notes": null
}
```

Search-result candidates will be kept in a local discovery index with
canonicalized URLs and priority signals. Local discovery-index processing does
not consume page budget.

## Source ledger schema

`SeaView_Rejection_Source_Ledger.jsonl` will contain one normalized JSON object
per independent rejection account or meaningful non-rejection pain-point
source. Unknown values will be `null`, never inferred merely to fill fields.
The required core schema is:

```json
{
  "case_id": "SV-0001",
  "source_url": "https://example.com/thread",
  "source_platform": "Reddit",
  "community": "r/example",
  "title": "Example title",
  "published_at": "2026-01-01",
  "retrieved_at": "2026-07-17",
  "recency_bucket": "Most recent",
  "source_type": "rejection_account",
  "account_locator": "original post",
  "first_hand": true,
  "evidence_grade": "A",
  "developer_type": "first_time_solo",
  "solo_or_team": "solo",
  "app_category": "utility",
  "technology_stack": ["SwiftUI"],
  "submission_type": "first_submission",
  "apple_guideline": ["2.1"],
  "rejection_category": "reviewer_access",
  "rejection_subtype": "expired_demo_credentials",
  "rejection_message_summary": "Reviewer could not authenticate.",
  "product_feature_involved": "authentication",
  "reviewer_visible_failure": "Login failed with supplied credentials.",
  "confirmed_root_cause": "The demo account had expired.",
  "suspected_root_cause": null,
  "changes_before_resubmission": ["Created a persistent test account."],
  "attempted_fix": null,
  "successful_fix": "Created a persistent test account and updated review notes.",
  "apple_clarified": false,
  "appealed": false,
  "rejection_cycle_count": 1,
  "rejection_count_wording": null,
  "resubmission_count": 1,
  "distinct_problem_count": 1,
  "final_outcome": "approved",
  "time_to_approval": null,
  "screenshots_of_apple_message": false,
  "resolution_confirmed": true,
  "root_cause_confidence": "Confirmed",
  "pain_points": ["reviewer-state mismatch"],
  "seaview_opportunities": ["persistent test-account monitor"],
  "duplicate_group_id": null,
  "related_case_ids": [],
  "researcher_notes": null
}
```

Additional fields may be added consistently when the evidence supports them.

## Rejection-cycle extraction

The analysis will distinguish:

- number of explicit rejection messages/cycles;
- number of submitted builds or resubmissions;
- number of materially distinct underlying problems.

Exact integers will be recorded only when stated or unambiguously reconstructable.
Words such as `again`, `several`, `many`, or `keeps getting rejected` will be
preserved in `rejection_count_wording` and never converted into a number.
Statistics will identify their denominator and include only sufficiently
supported, non-duplicate grade A–C cases. Distribution bins are 1, 2, 3, 4, 5,
and more than 5 cycles. Mean, median, range, or percentiles will be calculated
only when the clean explicit-count sample is large enough to make them useful.

## Inductive taxonomy method

The provisional taxonomy starts broad but remains open:

- completeness/stability and reviewer access;
- authentication/account deletion;
- privacy, permissions, manifests, SDKs, and ATT;
- metadata and review communication;
- minimum functionality, wrappers, templates, spam, and IP;
- design, UX, compatibility, and accessibility;
- payments, subscriptions, IAP, and business-model classification;
- UGC, moderation, safety, social/dating, and objectionable content;
- regulated categories, legal/export/encryption, and regional availability;
- entitlements, protected resources, binary/build configuration, extensions,
  and runtime/environment differences;
- AI-generated content/implementation and content licensing.

A subtype is split when reviewer-visible symptoms, causal checks, required
evidence, or successful remediation materially differ. It is merged when only
the wording differs but the underlying failure and remediation are the same.
Each stable subtype will receive a taxonomy identifier such as `COMP-CRASH-001`
only after corpus analysis.

## Saturation and adaptation checks

At approximately every 100 analysed fetched pages, this methodology will record:

- cumulative page-equivalents;
- number of relevant unique threads;
- number of independent accounts;
- count of genuinely new rejection subtypes since the prior checkpoint;
- count of pages that only reinforce existing patterns;
- irrelevant/duplicate/blocked yield;
- most productive and noisiest query families;
- weak categories, stacks, dates, and communities;
- any budget reallocation and rationale.

A query family will be retired when two consecutive batches show poor relevant
yield and no new or stronger evidence. A source may receive more allocation only
while it adds new subtypes, recent high-quality cases, resolution evidence, or
needed cohort coverage.

Saturation sufficient to stop below 1,000 requires all of:

1. two consecutive roughly 100-page checkpoints with very few genuinely new
   material subtypes;
2. the major high-impact families supported by recent, traceable cases;
3. explicit targeting of remaining gaps producing mostly irrelevant,
   duplicate, or grade-D evidence;
4. sufficient clean cases for a useful, denominator-stated cycle analysis;
5. the unused balance and expected marginal value documented precisely.

## Analysis and quantitative controls

- Corpus frequency means frequency within this researched, self-selected
  corpus—not App Store-wide incidence.
- Every numerical claim will name its eligible sample and denominator.
- Duplicate stories will not be double-counted.
- Grade D material is excluded from frequencies, cycle statistics, and
  market-size conclusions.
- Official Apple requirements, Apple staff contextual guidance, repeated
  community precedent, disputed reviewer behaviour, and developer
  interpretation will be labelled separately.
- Contradictions will be retained and analysed rather than averaged away.
- Important claims in the report will cite source URLs and/or ledger case IDs.
- Quotes will be short, necessary, and faithfully reproduced; paraphrase is the
  default.

## Anticipated report structure

`SeaView_App_Store_Rejection_Research.md` will contain:

1. Executive synthesis
2. Methodology and corpus
3. Developer Pain-Point Map
4. Comprehensive Rejection Taxonomy
5. Most Recent Rejection Patterns
6. Rejection Cycle Analysis
7. Why Developers Fix the Wrong Thing
8. Amateur, No-Code, AI-Assisted, and Vibe-Coder Bottlenecks
9. Review Variability, Clarification, and Appeals
10. What Developers Currently Use Instead of SeaView
11. SeaView Market Opportunities
12. SeaView Knowledge and Evidence Requirements
13. Detection Coverage Matrix
14. False-Positive and False-Negative Risks
15. Research-Backed Product Principles
16. Open Questions for the Next Research Phase

The executive synthesis will be written last. The taxonomy will be the most
detailed section and will include stable identifiers, practical root causes,
reviewer symptoms, failed and successful fixes, within-corpus frequency,
high-quality case counts, newest supporting dates, confidence, and
representative case identifiers.

## Quality-control checklist

Before finalization:

- verify the crawl-ledger page sum is at most 1,000;
- reconcile fetched pages, irrelevant pages, unique threads, extracted
  accounts, duplicates, recency, platforms, communities, grades, and app
  categories;
- validate every quantitative denominator from the JSONL ledger;
- ensure every major subtype has a cited case or is labelled an unverified
  hypothesis;
- recalculate every recency bucket from absolute publication dates;
- warn where historical requirements may be obsolete;
- separate official policy from anecdotal precedent and reviewer variability;
- check duplicate groups and case counts;
- validate every JSONL line as standalone JSON;
- verify all report case identifiers exist in the source ledger;
- confirm only research files were created or modified.

## Execution log

No SeaView discovery request or source fetch had been made when the prospective
sections above were written.

