# New ChatGPT Desktop App (`com.openai.codex`): Accessibility Tree Findings

Status: **research only, nothing is implemented.** No file under `agent/` was changed.
Captured on 2026-11-18 against a live, signed-in app on this Mac.

All findings below are **observed** (seen in live AX dumps or read from the app's
bundled JS) unless they are marked **[inferred]** or **[not verified]**. Some sections
describe how a future handoff could use these findings. Those parts are proposals and
are labelled **[proposal]**, because no such code exists yet.

User content (conversation titles, project names, message text, account name) is
redacted as `<chat title>`, `<project name>` and so on.

---

## 0. TL;DR for the handoff work

1. **Different app, colliding name.** The new app is `/Applications/ChatGPT.app` with
   bundle `com.openai.codex`. The old one is now `/Applications/ChatGPT Classic.app` with
   bundle `com.openai.chat`. Both present themselves as "ChatGPT" (System Events
   process name `ChatGPT`, AX app title `ChatGPT`). **Always target by bundle id.**
2. **It is Electron/Chromium (Chrome/155), not SwiftUI.** The Classic-specific
   heuristics in `agent/` do not apply. For example, `agent/chatgpt_ax_capture.py`
   looks for `AXGroup`/`AXHostingView` containers, and the composer heuristics in
   `agent/chatgpt_ax_destination_snapshot.py` look for "message chatgpt" or "ask anything".
3. **The tree has to be switched on.** The web content is an empty chain of groups until:
   - the window is **on screen** (it is in native fullscreen on its own Space; while
     another Space is active, `AXWindows` is `[]` and the web area isn't attached), and
   - `AXEnhancedUserInterface=true` is set on the app element. Without it the tree can
     exist but **every web element loses `AXPress`**.

   `AXManualAccessibility` is **not supported** by this build (AXError -25205).
4. **The composer accepts direct AX text injection with no clipboard and no keystrokes.**
   Setting `AXValue` on the `AXTextArea` titled "Ask ChatGPT" (a ProseMirror
   contenteditable) works:
   - multi-line text is preserved exactly,
   - the editor model updates, so the Send button enables,
   - an empty value clears it.
5. **Send works with `AXPress` on `AXButton` "Send".** While a response streams, the same
   slot becomes `AXButton` "Stop". The Send button is removed, not disabled.
6. **Turns are a flat sibling list.** Hidden headings mark the boundaries:
   `AXHeading` "You said:" and "ChatGPT said:", both with `AXValue=4`.
   The assistant's markdown renders as ordinary AX roles (lists, list markers,
   static text, tables, code groups).
7. **Completion has several independent signals**, in order:
   - `Stop` disappears and `Send` reappears (disabled while the composer is empty),
   - the action row (Copy, Read aloud, Regenerate response, Rate response) appears
     after the last "ChatGPT said:" heading,
   - a live region announces "Response complete", with an `AXLiveRegionChanged`
     notification.
8. **There's no conversation ID in AX.** `AXWebArea.AXURL` is just `app://-/index.html`.
   Sidebar rows are buttons with no href. The only identity is the title
   (`AXWebArea.AXTitle`, which equals the document title), and titles are not unique.
9. **Index paths are unstable.** Opening any popover inserts a portal before `#root`
   and shifts sibling indexes. Select by role, label, DOM id or DOM class, never by
   index path.

---

## 1. Environment and provenance

| Item | Value |
|---|---|
| App path | `/Applications/ChatGPT.app` |
| Bundle id | `com.openai.codex` |
| Version | `26.1002.52244` (build `13536`) |
| Electron package | `openai-codex-electron`, productName `Codex` (from `app.asar` `package.json`) |
| Chromium | `Chrome/155.0.8059.27` (string in `Codex Framework.framework`) |
| Framework layout | `Contents/Frameworks/Codex Framework.framework` (renamed Electron framework), `Sparkle.framework` |
| Classic app (for comparison) | `/Applications/ChatGPT Classic.app`, bundle `com.openai.chat`, version `1.2026.184`, native Swift (`ChatGPT.framework`, `LiveKitWebRTC.framework`, ...) |
| macOS | Darwin 27.2.0 (arm64) |
| AX trust | The calling process was trusted (`AXIsProcessTrusted() == true`) |
| App language | `AXPreferredLanguage = en_GB`. Every label below is **localized**. |
| Window state during capture | One window, **native fullscreen** (`AXFullScreen=true`), 1920x1080 on the external display |

Method: a standalone Swift CLI (`tools/axprobe.swift`) that calls the public AX C API
directly. It walks `AXChildren` and records, for every node, all attribute names,
selected attribute values, settable attributes, actions, parameterized attributes and
the frame. Raw dumps (several thousand nodes each) are **not** committed because they
contain private conversation content. They were written to the session scratchpad.

---

## 2. Enabling the tree (observed sequence)

| Step | What was done | Result |
|---|---|---|
| 0 | Baseline: app running, window fullscreen on a **different Space** | App element has one child (`AXMenuBar`). `AXWindows = []`, while `AXFocusedWindow`/`AXMainWindow` still return the window. The window subtree is 7 nodes of Chromium views (`RootView > NonClientView > NativeFrameViewMac > ChromeNodeClientView > View > ContentsContainerView`) with **no `AXWebArea`**. CG windows report not on-screen. |
| 1 | `AXUIElementSetAttributeValue(app, "AXManualAccessibility", true)` | **AXError -25205 (attribute unsupported)**. Reading it also returns -25205. No change. |
| 2 | Set `AXEnhancedUserInterface=true` (the app lists it as settable) | Returns **AXError -25208**, yet reading back gives `true`, so the set **did** take effect. The tree is still 7 nodes because the window is still off-screen. |
| 3 | `tell application id "com.openai.codex" to activate` (the window comes on-screen) | `AXWindows` now lists the window. The full web tree appears: **4,408 nodes** in a long conversation, max depth 34. |
| 4 | Activated Finder, so ChatGPT is in the background | The tree **stays populated** (same node count). |
| 5 | Set `AXEnhancedUserInterface=false` | The tree **stays populated**, but **`AXPress` disappears from every web element**. Actions become `[AXShowMenu, AXScrollToVisible]` only. `AXPress` on a sidebar row or the web "Back" button returns 0 and does nothing. |
| 6 | Set `AXEnhancedUserInterface=true` again | `AXPress` comes back. Sidebar-row navigation works again. |

**[not verified]** A true cold start (app launched with EUI never set) was not tested,
because that would require quitting the app. So it is unknown whether step 2 or merely
the AX queries made Chromium build the web tree. Step 5 does show that EUI controls
whether **actions** are exposed.

**[proposal]** Handoff preflight:
1. Resolve the pid by bundle id `com.openai.codex`. Never resolve by name.
2. Set `AXEnhancedUserInterface=true`. **Ignore the -25208 return code and verify by
   reading the value back.**
3. Make sure the window is on-screen. Activating by bundle id worked here, including
   across Spaces.
4. Assert that `AXWindows` is non-empty, that an `AXWebArea` exists with
   `AXLoaded == true`, and that the composer is found.
5. Assert that the composer's Send button has `AXPress` among its actions. This is the
   check for EUI being effective.

---

## 3. Top-level structure

```
AXApplication "ChatGPT"                       settable: AXFrontmost, AXEnhancedUserInterface, AXHidden
├─ AXMenuBar                                  (native Electron menus, section 9)
└─ AXWindow /AXStandardWindow "ChatGPT"       actions: AXRaise; settable: AXFullScreen, AXMain, AXMinimized, AXSize, AXSections
   └─ AXGroup "ChatGPT" .RootView → .NonClientView → .NativeFrameViewMac → .ChromeNodeClientView → .View → .ContentsContainerView
      └─ AXScrollArea
         └─ AXWebArea  AXTitle = document title (= conversation title)   AXURL = app://-/index.html   AXLoaded=true
            └─ AXGroup                       ← "top group": its children are #root plus all portals and global live regions
               ├─ AXGroup #root              ← app shell (always present)
               ├─ [portal] AXGroup …         ← Radix menus, popovers and dialogs mount here, as SIBLINGS of #root
               ├─ AXGroup /AXApplicationStatus #DndLiveRegion-N   (dnd-kit announcements)
               └─ AXGroup /AXApplicationStatus .sr-only.select-none  (global polite live regions; one held "Response complete")
```

Inside `#root` (layout class `._Layout_* ._PageSurfaceLayout_*`):

```
#root
├─ AXLink "Skip to content"                                   (1x1, sr-only)
└─ AXGroup .relative.flex…                                    main shell
   ├─ AXGroup /AXLandmarkComplementary .app-shell-left-panel
   │   └─ AXGroup #app-shell-sidebar                          ← STABLE id
   │       ├─ AXGroup /AXLandmarkNavigation "App navigation"  (52px icon rail)
   │       │    AXButton Home | Images | Scheduled | Library | Plugins
   │       │    AXPopUpButton "Explore" (popup=dialog)
   │       │    AXPopUpButton "Open profile menu" (popup=menu)
   │       └─ AXGroup …sidebar-…
   │           └─ AXGroup /AXLandmarkNavigation "Home"  ._Navigation_*
   │               ├─ header: AXPopUpButton "Switch mode, current mode: ChatGPT" (popup=menu)
   │               │          AXCheckBox/AXToggleButton "View activity"
   │               │          AXButton "Search"
   │               │          AXButton "New chat"
   │               └─ sections (each AXGroup .group/nav-section, header AXButton with AXExpanded):
   │                    "Pinned"   → AXList/AXContentList of chat rows
   │                    "Projects" → per project: AXButton "<project name>" (expanded),
   │                                 AXPopUpButton "Project actions for <project name>",
   │                                 AXButton "New chat in <project name>",
   │                                 AXList/AXContentList "Chats in <project name>" → chat rows,
   │                                 AXButton "Show more"
   │                    "Recents"  → chat rows
   ├─ AXSplitter (sidebar resize; AXValue settable)
   ├─ AXGroup /AXApplicationStatus .sr-only (polite)
   ├─ AXGroup /AXLandmarkBanner   (44px title bar)
   │    AXButton "Back" | "Forward" | "Hide sidebar" (AXExpanded)
   │    AXImage "Project: <project name>"  +  AXButton "<conversation title>"
   │    AXButton "Share" | AXPopUpButton "More" | AXCheckBox/AXToggleButton "Toggle pinned summary" | AXButton "New tab"
   └─ AXGroup /AXLandmarkMain  #_r_xxx_        (dynamic id)
       └─ … AXGroup #_r_xxx_-chat-panel        ← id SUFFIX "-chat-panel" is stable, prefix is dynamic
           └─ thread scroll layout (.group/thread-scroll-layout)
               ├─ turn container (AXGroup .relative.shrink-0): FLAT list of turn parts (section 6)
               ├─ AXStaticText "ChatGPT can make mistakes. Check important info."
               └─ bottom dock: AXButton "Scroll to bottom", AXHeading "Latest response" (sr-only),
                  AXGroup /AXApplicationStatus (polite), composer (section 4)
```

### Sidebar chat row (each conversation)

```
AXButton "<chat title>"  (AXTitle == AXDescription), ~19-21 DOM classes, actions: AXPress
├─ AXGroup /AXEmptyGroup                       (status dot slot)
├─ AXGroup /AXApplicationGroup #_r_xxx_ ._viewport_* (title marquee)
└─ AXGroup …
   ├─ AXPopUpButton "Chat actions"  #radix-_r_xxx_ (popup=menu)
   └─ AXButton "Pin chat" | "Unpin chat"
```
- `AXPress` on the row **navigates to that conversation, but only when EUI is true**
  (section 2, steps 5 and 6).
- The rows are inside a dnd-kit sortable list (`DndLiveRegion-*` live regions).
- The same conversation can appear twice, for example under a project and under
  "Recents". Both rows carry the same title.

---

## 4. Composer

The same structure was seen in an existing conversation, on the new temporary-chat
screen, and after sending:

```
AXGroup ._ComposerLayoutBody_<hash>
├─ AXGroup /AXEmptyGroup
├─ AXButton "Add files and more"               (AXExpanded=false; opens a portal menu)
├─ AXTextArea "Ask ChatGPT"                     ← THE COMPOSER
├─ AXPopUpButton "Select ChatGPT model"  #radix-…   (absent on a fresh Temporary-chat screen; present after the first send)
├─ AXButton "Dictate"
└─ AXButton "Send" (AXEnabled=false when empty)  ↔  AXButton "Stop" while streaming (same slot, same frame)
```

### `AXTextArea` "Ask ChatGPT"

| Property | Observed |
|---|---|
| Role / roledesc | `AXTextArea` / "text entry area" |
| `AXTitle` = `AXDescription` | `"Ask ChatGPT"` (i18n id `chatgptConversations.composer.placeholder`) |
| `AXDOMClassList` | `["ProseMirror"]`, plus `"ProseMirror-focused"` while focused |
| `AXValue` when empty | **`"Ask ChatGPT\n"`**: the placeholder leaks into the value. `AXNumberOfCharacters = 12`. |
| Settable | `AXValue`, `AXFocused`, `AXSelectedText`, `AXSelectedTextRange`, `AXSelectedTextMarkerRange`, `AXCustomContent` |
| Actions | `AXPress`, `AXShowMenu`, `AXScrollToVisible` |
| Param attrs | Full Chromium text set: `AXStringForRange`, `AXBoundsForRange`, `AXRangeForLine`, `AXReplaceRangeWithText`, `AXTextMarker*`, `AXUIElementsForSearchPredicate`, … |
| Focus | Auto-focused on a new or temporary chat. Stays focused after send. |

**Injection tests (all against a Temporary chat):**

| Test | Result |
|---|---|
| Set `AXFocused=true` | Focuses the composer **and dismisses any open Radix menu** (focus-outside). |
| Set `AXValue="AX probe test one"` | Value reads back exactly. `AXNumberOfCharacters=17`. **Send becomes enabled**, so the ProseMirror model updated. |
| Set `AXValue` to multi-line text containing blank lines, double spaces and `- bullet` | Read-back is **byte-identical**. It renders as one paragraph with line breaks (one `AXStaticText` per line). `- bullet` did **not** turn into a list, so no markdown input rules fired. |
| Set `AXValue=""` | Value goes back to `"Ask ChatGPT\n"` and Send is disabled. |
| Set `AXSelectedText="…"` on an empty composer | **No effect.** Don't use it. |
| Set `AXValue` (2-line prompt), then `AXPress` Send | **The message was sent.** The user turn's text was byte-identical to the injected value, newlines included (section 7). |

**[proposal]** Treat the composer as empty when Send is disabled, or when
`AXValue == AXTitle + "\n"`. Do not use `AXValue == ""`. Not verified: very long
prompts (only 51 to 121 characters were tested), and whether attachments or slash
commands interact with this.

### Other `AXTextArea` nodes (don't confuse them with the composer)

- `AXTextArea` "Start writing" with classes `.ProseMirror._content_<hash>.leading-relaxed`.
  This is an inline **writing block** (document-style artifact) inside an assistant turn.
  It is also a ProseMirror instance with `AXValue` settable. It sits under an
  `AXGroup /AXSectionHeader` that holds the buttons "Add to Library", "Copy" and
  "Open editor".
- Other i18n placeholders exist and may appear in other contexts: "Make a correction"
  (memory correction), "Tell ChatGPT what to write", "Ask for changes".

**[proposal]** The composer selector should be: `AXTextArea` with `AXDescription`
equal to the localized "Ask ChatGPT", **whose parent is the `._ComposerLayoutBody_*`
group** (or which has a sibling `AXButton` "Send"/"Stop"), inside the `*-chat-panel`
group.

---

## 5. Send / Stop

| State | Slot contents | Notes |
|---|---|---|
| Idle, composer empty | `AXButton "Send"` with `AXEnabled=false` | |
| Idle, composer has text | `AXButton "Send"` with `AXEnabled=true` | `AXPress` sends |
| Request in flight / streaming | `AXButton "Stop"` | **The Send node is gone.** Stop was briefly `AXEnabled=false` (about 0.7 s) and then enabled. An `AXElementBusyChanged` notification fired on the Stop button at +1.8 s. |
| Done | `AXButton "Send"` with `AXEnabled=false` | |

- i18n ids: `chatgptConversations.composer.send.ariaLabel` ("Send") and
  `chatgptConversations.composer.stop.ariaLabel` ("Stop").
- **Escape while streaming opens a confirmation dialog** ("Stop this response?", with
  buttons "Keep thinking" / "Stop", i18n `chatgptConversations.keyboardStop.*`).
  Automation must not press Escape to "close menus" during a stream. Use
  `AXFocused=true` on the composer instead (section 8).

---

## 6. Thread / turn structure

All turns are **flat siblings** inside one container (`AXGroup .relative.shrink-0`
under the thread scroll layout). There's no per-turn wrapper element. Boundaries are
hidden headings:

```
AXHeading "You said:"        AXValue=4  .sr-only.m-0.select-none   (i18n codex.conversation.roleHeading.user)
AXGroup                      → AXGroup .text-size-chat → AXGroup .text-size-chat.whitespace-pre-wrap → AXStaticText <user prompt, newlines preserved>
AXGroup .contents → AXButton "Copy message"
AXGroup .contents → AXButton "Share prompt"        (absent in Temporary chat)
AXGroup .contents → AXButton "Edit message"
AXHeading "ChatGPT said:"    AXValue=4                               (i18n codex.conversation.roleHeading.assistant)
<assistant content>          see below
AXGroup .contents → AXButton "Copy"
AXGroup .contents → AXButton "Share"                (absent in Temporary chat)
AXGroup .contents → AXCheckBox/AXToggleButton "Add to project sources"   (project chats only)
AXGroup .contents → AXCheckBox/AXToggleButton "Read aloud"
AXPopUpButton "Regenerate response"   #radix-…  (popup=menu)
AXGroup #radix-… (popup=menu) → AXButton "Rate response"
AXPopUpButton "More actions"          #radix-…  (popup=menu)       (not seen in Temporary chat)
[AXSplitter "Today 9:31"]            ← date separators appear between turns as AXSplitter
AXHeading "You said:" …
```

Assistant content renders as regular AX roles under
`AXGroup .relative._PortalBoundary_*._Renderer_*` (the markdown renderer). That
wrapper can be collapsed into its children:
- paragraphs: `AXGroup ._TextBase_*._Text_*` → `AXStaticText`
- ordered and unordered lists: `AXList/AXContentList ._List_*` → `AXGroup ._Item_*` → `AXListMarker` (`AXValue` "1. ", "2. ", …) and an `AXGroup` of text
- headings: `AXHeading` (`AXValue` = level)
- code: `AXGroup/AXCodeStyleGroup` (roledesc "code")
- tables: `AXTable` / `AXRow` / `AXCell` / `AXColumn` (with "column header" cells and "sortable" header buttons)
- blockquotes: `AXGroup` with roledesc "blockquote"
- writing blocks: see section 4

**Observed text-extraction caveat.** Concatenating the `AXStaticText` values of a list
gives "AppleBananaMango…" with no separators. Extraction has to add a newline per
block element (paragraph, list item, heading, row) and use `AXListMarker` values as
prefixes.

**[proposal]** As an alternative capture source, the "Copy" button on an assistant
turn puts the markdown on the clipboard. That is a clipboard operation, so it falls
under the AGENTS.md live-automation restrictions.

### Off-screen turns and virtualization

- In a 6-turn conversation, **every turn was present** in the tree, including turns
  scrolled out of view. Off-screen nodes report frames clamped to the viewport edge
  (for example `@760,44 2x1`). **Don't use frames for visibility or order.** Use
  sibling order.
- Source: the turn list is **virtualized** (`virtualized-turn-list-*.js`,
  `thread-virtualizer-*.js`, with an overscan count and a rendered range anchored to
  the distance from the bottom). **[inferred]** In long conversations, older turns
  will be missing from the AX tree. The latest turn is always rendered while the view
  is scrolled to the bottom. "Scroll to bottom" is an `AXButton` in the bottom dock.

---

## 7. Send lifecycle (observed timeline)

Test: a Temporary chat. Prompt injected with `AXValue` (2 lines, 121 characters), then
`AXPress` on Send at t=0. Snapshots were taken every 0.5 s, together with an
`AXObserver` on the app element. The model was the default (GPT-6).

| t (s) | Tree state | Notifications |
|---|---|---|
| -0.7 to +0.17 | Composer value set | `AXValueChanged` and `AXSelectedTextChanged` on the composer and the web area |
| +0.17 | — | `AXFocusedUIElementChanged` (composer). `AXLiveRegionCreated` ×5 (one is `DndLiveRegion-22`). |
| +0.73 | **Send gone, `Stop` present (disabled)**. Composer back to `"Ask ChatGPT\n"`. `AXWebArea.AXTitle` = **the prompt text**. Turn container shows `U`. A new `AXGroup/AXApplicationStatus` (`.relative.inline-flex`, polite, **`AXElementBusy=true`**) holds `AXStaticText "ChatGPT is responding"` (i18n `chatgptConversations.loadingMessage`). | — |
| +1.69 | `Stop` enabled. The busy indicator is still there. | +1.82 `AXElementBusyChanged` (on Stop) |
| +4.54 | `AXHeading "ChatGPT said:"` appears. The renderer group is empty (`AXEmptyGroup`, height 0). | — |
| +5.61 | Assistant content present (12-item `AXList`). No action row yet. | +5.77 `AXTitleChanged` on an `AXStaticText` |
| +6.60 | **`Stop` gone, `Send` back (disabled)**. Action row present: Copy, Read aloud, Regenerate response, Rate response. The global live region `.sr-only.select-none` contains **"Response complete"** (i18n `chatgptConversations.responseComplete`). | +6.69 `AXLiveRegionChanged` ×2 |

Notes:
- **No per-token notifications.** No `AXValueChanged` or `AXCreated` fired during
  streaming on the app-level observer. Streaming text has to be polled. This response
  was short (about 1 s of streaming), so intermediate partial-text states were not
  captured.
- In this Temporary chat, the web-area title stayed equal to the prompt text after
  completion. **[not verified]** Whether a normal chat later swaps in a generated title.
- **[proposal]** Completion predicate: Stop absent, **and** Send present, **and** an
  action row exists after the last "ChatGPT said:" heading, **and** the content text is
  stable across 2 or more polls. Use `AXLiveRegionChanged` only as a wake-up hint.
  Polite live regions are reused (a stale "Response complete" from the previous turn
  was already there before send), so their text alone is not proof of completion.
- **[not verified]** Thinking/reasoning models ("Thought for {elapsed}", i18n
  `reasoningItem.thoughtWithElapsed`), tool use, image generation, errors
  ("Could not send this ChatGPT message", `chatgptConversations.submitError`), rate
  limits, and the keyboard-stop dialog were not exercised.

---

## 8. Popovers, menus and dismissal

All Radix popovers mount as **portal siblings of `#root`** under the web area's top
group. They grab focus. Their DOM ids are dynamic (`radix-_r_xxx_`).

| Trigger | Portal content (observed) |
|---|---|
| `AXPopUpButton "Select ChatGPT model"` | `AXGroup "Select ChatGPT model"` (roledesc "menu", FOCUSED, actions include `AXCancel`). Contains `AXMenuItem "Select model"` (view toggle), `AXMenuItem "Power"` (effort slider with keyboard announcement "High, 3 of 3." and "Use Left and Right arrow keys to adjust power"). After pressing "Select model": `AXMenuItem "GPT-6"` (`AXValue=1`, the current model) and `AXMenuItem "GPT-5.6 Sol"` (`AXValue=0`). |
| `AXPopUpButton "Switch mode, current mode: ChatGPT"` | `AXMenu` → `AXMenuItem "ChatGPT Create, learn, and explore"`, `AXMenuItem "Codex Build, debug, and ship"`. **The app has two product modes. The handoff must be in ChatGPT mode.** The trigger label carries the current mode (i18n `sidebarElectron.productMode.trigger`). |
| `AXButton "Add files and more"` | A fixed panel of `AXButton`s: "Add photos & files", "Add from library", "Deep research", "Create image", "Web search", "Sketch", "Documents", "PDF", "Spreadsheets", "Presentations" (title = name and subtitle). |
| `AXPopUpButton "Explore"` (left rail) | `AXGroup/AXApplicationDialog "Explore"` → `AXApplicationGroup`s "Projects", "Sites", "Maps", "GPTs", "Code Review", each holding an `AXButton` with a **semi-stable DOM id** `_r_xxx_-builtin:projects`, `-builtin:sites`, `-builtin:maps`, `-builtin:gpts`, `-builtin:pull-requests`, plus "Pin to sidebar". |

**Dismissal (observed):**
- `AXCancel` on the menu group returns 0 but **does not close** it.
- `AXPress` on the trigger again **does not close** it (the trigger keeps `AXExpanded=true`).
- **Setting `AXFocused=true` on the composer closes it.** This is the reliable,
  keystroke-free way.
- Escape was not used, given the keyboard-stop dialog risk (section 5).

---

## 9. Native menu bar (Electron `Menu`; real AX menu items)

`AXPress` on a native `AXMenuItem` works even when the menu is closed, and **does not
depend on EUI**. It is a robust fallback for navigation.

| Menu | Items relevant to handoff (shortcut) |
|---|---|
| ChatGPT | Settings… (⌘,), Check for Updates…, Log Out, Hide (⌘H), Quit ChatGPT (⌘Q) |
| File | New Window, **New Chat (⌘N)**, **New Temporary Chat (⇧⌘N)**, Open Folder… (⌘O), Close (⌘W) |
| Edit | Undo/Redo, Cut/Copy/Paste (⌘X/⌘C/⌘V), Paste and Match Style (⇧⌘V), Select All (⌘A), … |
| View | Toggle Sidebar (⇧⌘S), Toggle Bottom Panel (⌘J), Toggle Pinned Summary, Open Terminal (⌃\`), Toggle navigation panel (⇧⌘E), Switch between Chat and tabs (⌥⌘B), Browser ▸ (Open Browser Tab ⌘T, Focus Browser Address Bar, Reload Browser Page ⌘R), Find (⌘F), **Previous Chat (⇧⌘[)**, **Next Chat (⇧⌘])**, **Back (⌘[)**, **Forward (⌘])**, Zoom In/Out/Actual Size, Enter Full Screen |
| Window | Minimize, Zoom, Fill, Centre, Move & Resize ▸, Full-Screen Tile ▸, Bring All to Front, a window list entry "ChatGPT", Move to Built-in Retina Display |
| Help | Documentation, Keyboard Shortcuts (⌘/), What's New, Troubleshooting, System Status, Send Feedback, Task Manager, Start Performance Trace |

Observed in this session:
- `AXPress` on "New Temporary Chat" opened a temporary chat.
- `AXPress` on "Back" returned to the previous conversation while web `AXPress` was
  broken (EUI off).

Menu items expose `AXMenuItemCmdChar`, `AXMenuItemCmdModifiers` and
`AXMenuItemCmdVirtualKey`.

---

## 10. Attribute / action census (4,408-node conversation dump)

Roles: `AXStaticText` 2298, `AXGroup` 1251, `AXCell` 376, `AXButton` 153, `AXRow` 76,
`AXHeading` 71, `AXPopUpButton` 61, `AXImage` 34, `AXColumn` 24, `AXCheckBox` 17
(all `/AXToggleButton`), `AXListMarker` 17, `AXList` 10 (all `/AXContentList`),
`AXTable` 10, `AXLink` 3, `AXSplitter` 2, `AXTextArea` 2, plus one each of `AXWindow`,
`AXScrollArea` and `AXWebArea`.

Group subroles seen: `AXEmptyGroup`, `AXApplicationGroup`, `AXCodeStyleGroup`,
`AXApplicationStatus` (live regions), `AXApplicationDialog`, `AXLandmarkNavigation`,
`AXLandmarkMain`, `AXLandmarkBanner`, `AXLandmarkComplementary`, `AXSectionHeader`.

Chromium-specific attributes present on web nodes: **`AXDOMIdentifier`** (the HTML
`id`), **`AXDOMClassList`** (the HTML classes), `ChromeAXNodeId`, `AXElementBusy`,
`AXARIALive`, `AXARIARelevant`, `AXARIAAtomic`, `AXHasPopup` / `AXPopupValue`,
`AXExpanded`, `AXRequired`, `AXVisited`, `AXBlockQuoteLevel`, `AXFocusableAncestor`,
`AXEditableAncestor`, `AXStartTextMarker` / `AXEndTextMarker`, `AXCustomContent`.
**`AXIdentifier` is never set** on web nodes. Note that `data-*` attributes such as
`data-testid` and `data-turn-key` are **not** exposed through AX.

Actions with EUI=true: every web element has `AXPress`, `AXShowMenu` and
`AXScrollToVisible`. Menu content adds `AXCancel`. With EUI=false only `AXShowMenu`
and `AXScrollToVisible` remain.

Settable (web): `AXFocused` on focusable elements. `AXValue` on `AXTextArea`,
`AXSplitter` and (oddly) generic text groups. `AXSelectedTextMarkerRange` everywhere.

Parameterized attributes (web): the full Chromium set, including
`AXUIElementsForSearchPredicate` / `AXUIElementCountForSearchPredicate`. **[proposal]**
These could replace full tree walks for finding headings, buttons and so on.
`AXTable` adds `AXCellForColumnAndRow`.

---

## 11. Selector stability guide

| Selector kind | Stability | Examples |
|---|---|---|
| Fixed DOM ids | **Stable** | `#root`, `#app-shell-sidebar` |
| DOM id suffixes | Stable suffix, dynamic prefix | `*-chat-panel`, `*-builtin:projects` |
| Radix / React ids | **Unstable** (renumbered per render) | `radix-_r_4hf_`, `_r_3r5_` |
| Plain utility classes | Medium (design churn) | `ProseMirror`, `app-shell-left-panel`, `sr-only`, `group/nav-section`, `group/thread-scroll-layout` |
| CSS-module classes | **Unstable across app versions** (hash suffix) | `_ComposerLayoutBody_15oep_2`, `_Button_eg66f_2`, `_Renderer_1iy3o_1`. Match the prefix only (`_ComposerLayoutBody_`), if at all. |
| Role + subrole | Stable | `AXTextArea`, `AXHeading`, `AXGroup/AXLandmarkMain` |
| Accessible names | Stable per locale, **localized** | "Ask ChatGPT", "Send", "Stop", "You said:", "ChatGPT said:" |
| Index paths | **Unstable**: portals shift sibling indexes | `0.0.…` |
| Frames | Unreliable for off-screen nodes (clamped) | |

`tools/handoff_label_catalog.tsv` maps each relevant label to its i18n message id, so
labels can be re-checked after an app update by re-extracting `app.asar`.

---

## 12. Static source findings (`app.asar`, read-only extraction)

- 20,915 files. The renderer is `webview/assets/*.js` (Vite chunks). The main process
  is `.vite/build/*.js`. Package name `openai-codex-electron`. Build scripts mention
  "owl" (`owl-shell`, `ensure-owl-electron-types`), so **[inferred]** this is a custom
  Electron shell. That probably explains why `AXManualAccessibility` is unsupported.
  The main bundle contains no `setAccessibilitySupportEnabled` call.
- i18n catalog: 49,464 message ids extracted (`id`/`defaultMessage`/`description`).
  The handoff-relevant ids are in `tools/handoff_label_catalog.tsv`. Key ones:
  - `chatgptConversations.composer.placeholder` = "Ask ChatGPT"
  - `chatgptConversations.composer.send.ariaLabel` = "Send"
  - `chatgptConversations.composer.stop.ariaLabel` = "Stop"
  - `codex.conversation.roleHeading.user` = "You said:"
  - `codex.conversation.roleHeading.assistant` = "ChatGPT said:"
  - `codex.conversation.latestResponse` = "Latest response"
  - `chatgptConversations.loadingMessage` = "ChatGPT is responding"
  - `chatgptConversations.responseComplete` = "Response complete"
  - `chatgptConversations.modelPicker.ariaLabel` = "Select ChatGPT model"
  - `sidebarElectron.productMode.trigger` = "Switch mode, current mode: {mode}"
  - `chatgptConversations.keyboardStop.title` = "Stop this response?"
  - `chatgptConversations.submitError` = "Could not send this ChatGPT message"
- The turn list is virtualized (`virtualized-turn-list-*.js`, `thread-virtualizer-*.js`).

---

## 13. Impact on the current code (code-confirmed references)

| Current assumption in `agent/` | New app reality |
|---|---|
| `CLASSIC_CHATGPT_BUNDLE_ID = "com.openai.chat"` (`agent/mac_app_control.py:10`). `activate_chatgpt` maps app name "chatgpt" to that bundle (`agent/mac_app_control.py:181`). | The new app is `com.openai.codex`. Name-based targeting ("ChatGPT") is now ambiguous: System Events reports the new app's process as `ChatGPT`. |
| pid resolved through `resolve_classic_chatgpt_pid` (`agent/chatgpt_ax_destination_snapshot.py`, used by `agent/mac_ui_inspect.py` and `agent/chatgpt_ax_capture.py`) | A new resolver by bundle id is needed. **[proposal]** |
| Capture container `AXGroup` / `AXHostingView` (`agent/chatgpt_ax_capture.py:55-56`) | No `AXHostingView`. Containers are web `AXGroup`s. Turns are delimited by the "You said:" / "ChatGPT said:" headings. |
| Composer hints "message chatgpt", "ask anything", "send a message" (`agent/chatgpt_ax_destination_snapshot.py:508`) | The label is "Ask ChatGPT". Value leaks the placeholder when empty. |
| Clipboard paste for text entry (`agent/mac_paste.py`) | **Direct `AXValue` set works** (section 4). Clipboard and keystrokes are not required for text entry. |
| Identity read from `AXIdentifier` (snapshot code reads it) | Web nodes never have `AXIdentifier`. Use `AXDOMIdentifier` / `AXDOMClassList`, which the current code does not read. |
| No tree-enable step | Requires `AXEnhancedUserInterface=true` and an on-screen window (section 2). |

---

## 14. Gaps / not tested

- A true cold launch (does reading AX alone trigger the web tree, or is EUI required?).
- A long streaming response (partial-text growth pattern), thinking models, tool and
  web-search turns, image generation, attachments, error and rate-limit states, and
  the "Stop this response?" dialog.
- Very long prompts through `AXValue` (size limits, performance), and IME edge cases.
- A conversation long enough to trigger turn virtualization, to observe which turns drop out.
- The Codex mode UI (only the switcher was opened).
- Multiple windows or tabs ("New Window", "New tab", "Switch between Chat and tabs").
- How the Classic and new apps coexist when **both** are running (process names, `tell application "ChatGPT"`).
- Settings window, profile menu, chat-actions menus (not opened).
- Whether Chromium's accessibility auto-disable eventually drops the tree if AX
  clients go quiet for a long time.

---

## 15. Side effects of this research session

- `AXEnhancedUserInterface` on the new app was **left at `true`**. It was `false`
  before the session. It resets when the app restarts.
- One **Temporary chat** was created and sent:
  > "Accessibility probe test (automated, please ignore). Reply with a numbered list of 12 fruits, one per line, nothing else."

  Temporary chats are not kept in history.
- The model picker, "Add files and more", "Explore" and mode-switcher popovers were
  opened and closed. No setting or selection was changed.
- Navigation went to one other conversation in the same project and back. The app was
  left on the conversation that was open at the start.
- Finder was briefly activated for the background test, then ChatGPT was re-activated.

---

## 16. Reproducing (tools in `docs/new-chatgpt-app/tools/`)

These are research tools only. Building `axprobe` requires Xcode command line tools,
which are already present. Nothing gets installed.

```sh
cd docs/new-chatgpt-app/tools
swiftc -O axprobe.swift -o "$TMPDIR/axprobe"

"$TMPDIR/axprobe" info                                   # app attrs, trust, EUI state
"$TMPDIR/axprobe" enable --attr AXEnhancedUserInterface   # expect -25208; verify with `info`
"$TMPDIR/axprobe" dump --root window --out "$TMPDIR/win.json"
"$TMPDIR/axprobe" dump --root menubar --full --out "$TMPDIR/menubar.json"
python3 render.py "$TMPDIR/win.json" --collapse --no-text  # outline
python3 render.py "$TMPDIR/win.json" --census              # roles/attrs/actions census
"$TMPDIR/axprobe" find --root window --match 'role=AXTextArea,desc=Ask ChatGPT'
"$TMPDIR/axprobe" observe --seconds 30                     # AX notifications
python3 -I asar_read.py /Applications/ChatGPT.app/Contents/Resources/app.asar "$TMPDIR/asar"
```

`--match` syntax: comma-separated `key=value` pairs, where `key~=value` means
"contains". Keys are `role`, `subrole`, `title`, `desc`, `value`, `domid`, `class`,
`rd`, `help`, `ph`, `focused`. **Values must not contain commas.**

Live actions (`act`, `set`, `menuprobe.sh`, `poll.sh`) drive the real app. Per
AGENTS.md, run them only when a live action is explicitly requested.

---

## 17. App update observed 2026-11-20 (same pid 72274, so the update landed before that process started)

These changes were read live and read-only during Step 2 of `MIGRATION_PLAN.md`.

- **The `*-chat-panel` DOM id is gone.**
  - Path from `AXLandmarkMain` (id `_r_58i_`, still dynamic) to the transcript:
    `WorkspaceContent-*` -> `MainContentViewport-*` -> `MainContentFrame-*` -> ...
    -> `.group/thread-scroll-layout` -> `AXGroup/AXLandmarkRegion`
    `.scrollContainer-*.thread-scroll-container` -> `.relative.shrink-0`, which is
    the turn list.
  - **`group/thread-scroll-layout` survived the update.** The reader now uses it as
    the chat-panel anchor when no `-chat-panel` id exists.
- **The composer moved inside the scroll container.**
  - Path: `.thread-scroll-container` -> `footer-*` -> ... ->
    `AXGroup/AXFieldset` -> `ComposerLayoutBody-*` -> `AXTextArea "Ask ChatGPT"`
    `.ProseMirror`.
  - "Send" is a sibling of the composer in the same `ComposerLayoutBody-*` group.
  - The labels didn't change.
- **The CSS-module class format changed** from `_Name_hash_n` (for example
  `_ComposerLayoutBody_15oep_2`) to `Name-hash` (for example
  `ComposerLayoutBody-uBBf1A`). This confirms section 11: never match these
  classes.
- **Title bar (`AXLandmarkBanner`) changes.**
  - The conversation title is now an **`AXPopUpButton`** (it was an `AXButton`),
    inside `.group/titlebar`.
  - No `AXImage "Project: <name>"` was present in the chat that was open. It isn't
    known yet whether that chat is in a project. Re-check with a known project
    chat before building the Step 4 gate.
- **More empty siblings of `#root`:** 8 `AXGroup/AXEmptyGroup` nodes (a
  `fixed inset-0 pointer-events-none` layer, `contents` wrappers, and empty
  `PortalBoundary-*` renderers). None are menus or dialogs.
- **Live reader timings in this conversation** (8 turn headings):
  - locate all anchors: 0.05 s (120 nodes visited)
  - sidebar snapshot: 682 nodes in 0.07 s
  - chat panel snapshot: 462 nodes in 0.04 s

## 18. Sidebar navigation details (read live and read-only, 2026-11-20)

- **The open conversation's sidebar row has `AXARIACurrent = "page"`**
  (aria-current). No other row has it. `AXSelected` is false on every row, and
  `AXARIACurrent` is absent rather than empty on the other rows.
  - The row also gets the static class `bg-secondary-ghost-hover`, but use the
    attribute, not the class.
  - The same chat's copy under Recents/Pinned **can also** carry
    `AXARIACurrent = "page"`. It was seen with and without it. Only use the copy
    inside `"Chats in <project>"`.
- **Projects section:**
  - Header `AXButton "Projects"` (`AXExpanded`).
  - Each project row is `AXButton "<project>"` with classes
    `sidebar-item sidebar-row-focus`, has `AXExpanded`, and contains
    `AXPopUpButton "Project actions for <project>"` and
    `AXButton "New chat in <project>"`.
  - An expanded project is followed by a sibling
    `AXList/AXContentList "Chats in <project>"`.
  - Each chat row is `AXButton "<chat>"` (class `sidebar-item`) containing
    `AXButton "Pin chat"`/`"Unpin chat"` and `AXPopUpButton "Chat actions"`.
  - When more chats exist, the list ends with `AXButton "Show more"`.
  - A collapsed project (`AXExpanded = false`) has no chat list.
- **Pinned** can hold pinned chats and pinned projects. Pinned project rows also
  carry "Project actions for <project>". Navigation therefore only searches
  inside the "Projects" section.
- Row attributes available on a chat row include `AXURL`, `AXVisited` and
  `AXLinkedUIElements`. `AXURL` was empty, so there is still no conversation ID.

## 19. The AX tree only updates while the window is on screen (live, 2026-11-20)

- `AXPress` on a sidebar chat row **works with the app in the background**: the
  conversation switched every time.
- **While the window was not on screen** (fullscreen on its own Space, the user
  working in a terminal elsewhere), the AX tree kept showing the **previous
  state** for at least 5 s:
  - the old row still had `AXARIACurrent = "page"`
  - and/or the web area title was still the old chat
- When the user brought the app forward, the tree caught up within about
  0.7 to 1.1 s.
- **Most likely cause** (not verified): Chromium throttles rendering and AX
  serialization for occluded or hidden windows.
- **Consequence:** any step that must *observe* the app react has to bring it to
  the front first. That covers navigation confirmation, send confirmation and
  reply capture. `preflight_new_chatgpt(bring_to_front=True)` does that and
  waits for `AXFrontmost`.
- This corrects the earlier note (section 0 and the Step 1 summary) that
  "after activation it stayed readable in the background". It stays
  *readable*, but it goes *stale*.

## 20. Where the open chat's project is shown (live, read-only, 2026-11-20)

- In the 2026-11-20 build, **the project name appears nowhere in the AX tree
  outside `#app-shell-sidebar`**. This was checked across the whole web area
  (about 1,640 nodes) with a known project chat open.
  - The title bar holds only `AXPopUpButton "<chat title>"` (`AXHasPopup`,
    popup = menu).
  - The 2026-11-18 `AXImage "Project: <name>"` is gone.
- **The window title (`AXWindow.AXTitle`) is the conversation title.** It was
  "ChatGPT" in the 2026-11-18 dumps. It equals the web area title and the
  title-bar button label. None of them includes the project.
- So the only project signal is the sidebar: the project whose
  `"Chats in <project>"` list (in the "Projects" section) holds the row with
  `AXARIACurrent = "page"`.

## 21. A finished reply with a Codex prompt (live, read-only, 2026-11-20)

These were read from a real project chat whose latest reply carried a
`BEGIN/END_NEXT_CODEX_PROMPT` block in a "Plain text" code block. About 8,460
characters of text were rebuilt and a 7,491-character prompt was extracted.

- **Footer of a finished reply**, in order: `Copy`, `Rate response`,
  `Read aloud`, `Share`, `Add to project sources`, `More actions`.
  - `Regenerate response` was **not** a separate footer control on this reply.
    It is not needed for completion.
- **Thinking summary.** "Worked for 1m 37s" is a **button** inside the
  assistant turn. Its text must not be counted as reply text.
- **Code block.**
  - It sits under a header whose text "Plain text" is an `AXStaticText`, plus a
    toolbar with its own copy button inside the body. The capture keeps "Plain
    text" as one line before the code. It is outside the sentinels, so the
    prompt is unaffected.
  - Lines up to 368 characters came through whole.
- **Bold run-in labels.** "What we're doing:" plus the sentence are separate
  `AXStaticText` siblings in one paragraph group. Joining them with "" gives the
  right text.
- The reply was finished and stable on the first two polls, 1 s apart, with no
  `Stop` next to the composer.

## 22. The composer drops a trailing newline (first real supervised run, 2026-11-20)

- The loop's feedback message ends with `"\n"`. After setting `AXValue`, the
  composer read back the same text **without the final newline**. Inner blank
  lines and spacing were preserved exactly.
- The read-back required an exact match (allowing only an *added* trailing
  newline). So every attempt failed with `composer_value_mismatch`. Send was
  never pressed, and the handoff retried 28 times, each with a new marker nonce.
- **Fix:** the read-back now ignores trailing newlines only
  (`agent/new_chatgpt_submit.py` `_value_matches`), with regression tests.
- **Lesson:** the earlier live send test used a single line with no trailing
  newline. Test the live paths with the loop's real message shape.
