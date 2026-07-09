# Watch to Codex — Current Technical State Handoff

## 1. Executive Summary

[Code-confirmed] This repository is a local Python orchestration prototype for a supervised agent loop on macOS. The README describes it as a "Local supervised agent loop prototype" and includes ledger, Codex execution, ChatGPT Desktop interaction, capture, and supervision commands.

[Design decision] The broader direction is Codex/ChatGPT orchestration: Codex executes work locally, ChatGPT Desktop can provide review/next prompts, and the local controller records evidence and gates risky operations.

[Code-confirmed] The currently completed foundational capability in scope here is reliable navigation to an exact ChatGPT Desktop project and an exact named project chat through `agent/chatgpt_navigation_diagnostic.py` and CLI wiring in `agent/cli.py`.

[Design decision] The core primitive is:

```text
open exact ChatGPT project
-> find exact named chat
-> open exact conversation
-> verify or fail closed
```

[Design decision] This primitive is foundational for future multi-workstream orchestration because every future workstream must be able to reacquire its exact ChatGPT project/chat before sending or reading anything.

## 2. Product Direction and Intended End State

[Design decision] Intended workstream model:

```text
workstream =
  repository / Codex session
  + exact ChatGPT project
  + exact ChatGPT chat
  + user-provided task statement
  + isolated run state, evidence, approvals, recovery state
```

[Design decision] Multiple Codex sessions may eventually run concurrently because local repository work can be isolated by worktree, repo, or process state.

[Design decision] One ChatGPT Desktop UI lane must remain serialized because ChatGPT Desktop exposes a single interactive UI surface; concurrent UI actions would race focus, selection, scrolling, and AX state.

[Design decision] Every workstream must reacquire and verify its exact project/chat. It must not assume the UI remains on the correct project/chat after another workstream used the serialized ChatGPT Desktop lane.

[Code-confirmed] The scheduler/workstream layer is not the current implementation target in this code. Current implementation focuses on exact project/chat navigation, diagnostics, local controller services, ledger, Codex execution, and ChatGPT capture/submission primitives. There is no current multi-workstream scheduler object in the inspected files.

## 3. Current Repository Architecture Relevant to This Work

[Code-confirmed] `agent/cli.py` owns command-line parser wiring and result printers. Relevant symbols include `_build_parser`, `_print_inspect_chatgpt_project_visible_chats_result`, `_print_diagnose_chatgpt_project_chat_rows_result`, `_print_open_chatgpt_project_chat_result`, and the command dispatch blocks around `inspect-chatgpt-project-visible-chats`, `diagnose-chatgpt-project-chat-rows`, and `open-chatgpt-project-chat` (`agent/cli.py:431`, `agent/cli.py:499`, `agent/cli.py:535`, `agent/cli.py:4450`, `agent/cli.py:4489`, `agent/cli.py:4508`).

[Code-confirmed] `agent/chatgpt_navigation_diagnostic.py` owns ChatGPT Desktop AX inspection, exact project identity resolution, project Chats-list identity gates, chat row extraction, scroll search, action planning, AXPress/geometry click behavior, target alignment, and post-action verification. Key symbols include `resolve_open_project_content_and_visible_chats`, `_visible_project_chat_rows`, `_project_chat_accessibility_text_parts`, `open_chatgpt_sidebar_destination`, `open_chatgpt_project_chat`, `_bounded_project_chat_scroll_search`, `_open_ready_project_chat_plan`, `_attempt_project_chat_target_alignment`, `_project_chat_post_action_inspection`, `_AutonomousSidebarAXReader.perform_action`, and `_project_chat_alignment_policy_conditions_satisfied`.

[Test-confirmed] `tests/test_chatgpt_navigation_diagnostic.py` is the main validation area for this work. It includes parser tests, resolver tests, strict matching tests, scrolling/hydration tests, target-alignment tests, action authorization tests, and fail-closed tests.

[Code-confirmed] Adjacent systems exist but were out of scope for this navigation work: Codex execution (`agent/codex_services.py`, `agent/codex_terminal.py`), event ledger (`agent/ledger.py`), local controller/server (`agent/local_controller.py`, `agent/local_server.py`), browser/static UI files (`agent/web_static/*`), macOS app control (`agent/mac_app_control.py`), paste/capture services (`agent/chatgpt_services.py`, `agent/chatgpt_ax_capture.py`).

## 4. Completed Capability: Exact Project and Exact Chat Navigation

[Code-confirmed] Primary available commands and options are:

```sh
python -m agent.cli open-chatgpt-sidebar-destination \
  --kind project \
  --title "PTG Assistant" \
  --confirm-open-destination

python -m agent.cli inspect-chatgpt-project-visible-chats \
  --project-title "PTG Assistant"

python -m agent.cli diagnose-chatgpt-project-chat-rows \
  --project-title "PTG Assistant"

python -m agent.cli diagnose-chatgpt-project-chat-rows \
  --project-title "PTG Assistant" \
  --contains-title "Mock Data Insertion SQL"

python -m agent.cli open-chatgpt-project-chat \
  --project-title "PTG Assistant" \
  --chat-title "Mock Data Insertion SQL" \
  --confirm-open-chat
```

[Code-confirmed] `open-chatgpt-sidebar-destination` is gated by `--confirm-open-destination` before activation/action (`agent/cli.py:347`, `agent/cli.py:367`, `agent/cli.py:4411`).

[Code-confirmed] `inspect-chatgpt-project-visible-chats` is read-only and does not activate, click, scroll, press keys, paste, or open chats (`agent/cli.py:431`).

[Code-confirmed] `diagnose-chatgpt-project-chat-rows` is read-only and supports `--contains-title`; filtering affects diagnostic output, not collection or matching (`agent/cli.py:499`, `agent/cli.py:512`).

[Code-confirmed] `open-chatgpt-project-chat` opens/confirms the exact project first, then resolves and opens the exact chat only when `--confirm-open-chat` is supplied (`agent/cli.py:535`, `agent/cli.py:554`, `agent/cli.py:4508`).

## 5. Exact Navigation State Machine

[Code-confirmed] Real chronological state machine:

```text
activate/resolve ChatGPT window when required
-> identify exact requested project
-> confirm project identity
-> confirm Chats tab and valid Chats-list container
-> enumerate normalized visible chat rows
-> emit newly discovered canonical titles
-> exact-match requested title
-> scroll only while target is absent
-> preserve overlap/continuity
-> stop immediately at exact target detection
-> fresh re-resolve exact target
-> align partially clipped target through AXScrollToVisible when authorized
-> fresh re-resolve again
-> AXPress first
-> validated geometry-click fallback only if allowed/needed
-> post-action conversation verification or explicit fail-closed outcome
```

[Code-confirmed] Initial visible target path: `_fresh_project_chat_targeting_plan` builds a plan from a fresh AX tree; `_project_chat_target_detection_from_plan` marks the exact target; `_open_ready_project_chat_plan` re-resolves before action and prefers AXPress (`agent/chatgpt_navigation_diagnostic.py:6694`, `agent/chatgpt_navigation_diagnostic.py:4981`, `agent/chatgpt_navigation_diagnostic.py:5118`).

[Test-confirmed] Initial visible target opens without scroll and records zero scroll pulses (`tests/test_chatgpt_navigation_diagnostic.py:2797`, `tests/test_chatgpt_navigation_diagnostic.py:3893`).

[Code-confirmed] Offscreen target path: if initial targeting is `chat_not_currently_visible`, `_bounded_project_chat_scroll_search` scrolls the confirmed list and checks target detection during pre-scroll, hydration, settled, and recovery phases (`agent/chatgpt_navigation_diagnostic.py:4752`, `agent/chatgpt_navigation_diagnostic.py:5460`).

[Test-confirmed] Offscreen target is found after one or multiple controlled scrolls and opened via AXPress (`tests/test_chatgpt_navigation_diagnostic.py:2822`, `tests/test_chatgpt_navigation_diagnostic.py:2851`).

[Code-confirmed] Partially clipped target path: `_open_ready_project_chat_plan` calls `_attempt_project_chat_target_alignment` only after exact target detection and failed fresh ready status; alignment can perform one `AXScrollToVisible` on the freshly resolved exact target, then fresh-resolve again (`agent/chatgpt_navigation_diagnostic.py:5148`, `agent/chatgpt_navigation_diagnostic.py:5289`).

[Test-confirmed] Partially clipped target alignment posts exactly one `AXScrollToVisible`, then actions the fresh fully visible row, not the stale clipped row (`tests/test_chatgpt_navigation_diagnostic.py:3459`).

[Code-confirmed] Target disappears before fresh re-resolution path: if exact detection occurred but the fresh plan is not ready and alignment cannot handle it, outcome is `target_detected_but_not_stably_re_resolved` (`agent/chatgpt_navigation_diagnostic.py:5169`).

[Test-confirmed] Disappearing target fails closed without AXPress (`tests/test_chatgpt_navigation_diagnostic.py:4043`).

[Code-confirmed] Ambiguous/duplicate title path: more than one matching row returns `chat_title_ambiguous` (`agent/chatgpt_navigation_diagnostic.py:6817`).

[Test-confirmed] Duplicate visible titles and duplicate accessibility-prefix matches fail closed without actions (`tests/test_chatgpt_navigation_diagnostic.py:2576`, `tests/test_chatgpt_navigation_diagnostic.py:2754`).

[Code-confirmed] Absent target path: absence remains `chat_not_currently_visible`, `chat_list_end_reached_without_match`, `chat_list_scroll_no_progress`, `chat_search_budget_exhausted_without_confirmed_end`, or `chat_search_time_budget_exhausted_while_list_progressing` depending on scan evidence and budgets (`agent/chatgpt_navigation_diagnostic.py:180`, `agent/chatgpt_navigation_diagnostic.py:5847`).

[Code-confirmed] Project/list identity failure path: if project identity or Chats-list identity is not confirmed, no targeting/action proceeds (`agent/chatgpt_navigation_diagnostic.py:3233`, `agent/chatgpt_navigation_diagnostic.py:3310`, `agent/chatgpt_navigation_diagnostic.py:4699`).

## 6. Chat Row Discovery and Normalization Model

[Code-confirmed] ChatGPT project chats are resolved as row-sized elements under a confirmed Chats-list content provider. The resolver accepts row-like roles/actions including `AXButton`, `AXGroup`, `AXCell`, `AXRow`, and `AXLink`, and requires row geometry/list identity (`agent/chatgpt_navigation_diagnostic.py:3839`, `agent/chatgpt_navigation_diagnostic.py:3897`).

[Code-confirmed] Historical/current ChatGPT Desktop project chat rows can be exposed as `AXButton` rows where `AXDescription` contains `"Chat Title, preview text..."`. `_project_chat_accessibility_text_parts` parses `AXTitle` first, then `AXDescription`, then eligible `AXValue` (`agent/chatgpt_navigation_diagnostic.py:4055`).

[Code-confirmed] Canonical normalization order:

```text
AXTitle when present
-> otherwise AXDescription
-> otherwise AXValue where appropriate
```

[Code-confirmed] For merged descriptions, `_project_chat_accessibility_text_parts` splits at the first `", "`:

```text
"Mock Data Insertion SQL, How should we add mock data..."
-> canonical title: "Mock Data Insertion SQL"
-> preview: "How should we add mock data..."
```

[Code-confirmed] Title eligibility filters apply to the canonical title only. Preview suffixes are truncated but not used to reject a row after canonical extraction (`agent/chatgpt_navigation_diagnostic.py:4072`, `agent/chatgpt_navigation_diagnostic.py:4106`).

[Test-confirmed] SQL, code, JSON, URLs, logs/message-like prose in preview content do not reject a valid `"Mock Data Insertion SQL"` row (`tests/test_chatgpt_navigation_diagnostic.py:3262`, `tests/test_chatgpt_navigation_diagnostic.py:3276`, `tests/test_chatgpt_navigation_diagnostic.py:3288`).

[Code-confirmed] Strict match behavior is exact canonical title match, exact accessibility text match, or exact requested prefix before `", "` when the requested title itself does not contain a comma (`agent/chatgpt_navigation_diagnostic.py:6889`).

[Code-confirmed] Current comma-title policy: if the requested chat title contains a comma, it only matches when the canonical title equals the request and the source attribute is explicit `AXTitle`; description-prefix matching is rejected as not unambiguously representable (`agent/chatgpt_navigation_diagnostic.py:6894`).

[Test-confirmed] Comma-containing requested titles fail closed without explicit `AXTitle`, but exact explicit `AXTitle` can match (`tests/test_chatgpt_navigation_diagnostic.py:3694`).

## 7. Scrolling, Coverage, and Continuity Model

[Code-confirmed] Scrolling is scoped to the confirmed project Chats-list. `_project_chat_scroll_target` derives a scroll target from the resolved list path and its ancestors, and rejects targets not containing current row frames (`agent/chatgpt_navigation_diagnostic.py:6549`).

[Code-confirmed] CoreGraphics micro-scroll fallback posts at a point inside the confirmed list viewport and inside ChatGPT/display bounds (`agent/chatgpt_navigation_diagnostic.py:6646`, `agent/chatgpt_navigation_diagnostic.py:6661`).

[Code-confirmed] Where CoreGraphics scrolling is used, delta is derived from median visible row height via `_project_chat_computed_scroll_delta_y`, with min/max clamps, instead of a fixed large pulse (`agent/chatgpt_navigation_diagnostic.py:6620`, `agent/chatgpt_navigation_diagnostic.py:6630`).

[Test-confirmed] The focused test proves a row-height-derived `-49` delta for 65px rows and rejects the legacy `-360` behavior (`tests/test_chatgpt_navigation_diagnostic.py:4130`).

[Code-confirmed] Continuity is based on normalized row text, not AX paths, because AX paths can churn under virtualized lists. `_project_chat_longest_contiguous_overlap` uses ordered row text overlap (`agent/chatgpt_navigation_diagnostic.py:6371`).

[Test-confirmed] Path changes do not break overlap identity when row text is the same (`tests/test_chatgpt_navigation_diagnostic.py:4159`).

[Code-confirmed] If CoreGraphics scrolling jumps without sufficient contiguous overlap, `_recover_project_chat_scan_continuity` posts bounded reverse micro-scroll pulses and rechecks overlap/target detection (`agent/chatgpt_navigation_diagnostic.py:5905`).

[Test-confirmed] Overlap gaps trigger recovery rather than blind forward scrolling; insufficient continuity does not conclude not-found (`tests/test_chatgpt_navigation_diagnostic.py:4185`, `tests/test_chatgpt_navigation_diagnostic.py:4231`).

[Code-confirmed] Hydration/reset handling happens in `_observe_project_chat_list_hydration`, which samples fresh resolver snapshots between sleeps and distinguishes list advancement, reset, unavailability, stability, and target detection (`agent/chatgpt_navigation_diagnostic.py:6098`).

[Test-confirmed] Hydration uses fresh resolver snapshots, resets stability counters on viewport changes, and does not post a next scroll while the list is still hydrating (`tests/test_chatgpt_navigation_diagnostic.py:3050`, `tests/test_chatgpt_navigation_diagnostic.py:3166`, `tests/test_chatgpt_navigation_diagnostic.py:3201`).

[Code-confirmed] Discovered canonical titles accumulate in `_project_chat_discovery_state` and `_emit_project_chat_discovered_titles`; duplicate titles are printed once (`agent/chatgpt_navigation_diagnostic.py:4934`, `agent/chatgpt_navigation_diagnostic.py:5037`).

[Test-confirmed] Discovery output prints canonical titles only, appends newly exposed chats once, and excludes SQL previews/composer/transcript/sidebar controls (`tests/test_chatgpt_navigation_diagnostic.py:3346`, `tests/test_chatgpt_navigation_diagnostic.py:3818`, `tests/test_chatgpt_navigation_diagnostic.py:3842`, `tests/test_chatgpt_navigation_diagnostic.py:3866`).

[Code-confirmed] Search is bounded by `MAX_PROJECT_CHAT_SEARCH_CYCLES = 60` and `MAX_PROJECT_CHAT_SEARCH_ELAPSED_SECONDS = 90.0`; no mathematically complete coverage is claimed unless end evidence/anchor conditions confirm it (`agent/chatgpt_navigation_diagnostic.py:180`, `agent/chatgpt_navigation_diagnostic.py:5847`).

[Code-confirmed] Scanner stops immediately once exact target detection occurs and returns `scroll_pulses_after_target_detection: 0` (`agent/chatgpt_navigation_diagnostic.py:5561`, `agent/chatgpt_navigation_diagnostic.py:5617`, `agent/chatgpt_navigation_diagnostic.py:5715`, `agent/chatgpt_navigation_diagnostic.py:5819`).

[Test-confirmed] Target detection during initial, hydration, settled, and recovery states posts zero additional scrolls after detection (`tests/test_chatgpt_navigation_diagnostic.py:3893`, `tests/test_chatgpt_navigation_diagnostic.py:3917`, `tests/test_chatgpt_navigation_diagnostic.py:3946`, `tests/test_chatgpt_navigation_diagnostic.py:4024`).

[Code-confirmed] Relevant current output fields include `scroll_iterations_attempted`, `search_cycles_attempted`, `scroll_pulses_posted`, `scan_continuity`, `recovery_scroll_pulses_posted`, `target_exact_match_detected`, `target_detected_in`, `target_detected_cycle`, `scroll_pulses_after_target_detection`, `unique_chat_titles_printed`, `fresh_target_re_resolution_confirmed`, `target_alignment_required`, `target_alignment_method`, and `target_alignment_posted` (`agent/chatgpt_navigation_diagnostic.py:4793`, `agent/cli.py:1959`, `agent/cli.py:1981`).

## 8. Partially Clipped Target Alignment

[Code-confirmed] A target can be found while partially clipped at the list edge; row visibility is reported as `fully_visible` or `partially_clipped` based on row containment in the viewport (`agent/chatgpt_navigation_diagnostic.py:4334`).

[Code-confirmed] A partially clipped row may have a safe click center outside the visible list viewport; `_project_chat_row_interactable` and `_project_chat_validated_click_plan` require points to be inside both row/title frame and viewport, so geometry click fails closed (`agent/chatgpt_navigation_diagnostic.py:6870`, `agent/chatgpt_navigation_diagnostic.py:7068`).

[Code-confirmed] Accepted behavior is one semantic `AXScrollToVisible` on the exact freshly resolved target only. `_attempt_project_chat_target_alignment` requires prior exact detection, fresh row resolution, `partially_clipped` visibility, `AXScrollToVisible` on the row, and no prior alignment in this command (`agent/chatgpt_navigation_diagnostic.py:5289`, `agent/chatgpt_navigation_diagnostic.py:5406`, `agent/chatgpt_navigation_diagnostic.py:5425`).

[Design decision] This alignment is target alignment, not resumed discovery scanning. After alignment, code takes a fresh targeting plan, requires re-resolution/full safe actionability, and then proceeds through AXPress first (`agent/chatgpt_navigation_diagnostic.py:5356`, `agent/chatgpt_navigation_diagnostic.py:5369`, `agent/chatgpt_navigation_diagnostic.py:5191`).

[Code-confirmed] Only one alignment attempt is permitted per command execution. A second attempt returns `target_alignment_not_supported` before dispatch (`agent/chatgpt_navigation_diagnostic.py:5302`).

[Code-confirmed] `AXShowMenu` and arbitrary AX actions remain blocked by `_AutonomousSidebarAXReader.perform_action`, which permits only `AXPress` and narrowly authorized `AXScrollToVisible` (`agent/chatgpt_navigation_diagnostic.py:9317`).

[Test-confirmed] Dispatcher tests allow `AXScrollToVisible` only with exact target alignment context and reject missing context, sidebar project button context, missing exact-title evidence, `AXShowMenu`, and second alignment attempts (`tests/test_chatgpt_navigation_diagnostic.py:3369`, `tests/test_chatgpt_navigation_diagnostic.py:3376`, `tests/test_chatgpt_navigation_diagnostic.py:3384`, `tests/test_chatgpt_navigation_diagnostic.py:3393`, `tests/test_chatgpt_navigation_diagnostic.py:3402`, `tests/test_chatgpt_navigation_diagnostic.py:3410`).

## 9. Action Model and Safety Boundaries

[Code-confirmed] AXPress is preferred for both project/sidebar destination opening and project-chat opening (`agent/chatgpt_navigation_diagnostic.py:4529`, `agent/chatgpt_navigation_diagnostic.py:5191`).

[Code-confirmed] Geometry click is a constrained fallback after fresh resolution, safe point computation, bounds checks, and AX hit-test relationship verification (`agent/chatgpt_navigation_diagnostic.py:5220`, `agent/chatgpt_navigation_diagnostic.py:7051`).

[Code-confirmed] Action points must be inside the target row/title frame, confirmed list viewport, ChatGPT WindowServer bounds, and display bounds (`agent/chatgpt_navigation_diagnostic.py:6866`, `agent/chatgpt_navigation_diagnostic.py:7064`).

[Code-confirmed] No stale path/frame may be used after scrolling or alignment. `_open_ready_project_chat_plan` re-resolves before action; fallback click uses another stable fresh plan and fails if row path/frame materially changed (`agent/chatgpt_navigation_diagnostic.py:5139`, `agent/chatgpt_navigation_diagnostic.py:5220`, `agent/chatgpt_navigation_diagnostic.py:5234`).

[Test-confirmed] Fresh rows after scroll are used; stale/offscreen pre-scroll rows are not actioned (`tests/test_chatgpt_navigation_diagnostic.py:3759`).

[Test-confirmed] The project-chat open source slice has no cursor read, keyboard navigation, text entry, paste, OCR, screenshots, browser automation, or broad AX action channels (`tests/test_chatgpt_navigation_diagnostic.py:4388`).

[Code-confirmed] Unknown or unsupported action targets fail closed through status outcomes such as `chat_row_not_interactable`, `safe_click_point_unavailable`, `calculated_point_hit_test_mismatch`, `target_alignment_not_supported`, or `action_posted_but_chat_not_confirmed`.

[Code-confirmed] `AXScrollToVisible` is allowed only in exact-target alignment context (`agent/chatgpt_navigation_diagnostic.py:9332`, `agent/chatgpt_navigation_diagnostic.py:5425`).

## 10. Post-Action Verification Status

[Code-confirmed] `_project_chat_post_action_inspection` attempts to prove active conversation-open success after AXPress/click. It waits briefly, collects stable ChatGPT AX/window geometry, resolves whether the project Chats list is still primary content, and extracts verification signals (`agent/chatgpt_navigation_diagnostic.py:7101`).

[Code-confirmed] Evidence signals include `requested_project_chat_row_selected_or_focused`, `active_conversation_identity_outside_chat_list`, `conversation_structure_present`, `project_chat_list_not_primary_content`, and `main_region_layout_materially_changed` (`agent/chatgpt_navigation_diagnostic.py:7161`).

[Code-confirmed] Confirmation requires active conversation identity outside the chat list plus conversation structure/list-not-primary/selected evidence, or selected/focused row plus conversation structure (`agent/chatgpt_navigation_diagnostic.py:7137`).

[Test-confirmed] Success is not reported without post-action evidence; an action can be posted and still return `action_posted_but_chat_not_confirmed` (`tests/test_chatgpt_navigation_diagnostic.py:4360`).

[Open question] The requested handoff mentions a known prior post-action verifier false negative. The repository does not contain a durable live trace proving that false negative is fully solved. Treat post-action verifier hardening as still open unless a future live trace proves otherwise.

[Design decision] Action success and confirmed conversation-open success are intentionally separate. `actions_performed` can show AXPress/click was posted, while `ok: true` requires verifier confirmation.

## 11. Diagnostics and Debugging Workflow

[Code-confirmed] Project/list identity diagnostics:

```sh
python -m agent.cli inspect-chatgpt-project-visible-chats \
  --project-title "PTG Assistant"
```

[Code-confirmed] Visible row diagnostics:

```sh
python -m agent.cli diagnose-chatgpt-project-chat-rows \
  --project-title "PTG Assistant"

python -m agent.cli diagnose-chatgpt-project-chat-rows \
  --project-title "PTG Assistant" \
  --contains-title "Mock Data Insertion SQL"
```

[Code-confirmed] Bounded exact row AX audit also exists, although it was not in the primary command list:

```sh
python -m agent.cli inspect-chatgpt-project-chat-row-ax \
  --project-title "PTG Assistant" \
  --chat-title "Mock Data Insertion SQL"
```

[Code-confirmed] Important fields to inspect include AX role, AX path, frame, `AXTitle`, `AXDescription`, `AXValue`, AX actions, canonical title/current resolver title, accepted/rejected reason, viewport intersection, and row visibility. The printers expose these in `_print_inspect_chatgpt_project_visible_chats_result`, `_print_inspect_chatgpt_project_chat_row_ax_result`, and `_print_diagnose_chatgpt_project_chat_rows_result` (`agent/cli.py:1691`, `agent/cli.py:1761`, `agent/cli.py:1811`).

[Test-confirmed] Diagnostic commands remain read-only and do not activate, navigate, click, scroll, AXPress, type, paste, use cursor APIs, OCR, screenshots, browser automation, or persistent writes (`tests/test_chatgpt_navigation_diagnostic.py:2397`, `tests/test_chatgpt_navigation_diagnostic.py:4427`, `tests/test_chatgpt_navigation_diagnostic.py:5562`).

## 12. Key Historical Failures and Their Resolutions

| Failure | Observed evidence | Root cause | Resolution | Current status |
|---|---|---|---|---|
| Whole-window/composer controls incorrectly treated as chat rows | [Test-confirmed] Composer controls and whole-window/list-shell cases are represented in tests (`tests/test_chatgpt_navigation_diagnostic.py:5401`, `tests/test_chatgpt_navigation_diagnostic.py:5428`). | [Code-confirmed] Row extraction without a forward-resolved Chats-list container could admit unrelated controls. | [Code-confirmed] `_forward_resolve_project_chats_list_container` independently resolves the list and rejects whole window/sidebar/header/composer/transcript regions (`agent/chatgpt_navigation_diagnostic.py:3751`). | [Test-confirmed] Fails closed with no actions. |
| Fixed large scroll pulses and weak continuity behavior | [Test-confirmed] Test explicitly rejects legacy `-360` delta (`tests/test_chatgpt_navigation_diagnostic.py:4130`). | [Design decision] Coarse fixed pulses could skip rows. | [Code-confirmed] Delta derives from median row height and overlap is checked/recovered (`agent/chatgpt_navigation_diagnostic.py:6630`, `agent/chatgpt_navigation_diagnostic.py:5905`). | [Test-confirmed] Covered by focused continuity tests. |
| Real visible chats selectively omitted | [Test-confirmed] Resolver accepts canonical and historical-style list fixtures (`tests/test_chatgpt_navigation_diagnostic.py:5545`). | [Code-confirmed] Earlier overly narrow row model would miss merged direct AXButton rows. | [Code-confirmed] Row title info accepts `AXTitle`, merged `AXDescription`, and eligible `AXValue` under valid row geometry (`agent/chatgpt_navigation_diagnostic.py:4033`). | [Test-confirmed] Current resolver sees expected rows. |
| SQL/code previews causing valid chats to be rejected | [Test-confirmed] SQL/code/JSON/URL/prose preview tests (`tests/test_chatgpt_navigation_diagnostic.py:3262`, `tests/test_chatgpt_navigation_diagnostic.py:3276`). | [Code-confirmed] Preview-like text filtering must not apply to canonical title after splitting. | [Code-confirmed] Canonical title eligibility applies to title prefix only (`agent/chatgpt_navigation_diagnostic.py:4072`, `agent/chatgpt_navigation_diagnostic.py:4106`). | [Test-confirmed] Fixed. |
| Discovery pipeline printing a title but target matching not detecting it | [Test-confirmed] Tests assert canonical discovery and exact target detection for `"Mock Data Insertion SQL"` (`tests/test_chatgpt_navigation_diagnostic.py:3346`, `tests/test_chatgpt_navigation_diagnostic.py:3426`). | [Design decision] Discovery and matching must share normalized row representation. | [Code-confirmed] `_project_chat_normalized_rows` feeds both discovery and target detection (`agent/chatgpt_navigation_diagnostic.py:4958`, `agent/chatgpt_navigation_diagnostic.py:4981`). | [Test-confirmed] Fixed. |
| Stale/offscreen geometry after target detection | [Test-confirmed] Disappearing target and fresh-row-after-scroll tests (`tests/test_chatgpt_navigation_diagnostic.py:4043`, `tests/test_chatgpt_navigation_diagnostic.py:3759`). | [Design decision] AX paths/frames after scrolling can become stale. | [Code-confirmed] Fresh re-resolution is required before action; material changes fail closed (`agent/chatgpt_navigation_diagnostic.py:5139`, `agent/chatgpt_navigation_diagnostic.py:5185`). | [Test-confirmed] Fixed. |
| Partially clipped exact target unable to open | [Test-confirmed] Partially clipped target alignment test (`tests/test_chatgpt_navigation_diagnostic.py:3459`). | [Code-confirmed] Safe click point can be outside viewport for clipped row. | [Code-confirmed] One-shot semantic `AXScrollToVisible` aligns the freshly resolved exact row, then fresh re-resolves (`agent/chatgpt_navigation_diagnostic.py:5289`). | [Test-confirmed] Fixed in tests. |
| Central dispatcher rejecting target-only AXScrollToVisible | [Test-confirmed] Dispatcher now allows only authorized exact-target alignment context (`tests/test_chatgpt_navigation_diagnostic.py:3369`). | [Code-confirmed] Dispatcher previously only allowed AXPress for autonomous reader. | [Code-confirmed] `_AutonomousSidebarAXReader.perform_action` permits `AXScrollToVisible` only through `_autonomous_sidebar_axscrolltovisible_authorized` (`agent/chatgpt_navigation_diagnostic.py:9317`). | [Test-confirmed] Fixed narrowly. |
| Post-action conversation verifier false negative | [Open question] The prompt references this as prior behavior, but no repository live trace proves final live behavior. | [Open question] ChatGPT Desktop may not expose enough stable conversation identity signals after successful open. | [Code-confirmed] Current verifier uses outside-list title, conversation structure, selection/focus, and list-not-primary signals (`agent/chatgpt_navigation_diagnostic.py:7137`). | [Open question] Still needs live hardening/proof. |

## 13. Live-Confirmed Evidence

[Live-confirmed] No new live ChatGPT Desktop commands were run for this handoff. This task explicitly prohibited activation, clicking, scrolling, AXPress, typing, screenshots, browser automation, or app-state mutation.

[Open question] Opening an exact project: repository code and tests prove the implementation path, but this handoff did not find a durable repository trace proving a manual live success.

[Open question] Discovering a project Chats list: repository tests prove this against synthetic AX trees; durable live proof is not present in repository files.

[Open question] Discovering an offscreen target while scrolling and stopping on exact target detection: tests prove this; live proof is not present in repository files.

[Open question] Opening an already-visible exact target: tests prove this; live proof is not present in repository files.

[Open question] Opening a target after scrolling/alignment: tests prove this; live proof is not present in repository files.

[Open question] Remaining verifier limitation: current code can still report `action_posted_but_chat_not_confirmed`; live false-negative behavior requires a real ChatGPT Desktop trace to close.

## 14. Tests and Validation

[Test-confirmed] Focused command run during this handoff:

```sh
python -m unittest tests.test_chatgpt_navigation_diagnostic
```

[Test-confirmed] Result: `Ran 214 tests in 0.328s — OK`.

[Test-confirmed] Full unittest discovery run during this handoff:

```sh
python -m unittest discover
```

[Test-confirmed] Result: `Ran 451 tests in 9.692s — OK`. The command printed expected test-owned "Stopped..." messages after the OK summary.

[Test-confirmed] Compile check run during this handoff:

```sh
python -m compileall -q agent tests
```

[Test-confirmed] Result: exit code 0 with no output.

[Open question] Pytest availability was not checked and is not needed for the current repository validation path.

[Test-confirmed] Key covered categories include project/list identity, normalized row extraction, preview-safe title normalization, strict matching, discovery-to-match same-snapshot behavior, stale geometry prevention, target alignment, action authorization, and fail-closed behavior.

## 15. Current Known Limitations and Open Work

[Open question] Post-action conversation verification hardening remains the single biggest unresolved risk because action posting and confirmed conversation-open success are intentionally separate, and no durable live trace proves all verifier false negatives are eliminated.

[Open question] Title handling for comma-containing chat titles is conservative. Current policy requires explicit exact `AXTitle`; description-prefix rows with comma titles fail closed.

[Open question] Robustness across future ChatGPT Desktop versions/UI changes is not guaranteed. The resolver is defensive, but AX roles/descriptions/frames can change.

[Open question] Behavior when AX exposure changes or is incomplete remains bounded by fail-closed outcomes such as `project_chat_list_identity_not_confirmed`, `chat_row_not_interactable`, or `post_action_inspection_unavailable`.

[Open question] Search end-of-list certainty is evidence-based, not mathematically complete. The code can confirm end by scrollbar bottom evidence plus unchanged continuity, or by anchor stability, otherwise it reports budget/no-progress/continuity outcomes.

[Open question] Multi-workstream scheduler/orchestrator is not implemented in the inspected navigation code.

[Open question] Persistent per-workstream state/recovery is not implemented for this navigation primitive.

[Open question] Test coverage is broad but synthetic; live ChatGPT Desktop variability still needs controlled traces.

## 16. Rules for Future Changes

[Design decision] Do not weaken project/list identity gates.

[Design decision] Do not reintroduce whole-window or composer/transcript row extraction.

[Design decision] Do not use preview content to reject titles.

[Design decision] Do not let discovery, matching, and action use separate stale row representations.

[Design decision] Stop all discovery scrolling after exact target detection.

[Design decision] Require fresh re-resolution before action.

[Design decision] Never action partially clipped or stale geometry.

[Design decision] Keep target alignment one-shot and semantic.

[Design decision] Preserve fail-closed behavior.

[Design decision] Use narrow read-only audits before modifications.

[Design decision] Do not run broad whole-codebase audits unless the requested change truly requires it.

[Design decision] Keep automated validation focused and proportionate.

## 17. Recommended Immediate Next Step

[Open question] The next best engineering step is post-action conversation verifier hardening with a narrow live/read-only evidence pass first. Capture which AX signals are exposed after a known successful manual project-chat open, then adjust `_project_chat_post_action_inspection` and `_project_chat_verification_signals` only if the evidence shows a reliable missing signal.

## 18. File Map

| File | Responsibility | Important symbols |
|---|---|---|
| `agent/cli.py` | CLI commands, argument validation, result printers | `_build_parser`, `_print_inspect_chatgpt_project_visible_chats_result`, `_print_diagnose_chatgpt_project_chat_rows_result`, `_print_open_chatgpt_project_chat_result`, `_print_live_project_chat_discovery_lines` |
| `agent/chatgpt_navigation_diagnostic.py` | AX snapshot model, project/sidebar navigation, project chat list resolver, row normalization, scroll search, target alignment, action dispatch, verifier | `AXElementSnapshot`, `ProcessResolution`, `resolve_open_project_content_and_visible_chats`, `_forward_resolve_project_chats_list_container`, `_visible_project_chat_rows`, `_project_chat_accessibility_text_parts`, `open_chatgpt_sidebar_destination`, `open_chatgpt_project_chat`, `_bounded_project_chat_scroll_search`, `_attempt_project_chat_target_alignment`, `_project_chat_post_action_inspection`, `_AutonomousSidebarAXReader` |
| `tests/test_chatgpt_navigation_diagnostic.py` | Main synthetic validation suite for navigation | `ChatGPTNavigationDiagnosticTests`, `ChatGPTNavigationDiagnosticCLITests`, `_scrollable_project_chat_page`, `_mock_data_sql_project_chat_page`, `_project_chat_page_with_single_row_frame`, `_alignment_dispatch_context`, `_LiveOutputRecorder` |
| `README.md` | Repository-level overview and existing command docs | Local supervised loop setup, Codex run examples, ChatGPT paste/capture context |

## 19. Appendix: Example Successful Execution Trace

[Open question] The following is illustrative, assembled from current result fields and test-proven behavior. It is not a durable live trace.

```text
requested_project: PTG Assistant
requested_chat: Mock Data Insertion SQL

project_open_result:
  outcome: destination_opened_and_visible_chats_resolved
  ok: true

project/list identity:
  project_identity_confirmed: true
  project_chat_list_identity: confirmed
  chats_tab_active_evidence: chats_tab_present_with_resolved_list

visible chats discovered:
  Chats discovered:
  1. Content Moderation
  2. Mock Data Insertion SQL

target detection:
  target_exact_match_detected: Mock Data Insertion SQL
  target_detected_in: hydration
  target_detected_cycle: 1
  scroll_pulses_after_target_detection: 0

target alignment:
  target_alignment_required: true
  target_alignment_method: axscrolltovisible
  target_alignment_posted: true
  target_alignment_post_visibility: fully_visible

fresh re-resolution:
  fresh_target_re_resolution_confirmed: true

action:
  chosen_method: axpress
  actions_performed:
    - AXScrollToVisible on freshly resolved exact target row, if clipped
    - AXPress on freshly re-resolved fully visible row

final outcome:
  chat_opened_after_scrolling_via_axpress
```
