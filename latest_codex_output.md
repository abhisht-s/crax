### 1. Stage 3 implementation verdict

PASS at source level. The complete projected Tier 1 provider/capability machinery is implemented. The migration [versioned database change script] remains intentionally unapplied, so database behavior is not claimed as validated.

### 2. Files created

- [Stage 3 migration](/Users/abhisht/Documents/GitHub/ptg_productions/supabase/migrations/20260813170000_push_notification_tier1_stage3_attempt_metadata.sql:1)
- [Stage 3 validation SQL](/Users/abhisht/Documents/GitHub/ptg_productions/supabase/validation/20260813170000_push_notification_tier1_stage3_attempt_metadata_validation.sql:1)

No Markdown report was created.

### 3. Files modified

- [NotificationService.swift](/Users/abhisht/Documents/GitHub/ptg_productions/SocialEventsApp/Services/NotificationService.swift:715)
- [PushInstallationOwnershipTests.swift](/Users/abhisht/Documents/GitHub/ptg_productions/SocialEventsAppTests/PushInstallationOwnershipTests.swift:287)
- [notification-delivery-consumer.ts](/Users/abhisht/Documents/GitHub/ptg_productions/SocialEventsApp/media-worker/src/notification-delivery-consumer.ts:95)
- [notification-delivery-consumer.test.ts](/Users/abhisht/Documents/GitHub/ptg_productions/SocialEventsApp/media-worker/test/notification-delivery-consumer.test.ts:216)
- [notification-delivery-stage3-flow.test.ts](/Users/abhisht/Documents/GitHub/ptg_productions/SocialEventsApp/media-worker/test/notification-delivery-stage3-flow.test.ts:1770)
- [notification-stage4-b5-integration.test.ts](/Users/abhisht/Documents/GitHub/ptg_productions/SocialEventsApp/media-worker/test/notification-stage4-b5-integration.test.ts:36)

Neither `notification-apns.ts` nor `notification-fcm.ts` was modified.

### 4. Exact projected database schema changes

The migration adds:

- `push_device_tokens.direct_tap_analytics_version smallint NOT NULL DEFAULT 0` [a required small whole-number capability field], restricted to `0` or `1`; value `1` requires `platform = 'ios'`.
- `notification_attempts.provider text NULL` [an optional provider field with no default], restricted to `NULL`, `apns`, or `fcm`.
- `notification_attempts.direct_tap_analytics_version smallint NOT NULL DEFAULT 0`, restricted to `0` or `1`; value `1` requires non-null provider `apns`.

Evidence: [columns and constraints](/Users/abhisht/Documents/GitHub/ptg_productions/supabase/migrations/20260813170000_push_notification_tier1_stage3_attempt_metadata.sql:3).

It also projects these RPCs [database functions called through Supabase]:

- `register_push_device_token_v4(...)`: [line 181](/Users/abhisht/Documents/GitHub/ptg_productions/supabase/migrations/20260813170000_push_notification_tier1_stage3_attempt_metadata.sql:181)
- `get_notification_delivery_bundle_v2(uuid)`: [line 415](/Users/abhisht/Documents/GitHub/ptg_productions/supabase/migrations/20260813170000_push_notification_tier1_stage3_attempt_metadata.sql:415)
- `record_notification_attempt_v2(...)`: [line 600](/Users/abhisht/Documents/GitHub/ptg_productions/supabase/migrations/20260813170000_push_notification_tier1_stage3_attempt_metadata.sql:600)

No delivery-level capability column was added.

### 5. Exact registration v4 behavior

`register_push_device_token_v4` preserves V3 ownership and binding behavior [rules tying a token to the correct account and installation], including:

- Authenticated user and expected-user equality.
- Required installation ID, binding ID, and 32-byte revocation hash.
- Existing token, platform, environment, and bundle validation.
- Existing advisory transaction lock [database lock preventing concurrent installation-owner races].
- Existing installation-owner supersession and token upsert [insert-or-update] behavior.
- Capability accepts only `0` or `1`.
- Capability `1` is rejected unless normalized platform is `ios`.
- The exact capability is stored on both insert and re-registration.

### 6. Exact legacy registration compatibility

- V1 explicitly inserts or updates capability `0`: [line 82](/Users/abhisht/Documents/GitHub/ptg_productions/supabase/migrations/20260813170000_push_notification_tier1_stage3_attempt_metadata.sql:82).
- V3 delegates to V4 with capability `0`: [line 345](/Users/abhisht/Documents/GitHub/ptg_productions/supabase/migrations/20260813170000_push_notification_tier1_stage3_attempt_metadata.sql:345).
- V2 preserves its legacy binding behavior while registering capability `0`: [line 375](/Users/abhisht/Documents/GitHub/ptg_productions/supabase/migrations/20260813170000_push_notification_tier1_stage3_attempt_metadata.sql:375).
- Therefore an old build re-registering a capability-1 token explicitly downgrades it to `0`.

The iOS fallback sequence is V4 → V3 → V2. Fallback occurs only for the established missing-function conditions: `PGRST202`, `42883`, or a function-specific “not found/could not find” response. Network, validation, authentication, permission, and other failures do not downgrade: [NotificationService.swift](/Users/abhisht/Documents/GitHub/ptg_productions/SocialEventsApp/Services/NotificationService.swift:728).

### 7. Exact bundle v2 behavior

`get_notification_delivery_bundle_v2` preserves the current bundle selection, eligibility, preferences, token ordering, and token fields. It adds `direct_tap_analytics_version` to each exact token JSON object [structured token record]: [migration](/Users/abhisht/Documents/GitHub/ptg_productions/supabase/migrations/20260813170000_push_notification_tier1_stage3_attempt_metadata.sql:508).

The worker:

- Requests V2 first.
- Falls back to V1 only when V2 is genuinely missing.
- Treats every V1 token as capability `0`.
- Treats missing or malformed capability metadata as `0`, ensuring observational metadata cannot block sending.
- Forces Android/FCM [Firebase Cloud Messaging] capability to `0`.

Evidence: [parsing](/Users/abhisht/Documents/GitHub/ptg_productions/SocialEventsApp/media-worker/src/notification-delivery-consumer.ts:633) and [bundle fallback](/Users/abhisht/Documents/GitHub/ptg_productions/SocialEventsApp/media-worker/src/notification-delivery-consumer.ts:3492).

### 8. Exact attempt v2 behavior

`record_notification_attempt_v2` preserves the legacy attempt-number, timestamps, success, retry, deactivation, payload-preview, and delivery attempt-count behavior.

It additionally persists:

- `provider` from the actual `ProviderSendResult.provider`.
- Capability from the exact token involved in that operation.

Evidence: [worker persistence](/Users/abhisht/Documents/GitHub/ptg_productions/SocialEventsApp/media-worker/src/notification-delivery-consumer.ts:3278).

Fallback behavior:

- V1 bundle path directly uses legacy attempt persistence, producing provider `NULL` and capability `0`.
- If V2 bundle succeeds but attempt V2 is genuinely missing, the worker uses legacy persistence and keeps delivery behavior intact.
- Real attempt-persistence failures do not masquerade as missing-function fallbacks.

The projected rows support the later rule: at least one successful attempt, with every successful attempt being APNs [Apple Push Notification service] and capability `1`. Failed legacy, Android, or unknown attempts do not disqualify the delivery.

### 9. Exact iOS code changes

The iOS token registration now calls V4 with:

- Existing expected user, installation, token, environment, bundle, binding, and revocation-hash values.
- `p_platform = "ios"`.
- `p_direct_tap_analytics_version = 1`.

Evidence: [NotificationService.swift](/Users/abhisht/Documents/GitHub/ptg_productions/SocialEventsApp/Services/NotificationService.swift:715).

No permission, APNs registration, reconciliation, binding promotion, logout, account-switching, ownership, or cleanup flow was changed. No tap recorder was attached.

### 10. Exact worker changes

The worker now:

- Carries capability per token without merging it across devices.
- Tries bundle V2 before V1.
- Uses actual APNs/FCM result provider at attempt persistence.
- Tries attempt V2 before its safe legacy fallback.
- Keeps capability out of provider selection, payload construction, sending, skipping, routing, retries, cleanup, acknowledgement, and final delivery-state decisions.

The capability only appears in parsing and attempt persistence: [token metadata](/Users/abhisht/Documents/GitHub/ptg_productions/SocialEventsApp/media-worker/src/notification-delivery-consumer.ts:95), [attempt call](/Users/abhisht/Documents/GitHub/ptg_productions/SocialEventsApp/media-worker/src/notification-delivery-consumer.ts:3308).

### 11. Historical-row treatment

No historical reconstruction or inferential backfill was added.

After eventual application:

- Historical token registrations read as capability `0`.
- Historical attempts read as provider `NULL` and capability `0`.
- No provider is inferred from topic, payload preview, token joins, or current platform.
- Historical attempts remain analytically ineligible.

### 12. Test/validation source changes

Swift test sources now cover:

- V4 RPC name and capability `1`.
- Binding/ownership preservation.
- Missing V4 → V3 fallback.
- Missing V4 and V3 → existing V2 fallback.
- Network failure does not trigger fallback.

Evidence: [PushInstallationOwnershipTests.swift](/Users/abhisht/Documents/GitHub/ptg_productions/SocialEventsAppTests/PushInstallationOwnershipTests.swift:287).

Worker tests now cover:

- V2 per-token capability parsing.
- V1 capability `0`.
- APNs provider/capability persistence.
- FCM provider persistence without changing FCM behavior.
- Missing attempt V2 fallback.
- Correct token-to-capability pairing across multiple devices.

Evidence: [parser tests](/Users/abhisht/Documents/GitHub/ptg_productions/SocialEventsApp/media-worker/test/notification-delivery-consumer.test.ts:216), [provider/fallback tests](/Users/abhisht/Documents/GitHub/ptg_productions/SocialEventsApp/media-worker/test/notification-delivery-stage3-flow.test.ts:1770).

The rollback-contained validation SQL covers all requested schema, constraint, registration, bundle, attempt, multi-device, success/failure, and grant assertions, ending with `ROLLBACK`: [validation SQL](/Users/abhisht/Documents/GitHub/ptg_productions/supabase/validation/20260813170000_push_notification_tier1_stage3_attempt_metadata_validation.sql:1).

### 13. Validation actually run

- Read-only Production schema/function/grant inspection through Supabase.
- Final read-only proof found `0` Stage 3 columns and all three new RPCs absent, confirming the migration remains unapplied.
- TypeScript type check [worker source consistency check]: passed.
- Focused token parsing tests: 29 passed.
- Focused provider/persistence tests: 22 passed.
- Focused existing worker integration tests: 9 passed.
- `git diff --check` [whitespace/conflict-marker inspection]: passed.
- No dependencies were installed or fetched.

### 14. Validation intentionally not run

- Migration SQL was not executed.
- Validation SQL was not executed.
- No DEV or Production fixture writes were performed.
- No Xcode [Apple build/test tool] build or test was run.
- No broad worker test suite was run.
- No deployment or `supabase db push` was run.

Therefore database validation is pending by design.

### 15. Verification audit findings

- Migration is additive: no `DROP` or `TRUNCATE`; no legacy RPC was removed.
- Legacy downgrade semantics are explicit.
- V4 cannot mark Android capability `1`.
- Attempt capability `1` cannot be persisted with FCM.
- Capability is observational only.
- Provider comes from the actual provider result.
- Legacy fallbacks preserve delivery behavior.
- APNs/FCM payload constructors and sender files are untouched.
- No delivery-level capability field exists.
- No tap recorder, PostHog view, or analytics denominator was implemented.

Mechanical adjustment: required V4 capability and attempt-V2 provider/capability parameters are placed before existing defaulted parameters because PostgreSQL [the database engine] requires required parameters to precede parameters with defaults. Semantics are unchanged.

### 16. Dirty-tree preservation

All unrelated dirty-tree work remains present and untouched, including PostHog changes, bump work, unrelated migrations, root SQL files, reports, and assets.

`NotificationService.swift` also contains unrelated StoreKit/SKAdNetwork changes outside the Stage 3 registration hunk; they were preserved and not attributed to this implementation.

### 17. Any blocker before Stage 4

No product-contract ambiguity or source-level blocker was found. Stage 4 may reason against this projected schema.

The intentional operational gate remains: before any real rollout, the user must manually apply the reviewed migration and then manually run the validation SQL.

### 18. Explicit confirmation that no migration was applied and no PostHog/Supabase/AWS/deployment state changed

Confirmed:

- No migration was applied to DEV or Production.
- No Supabase database state changed; only read-only inspection queries ran.
- No PostHog state or PostHog code was changed by this stage.
- No AWS [Amazon Web Services] state or code was changed.
- No deployment occurred.
- `supabase db push` was never run.
