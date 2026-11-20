# Migration Plan: Classic ChatGPT -> New ChatGPT App

Status: **plan only, nothing here is implemented yet.** Everything below is intended
future architecture. Facts about the new app come from
`NEW_CHATGPT_APP_AX_FINDINGS.md` (section numbers are cited as "F-n").

- Classic app: bundle `com.openai.chat` (native SwiftUI)
- New app: bundle `com.openai.codex` (Electron, shows itself as "ChatGPT")

---

## The approach

Add the new app as a **second ChatGPT target** next to Classic, chosen by one setting.
Build the new app's support in **new modules**, and leave the Classic modules as they
are. The loop services pick the implementation by target, and both implementations
return results in the **same shape**, so the ledger, the dashboard and prompt
extraction don't change. Classic remains the default until the new path has passed
a live run. After that the default flips, and Classic stays available for rollback.

```
supervision loop
   |
   v
chatgpt_services / supervision_services   (choose by target)
   |                         |
   v                         v
Classic modules (as-is)    New-app modules (new)
```

---

## Step 0: Groundwork

> **Status (2026-11-20): done. No loop behavior changed.**
> - **`agent/chatgpt_target.py`:**
>   - Resolves the target: explicit value, else `CRAX_CHATGPT_APP`, else
>     `classic`. Invalid values are errors, never a fallback.
>   - Records it once per run as a `chatgpt_target_selected` event.
>   - A run can never switch apps. Runs without the event are Classic.
>   - `ChatGPTTargetServices` names the four services `run_supervision_step`
>     takes: submit, capture, destination adapter, navigation.
>     `classic_target_services()` returns exactly today's defaults.
>   - `services_for_target("new")` **refuses** with `chatgpt_target_not_wired`
>     until Step 8.
> - **`agent/chatgpt_desktop_mutex.py`:** `acquire(..., chatgpt_target=None)`
>   writes the target into the lock's owner record only when given, so existing
>   callers write exactly the same record. The lock stays shared by both apps.
> - **Characterization tests** (`tests/test_chatgpt_target.py`) pin today's
>   wiring:
>   - `run_supervision_step` defaults to the four Classic services.
>   - The submit and capture defaults forward their arguments unchanged. The
>     capture default **drops** the timeout it is given (current behavior,
>     pinned as-is).
>   - The adapter default is the Classic adapter, and navigation uses the
>     Classic navigator.
>   - The dashboard's `LocalController` passes no ChatGPT services, so it relies
>     on those defaults.
> - **Deferred to Step 8 on purpose:**
>   - the `--chatgpt-app` CLI flag
>   - recording the target when a run starts
>
>   Exposing a "new" choice before the loop can honor it would let a run claim
>   the new app while still driving Classic.
> - **Two loop entry points need the switch in Step 8:**
>   - the dashboard (`LocalController` → `run_supervision_step` defaults)
>   - the CLI `supervise` command, which passes **its own** submit and capture
>     wrappers (`agent/cli.py` `_run_supervise_command`) and the default
>     adapter and navigation
> - **Already-failing tests,** unrelated, confirmed failing without these
>   changes:
>   - `test_chatgpt_ax_capture` ...`test_supervise_capture_wiring_requires_sentinel`
>     (stale: the CLI now passes a different ledger object than the test
>     patches, so the step returns `run_not_found`)
>   - two `test_remote_access.ControllerSnapshotTests`
>   - one `test_web_static` dashboard contract test

1. **Target setting.**
   - Environment variable `CRAX_CHATGPT_APP=classic|new` and CLI flag
     `--chatgpt-app classic|new`. The default is `classic`.
   - Record the target on the run in the ledger when the run starts. A run never
     switches apps partway through.
2. **One interface for both apps.** It has these operations:
   - `preflight()`: find the app and make it automatable
   - `open_destination(project, chat)`
   - `read_destination_snapshot()`, returning the existing
     `ChatGPTDestinationSnapshot` type
   - `submit(message, marker)`
   - `capture(marker, ...)`

   The Classic implementation wraps today's functions. The new one is written from
   scratch.
3. **Characterization tests first** (AGENTS.md):
   - Add tests that pin today's behavior at the dispatch points in
     `agent/chatgpt_services.py` and `agent/supervision_services.py`, so we can show
     that Classic behaves the same after the split.
4. **Synthetic AX fixtures for the new app.**
   - Build small fake trees with the real structure: sidebar, banner, turn list with
     "You said:" / "ChatGPT said:" headings, composer, Send/Stop, a code block with
     `"\n"` line nodes, and a list. Use made-up content.
   - Unit tests run on these and never touch the live app.
   - Real dumps stay out of the repo.
5. **One shared lock.** Keep `agent/chatgpt_desktop_mutex.py` and its lock file as
   they are, so a Classic run and a new-app run can never drive ChatGPT at the same
   time. Add the target name to the lock metadata.

---

## Step 1: Find the app and make it automatable (`preflight`)

> **Status (2026-11-20): implemented, not wired into the loop.**
> - Code is in `agent/new_chatgpt_app.py`: `preflight_new_chatgpt()` returns a
>   `PreflightResult` with an `ok` flag and a `reason_code`. The bundle id
>   constant is in `agent/mac_app_control.py`.
> - Tested with synthetic trees in `tests/test_new_chatgpt_preflight.py`.
> - Not yet run against the live app.
> - Two choices made beyond the plan:
>   - An unknown overlay that is not a menu or dialog (for example a toast) is
>     reported but does not block.
>   - A hidden sidebar stops with `new_chatgpt_mode_unknown`, because the mode
>     can't be read without it.

Run this before **every** loop step that touches the app (navigate, gate, submit,
capture).

1. **Find the process by bundle id `com.openai.codex`.** Never search by the name
   "ChatGPT", because both apps use that name. Add `NEW_CHATGPT_BUNDLE_ID` next to
   `CLASSIC_CHATGPT_BUNDLE_ID` in `agent/mac_app_control.py`.
2. **Switch on clickable accessibility.**
   - Set `AXEnhancedUserInterface = true` on the app element. The call reports
     error -25208, but the setting still takes effect, so ignore that error.
   - Read the value back. If it isn't `true`, stop with
     `new_chatgpt_ax_not_enabled`.
   - The app resets this setting when it restarts (seen 2026-11-18), so never cache
     it (F-2).
3. **Make sure the window content is attached.**
   - If the window has no `AXWebArea`, or the web area has almost no children,
     activate the app by bundle id.
   - Then poll for up to about 5 s until `AXWebArea` exists with `AXLoaded = true`.
     The app lives on its own fullscreen Space, and its content is only attached
     while that Space is shown (F-2).
4. **Check the product mode.**
   - Read the sidebar control `AXPopUpButton "Switch mode, current mode: <X>"`. It
     must say `ChatGPT`.
   - If it doesn't, stop with `new_chatgpt_wrong_mode`. Switching modes
     automatically can come later (F-8).
5. **Close stray menus.**
   - If anything other than live regions sits next to `#root` (open menus and
     popovers mount there), set `AXFocused = true` on the composer.
   - **Never send Escape.** During a reply it opens "Stop this response?" (F-5, F-8).

---

## Step 2: One tree reader for the new app

> **Status (2026-11-20): implemented, not wired into the loop.**
> - `agent/new_chatgpt_reader.py`:
>   - `locate_anchors()` finds web area, `#root`, sidebar, title bar, main, chat
>     panel, turn list and composer, and returns element handles.
>   - `snapshot_region()` copies one region into immutable `AXNodeData` in
>     document order, with no index paths and no frames.
> - `agent/new_chatgpt_labels.py` is the label table.
> - Tests: `tests/test_new_chatgpt_reader.py` with synthetic trees in
>   `tests/new_chatgpt_ax_fixtures.py`.
> - The anchor search skips the inside of the sidebar, the title bar and each
>   turn, so its cost doesn't grow with chat length.
> - A composer or turn list is only accepted when exactly one candidate exists.
> - The real backend reads each element's attributes in one AX call
>   (`AXUIElementCopyMultipleAttributeValues`).
> - **Verified live, read-only, on 2026-11-20:**
>   - all 8 anchors found, every region snapshot complete
>   - locate takes 0.05 s; the chat panel snapshot takes 0.04 s
> - The app had updated and dropped the `*-chat-panel` id. The reader now falls
>   back to the `group/thread-scroll-layout` class (findings section 17).

The existing readers can't be reused. They stop at depth 18 and 1,200 nodes, but
chat content is at depth 26 to 31 and the window has about 1,700+ nodes. They also
group text by Classic's `AXHostingView`.

- **Start at `AXWebArea`.**
- **Find regions by stable anchors** (F-3, F-11), not by index paths:
  - Sidebar: `#app-shell-sidebar`
  - Title bar: `AXGroup/AXLandmarkBanner`
  - Chat panel: the DOM id ending in `-chat-panel`
  - Turn list: the group whose direct children include the "You said:" /
    "ChatGPT said:" headings
  - Composer: `AXTextArea` titled "Ask ChatGPT" with class `ProseMirror`, inside
    the chat panel
- **Walk only the region you need,** with generous limits (depth >= 45). Keep
  today's rule: if a walk hits its limit, report it as incomplete and don't act on
  partial data.
- **Never reuse index paths between reads.** Opening a menu shifts them. Re-find
  elements from their anchors every time.
- **Never use frames for visibility or order.** Off-screen nodes report fake 2x2
  frames. Use sibling order instead.
- **Keep all labels in one table** (new `labels` module), each with its i18n id from
  `tools/handoff_label_catalog.tsv`. For now we support English only. If a needed
  label is missing, stop with a clear reason code; don't guess.

---

## Step 3: Open the bound project chat (navigation)

> **Status (2026-11-20): implemented, not wired into the loop.**
> - `agent/new_chatgpt_navigation.py`: `open_project_chat(project, chat, dry_run=False)`.
> - The only things it presses: the "Projects" header (if collapsed), the
>   project (if collapsed), "Show more" (capped at 5) and the chat row.
> - It confirms by the row having `AXARIACurrent = "page"` **and** the web area
>   title equalling the chat title.
> - Duplicate project or chat names fail closed.
> - Tests: `tests/test_new_chatgpt_navigation.py`, using a fake sidebar that
>   re-renders on presses.
> - **Live dry run passed** (read-only): exactly one matching project and chat
>   row was found.
> - **Live click test (run by the user, 2026-11-20).** The press switched the
>   chat every time, even with the app in the background.
>   - Confirmation **only succeeded when the app was on screen**: 0.7 to 1.1 s
>     after the user brought it forward.
>   - With the app left in the background it timed out with
>     `new_chatgpt_nav_not_confirmed`, because the AX tree does not update while
>     the window is off screen (findings section 19).
> - **Fix:** navigation now calls preflight with `bring_to_front=True`, which
>   activates the app and waits for `AXFrontmost`. A dry run doesn't change
>   focus.
> - **Re-verified live after the fix (2026-11-20).** The user stayed in the
>   terminal and never touched the app. Two switches, there and back, both
>   reported `opened`, confirmed in 0.86 s and 0.45 s. **Step 3 is done.**

New function `open_project_chat(project, chat)`, used at
`agent/supervision_services.py:513` when the target is `new`:

1. Preflight.
2. In the sidebar, make sure the "Projects" section is expanded. Find the project
   button `"<project>"`, then the list `"Chats in <project>"`.
3. Find the row button whose title equals `<chat>` **inside that list only**. Ignore
   copies of the same chat under "Pinned" or "Recents" (F-3).
4. If the row isn't there, press that project's "Show more" (a few times at most),
   then look again.
5. Press the row with `AXPress`. This needs the Step 1 setting. Then wait until the
   title bar shows the chat.
6. Exactly as today, whether we landed in the right place is decided by the gate
   (Step 4), not by this function.

---

## Step 4: "Are we in the right chat?" gate

> **Status (2026-11-20): implemented, not wired into the loop.**
> - `agent/new_chatgpt_destination.py`: `NewChatGPTDestinationSnapshotAdapter`.
>   The gate in `agent/chatgpt_destination_gate.py` is unchanged.
> - **Deviation from the table below.** The project is read from the sidebar,
>   because the title bar no longer shows it (findings section 20). The
>   `active_conversation_*` fast-path fields are left **empty** on purpose. The
>   new app has no authoritative "<chat>, <project>" source like Classic's
>   window title item, so the gate uses its strict path: project, current row,
>   title bar, composer, transcript and surface must all agree.
> - The adapter reads twice, 0.2 s apart (stability), presses nothing, and runs
>   preflight with `bring_to_front=True`.
> - Tests: `tests/test_new_chatgpt_destination.py` runs the real gate against
>   fake trees.
> - **Verified live, read-only:**
>   - the correct binding passes
>   - the same project with a different chat is blocked (`chat_not_active`)
>   - a different project with the same chat is blocked (`project_not_active`)

New adapter that produces the existing `ChatGPTDestinationSnapshot`. The gate logic
in `agent/chatgpt_destination_gate.py` **stays unchanged**. Adapter factory:
`agent/supervision_services.py:352`.

| Snapshot field | New-app source |
|---|---|
| `active_conversation_chat_title` | Left empty: no authoritative source (see status above) |
| `active_conversation_project_title` | Left empty: the project isn't shown outside the sidebar (findings section 20) |
| `conversation_header_candidates` | Title-bar `AXPopUpButton "<chat>"`. Its identity is confirmed only when it equals both the web area title and the window title. |
| `active_project_candidates` | Project rows in the sidebar "Projects" section. Active = its `"Chats in <project>"` list holds the `AXARIACurrent = "page"` row. |
| `selected_chat_row_candidates` | The row in `"Chats in <project>"` with `AXARIACurrent = "page"` (confirmed live, findings section 18) |
| `composer_available` | Composer found |
| `transcript_available` / `conversation_surface_available` | Turn list found |
| `snapshot_complete` / `ax_tree_truncated` | From the Step 2 walk |

> **Resolved:** the project is shown only in the sidebar (findings section 20).

**Identity rule (implemented).** The new app exposes no conversation ID (F-0), so
identity is title-based:
- The title-bar chat title and project must match the binding.
- **Exactly one** row with that title may exist in that project's chat list.
- If two chats in the same project share a title, fail closed with
  `destination_ambiguous_title`, and the operator renames one.

---

## Step 5: Send the feedback

> **Status (2026-11-20): implemented, not wired into the loop.**
> - `agent/new_chatgpt_submit.py`: `submit_message_new_chatgpt(message, marker_text=...)`.
> - Tests: `tests/test_new_chatgpt_submit.py`, with a fake composer and Send
>   button that behave like the live app.
> - **Change from the original plan.** The retry press happens **only when
>   `AXEnhancedUserInterface` reads false** after a press with no visible
>   effect. That is the one known cause of a silently ignored press. Any other
>   "no effect" just keeps waiting and then fails with `send_not_registered`,
>   so a slow app can never cause a double send.
> - A new message is detected by the **count of user turns containing the
>   marker** rather than by turn indexes, so it is robust to the virtualized
>   turn list and to an identical earlier message.
> - **Verified live (user-approved, 2026-11-20).** The Step 4 gate passed
>   first, then one message was sent in the bound chat:
>   - read-back matched, one Send press
>   - new user turn found, Stop seen, composer cleared
>   - confirmed in 0.34 s

This replaces clipboard + Cmd+V + Enter for the new target. It needs no clipboard
and no keystrokes.

1. Preflight with `bring_to_front=True`. The value can be set and Send pressed in
   the background, but the read-back and send confirmation need a live tree
   (findings section 19).
2. **Find the composer** (Step 2).
   - Treat it as empty when its value is `""` or `"Ask ChatGPT\n"` (the placeholder
     appears in the value), or when Send is disabled (F-4).
   - If it holds a draft, behave as Classic does today: overwrite it, but record in
     the ledger evidence that a draft was overwritten.
3. **Set the composer's `AXValue`** to the full feedback message, marker block
   included. This was tested byte-identical with multi-line text (F-4).
4. **Read the value back.** It must equal the message exactly. Retry the set once;
   otherwise stop with `composer_value_mismatch`.
5. **Press `AXButton "Send"`** in the composer group with `AXPress`, only when it is
   enabled.
6. **Confirm the send** within a timeout. All of these must hold:
   - "Stop" replaced "Send", or the composer is empty with Send disabled.
   - A new "You said:" turn contains the marker.
7. **If the press did nothing** (Send still enabled, text still in the box) **and**
   `AXEnhancedUserInterface` reads false:
   - re-run preflight, which turns the setting back on,
   - press once more,
   - otherwise stop with `send_not_registered`. Never press a third time, and
     never press again when the setting was already on.

   There is no Enter-key fallback.

New ledger method names: `ax_set_value_new_chatgpt`, `ax_press_send_new_chatgpt`.
The returned result keeps the same keys as `submit_feedback_to_chatgpt_service`
returns today (`agent/chatgpt_services.py:189`).

---

## Step 6: Capture GPT's reply

> **Status (2026-11-20): implemented, not wired into the loop.**
> - `agent/new_chatgpt_capture.py`: `capture_reply_new_chatgpt(marker_text, require_sentinel=True)`.
>   It returns the rebuilt reply text and the prompt extracted with the
>   **existing** `extract_next_codex_prompt_from_text`.
> - **"Finished" means:** no `Stop`, a footer with `Copy` and `Rate response`,
>   and the same text on 2 consecutive polls.
> - **A finished reply without a prompt ends** with `completed_without_prompt`
>   (outcome `complete_without_prompt`) instead of waiting forever.
> - Tests: `tests/test_new_chatgpt_capture.py`.
> - **Verified live (read-only)** on a real reply in "Review Chat Architecture":
>   - `complete_with_prompt` after 2 polls
>   - a 7,491-character prompt with its line breaks intact
>   - the thinking-summary button text was excluded (findings section 21)
> - It needs the app on screen (preflight `bring_to_front=True`). The only press
>   it can make is "Scroll to bottom", once, when the marker turn is missing.

New capture function. The surrounding service
(`agent/chatgpt_services.py:942`) keeps its marker/sha checks, polling, timeout and
`should_stop`.

1. Preflight with `bring_to_front=True`. ChatGPT **must be on screen** while
   capturing. The AX tree stops updating while the window is off screen
   (findings section 19), so a background read would see a stale or unfinished
   reply. Classic already required ChatGPT to be frontmost here.
2. **Split the turn list into turns** at the "You said:" / "ChatGPT said:" headings
   (F-6).
3. **Find the user turn containing the marker.** There must be exactly one.
   - If there's none, press "Scroll to bottom" once and read again, since older
     turns can drop out of long chats.
   - Still none: stop with `marker_turn_not_found`.
4. **The reply** is everything from the next "ChatGPT said:" up to the following
   "You said:" (or the end).
   - Split it into **body** and **footer**. The footer is the action buttons: Copy,
     Share, Add to project sources, Read aloud, Regenerate response, Rate response,
     More actions.
5. **Rebuild the text exactly.**

   | Content | How it is read |
   |---|---|
   | Code blocks (`AXCodeStyleGroup`) | Join every text node as-is, keeping the `"\n"` nodes. No trimming, no dropping repeated lines. This is where the Codex prompt normally arrives. |
   | Paragraphs and headings | One line each, separated by newlines |
   | List items | Prefix each with its `AXListMarker` value ("1. ", "- ") |
   | Tables | One line per row |

   Never drop repeated lines anywhere.
6. **"Finished" means all of these:**
   - No "Stop" button, and "Send" is present.
   - The reply's footer contains "Copy" and "Rate response".
   - The text is unchanged across 2 polls about 1 s apart.

   The "Response complete" announcement is supporting evidence only, because the
   app reuses it from earlier replies (F-7).

   **"Still writing"** means "Stop" is present or the footer is missing.
7. Pass the reply text to the **existing** sentinel logic unchanged. The
   "finished without a Codex prompt" outcome now uses rule 6 instead of the Classic
   thumbs-up/down rule.

---

## Step 7: Extract the next Codex prompt

No change. `extract_next_codex_prompt_service`
(`agent/chatgpt_services.py:1130`) works on the captured text. Add one test: a
new-app fixture with the prompt in a code block must extract byte-identical to the
original.

---

## Step 8: File-by-file changes

> **Status (2026-11-20): done, Classic unchanged.** The design changed from the
> table below. It **reuses the Classic services** instead of branching inside
> them.
>
> **How it works.** `submit_feedback_to_chatgpt_service` and
> `capture_chatgpt_response_service` already accept every UI operation as an
> injected function. `agent/new_chatgpt_loop.py` supplies new-app versions:
>
> | Classic operation | New-app version |
> |---|---|
> | clipboard copy | stage in memory |
> | activate | preflight with `bring_to_front=True` |
> | inspect | the reader, in the Classic dict format |
> | paste | set `AXValue` and read it back |
> | press Send | `AXPress`, retried once only if `AXEnhancedUserInterface` was off |
> | Enter | never; always "not supported" |
> | capture | `capture_reply_new_chatgpt` mapped to Classic result keys and reason codes, including `chatgpt_completed_without_valid_prompt` |
>
> **What stays exactly the Classic code path:** feedback building, the marker,
> every ledger event and its metadata, approval, cancel and verification. The
> Classic Enter fallback can't fire, because it is keyed on Classic's AXPress
> method name.
>
> **Dispatch, per run:**
> - **Dashboard** (`agent/local_server.py`, `agent/local_controller.py`):
>   - `--chatgpt-app classic|new` or `CRAX_CHATGPT_APP` sets the app for
>     **new** runs.
>   - A new-app run gets a `chatgpt_target_selected` event at start. Classic
>     runs write nothing new.
>   - `_supervision_handoff_kwargs` adds the four new-app services only for
>     new-app runs.
> - **CLI** (`agent-loop supervise --chatgpt-app classic|new`):
>   - Without the flag or env var it follows the run's recorded app.
>   - An explicit "new" is recorded for a run that has not handed off yet.
>   - Switching a run that already used Classic is refused (exit 2,
>     `chatgpt_target_change_refused`).
>
> **Classic is byte-for-byte unchanged** (only additive parameters). The Step 0
> characterization tests still pass, and the full suite shows only the 4 failures
> that already existed (1,213 pass).
>
> **Tests:**
> - `tests/test_new_chatgpt_loop.py` runs the **real** Classic submit → capture
>   → extract services on a fake new app: the same 5 submit events, the prompt
>   extracted exactly, no clipboard, paste or Enter calls,
>   `completed_without_prompt`, and no double press.
> - `tests/test_chatgpt_target.py` covers controller and CLI dispatch, the
>   switch guard, and the env/flag settings.
>
> **Live read-only check:** activate and inspect on the real app. The composer
> placeholder reads as empty, Send is correctly not offered while it's empty,
> and the Classic send-button safety rule accepts the new Send.
>
> **Not done here:**
> - The desktop-mutex target field is not populated by the loop yet. It is
>   informational only, and the lock stays shared.
> - A real supervised run is Step 9.

| File | Change |
|---|---|
| `agent/mac_app_control.py` | Add `NEW_CHATGPT_BUNDLE_ID = "com.openai.codex"`. Activate by target. |
| **new** `agent/new_chatgpt_app.py` | Preflight and the ctypes AX backend (done) |
| **new** `agent/new_chatgpt_reader.py` | Anchors and region snapshots (done) |
| **new** `agent/new_chatgpt_labels.py` | Label table (done) |
| **new** `agent/new_chatgpt_submit.py` | Step 5 |
| **new** `agent/new_chatgpt_capture.py` | Step 6 |
| **new** `agent/new_chatgpt_destination.py` | Steps 3 and 4 (navigation and snapshot adapter) |
| `agent/chatgpt_services.py` | At submit and capture, branch on the run's target. The result shape is unchanged. |
| `agent/supervision_services.py` | Adapter factory (line 352) and navigation (line 513) chosen by target |
| `agent/cli_parser.py`, `agent/cli.py` | `--chatgpt-app classic|new` flag |
| `agent/chatgpt_desktop_mutex.py` | Add the target to the lock metadata only |
| Classic modules (`mac_paste.py`, `mac_ui_inspect.py`, `chatgpt_ax_capture.py`, `chatgpt_ax_destination_snapshot.py`, `chatgpt_navigation_diagnostic.py`) | **No change** |
| **new** `tests/test_new_chatgpt_*.py` + fixtures | Unit tests for steps 1 to 7 |

The new modules stay out of the god files (`cli.py`,
`chatgpt_navigation_diagnostic.py`).

---

## Step 9: Rollout

1. **Unit tests** on the synthetic fixtures (all steps).
2. **Read-only live check.** A command that runs preflight, the destination snapshot
   and "read last reply", with **no send**. It needs the user's explicit go-ahead
   (AGENTS.md live-automation rule).
3. **Live send test** in a throwaway chat: submit, capture, extract.
4. **One real supervised run** with `--chatgpt-app new` on a real project chat.
5. **Flip the default** to `new`. Keep `classic` working for at least one release so
   we can roll back, then decide whether to remove it.

---

## Still to confirm while building

Check each of these with the read-only probe tools in `tools/`:

- ~~Which attribute marks the current chat row~~: resolved. It is
  `AXARIACurrent = "page"` (findings section 18).
- **Long feedback messages** through `AXValue`. Only short texts were tested, and
  real feedback messages are several KB.
- Timing for **long or "thinking" replies** (only a short reply was observed).
- **Long chats**: confirm that the marker turn stays in the tree when scrolled to
  the bottom.
- After **each app update**, re-run the label catalog check (F-12) and the fixtures'
  structure assumptions.
