"""Synthetic AX trees shaped like the new ChatGPT app (``com.openai.codex``).

Structure follows docs/new-chatgpt-app/NEW_CHATGPT_APP_AX_FINDINGS.md sections 3-6.
All content is made up; no real conversation data is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field

AX_ERROR_NOT_IMPLEMENTED = -25208


class Node:
    """Synthetic AX element."""

    def __init__(self, role: str, children: list["Node"] | None = None, **attrs) -> None:
        self.attrs = {"AXRole": role, **attrs}
        self.children = list(children or [])

    def __repr__(self) -> str:
        a = self.attrs
        return f"Node({a.get('AXRole')}, {a.get('AXDOMIdentifier') or a.get('AXDescription') or a.get('AXTitle') or ''})"

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


class FakeBackend:
    def __init__(
        self,
        app: Node,
        *,
        trusted: bool = True,
        eui_set_error: int = AX_ERROR_NOT_IMPLEMENTED,
        eui_sticks: bool = True,
        bulk: bool = True,
    ) -> None:
        self.app = app
        self.trusted = trusted
        self.eui_set_error = eui_set_error
        self.eui_sticks = eui_sticks
        self.set_calls: list[tuple[Node, str, bool]] = []
        self.on_focus_composer = None
        self.read_nodes: list[Node] = []
        self.actions: list[tuple[Node, str]] = []
        # Called as on_press(node) after AXPress; lets tests simulate the app reacting.
        self.on_press = None
        # Per-node AXPress handlers, checked before on_press.
        self.press_handlers: dict[int, object] = {}
        # Called as on_set_string(node, name, value); may return an AXError to refuse.
        self.on_set_string = None
        if not bulk:
            self.attributes = None  # type: ignore[assignment]

    def is_trusted(self) -> bool:
        return self.trusted

    def application(self, pid: int) -> Node:
        return self.app

    def attribute(self, element: Node, name: str):
        self.read_nodes.append(element)
        if name == "AXChildren":
            return list(element.children)
        return element.attrs.get(name)

    def attributes(self, element: Node, names):  # noqa: F811 - may be disabled per instance
        self.read_nodes.append(element)
        return {name: (list(element.children) if name == "AXChildren" else element.attrs.get(name)) for name in names}

    def perform(self, element: Node, action: str) -> int:
        self.actions.append((element, action))
        handler = self.press_handlers.get(id(element))
        if action == "AXPress" and handler is not None:
            result = handler()
            return 0 if result is None else result
        if action == "AXPress" and self.on_press is not None:
            result = self.on_press(element)
            return 0 if result is None else result
        return 0

    def set_string(self, element: Node, name: str, value: str) -> int:
        self.set_calls.append((element, name, value))
        if self.on_set_string is not None:
            refused = self.on_set_string(element, name, value)
            if refused is not None:
                return refused
            return 0
        element.attrs[name] = value
        return 0

    def set_bool(self, element: Node, name: str, value: bool) -> int:
        self.set_calls.append((element, name, value))
        if name == "AXEnhancedUserInterface":
            if self.eui_sticks:
                element.attrs[name] = value
            return self.eui_set_error
        if name == "AXFocused":
            element.attrs[name] = value
            if self.on_focus_composer is not None:
                self.on_focus_composer()
            return 0
        return -25205


def live_region(dom_id: str | None = None) -> Node:
    attrs = {"AXSubrole": "AXApplicationStatus"}
    if dom_id:
        attrs["AXDOMIdentifier"] = dom_id
    return Node("AXGroup", [Node("AXStaticText", AXValue="Response complete")], **attrs)


def radix_menu() -> Node:
    return Node(
        "AXGroup",
        [Node("AXMenu", [Node("AXMenuItem", AXTitle="ChatGPT Create, learn, and explore")])],
        AXDOMIdentifier="radix-_r_4hf_",
    )


def labelled(role: str, label: str, children: list[Node] | None = None, **attrs) -> Node:
    return Node(role, children, AXTitle=label, AXDescription=label, **attrs)


def text(value: str) -> Node:
    return Node("AXStaticText", AXValue=value)


def user_turn(prompt: str) -> list[Node]:
    return [
        Node("AXHeading", AXTitle="You said:", AXValue=4, AXDOMClassList=["sr-only", "m-0", "select-none"]),
        Node("AXGroup", [Node("AXGroup", [Node("AXGroup", [text(prompt)], AXDOMClassList=["text-size-chat", "whitespace-pre-wrap"])], AXDOMClassList=["text-size-chat"])]),
        Node("AXGroup", [labelled("AXButton", "Copy message")], AXDOMClassList=["contents"]),
        Node("AXGroup", [labelled("AXButton", "Edit message")], AXDOMClassList=["contents"]),
    ]


def assistant_turn(paragraphs: list[str], *, finished: bool = True) -> list[Node]:
    body = Node(
        "AXGroup",
        [Node("AXGroup", [text(p)], AXDOMClassList=["_TextBase_x1", "_Text_x2"]) for p in paragraphs],
        AXDOMClassList=["relative", "_PortalBoundary_1q7hk_2", "_Renderer_1iy3o_1"],
    )
    parts = [Node("AXHeading", AXTitle="ChatGPT said:", AXValue=4, AXDOMClassList=["sr-only", "m-0", "select-none"]), body]
    if finished:
        parts += [
            Node("AXGroup", [labelled("AXButton", "Copy")], AXDOMClassList=["contents"]),
            Node("AXGroup", [Node("AXCheckBox", AXDescription="Read aloud", AXSubrole="AXToggleButton")], AXDOMClassList=["contents"]),
            labelled("AXPopUpButton", "Regenerate response"),
            Node("AXGroup", [labelled("AXButton", "Rate response")]),
        ]
    return parts


# --- sidebar ------------------------------------------------------------------------


def chat_row(title: str, *, current: bool = False, pinned: bool = False) -> Node:
    """Chat row as read live on 2026-11-20 (aria-current="page" marks the open chat)."""

    attrs = {"AXDOMClassList": ["sidebar-item"]}
    if current:
        attrs["AXARIACurrent"] = "page"
    return labelled(
        "AXButton",
        title,
        [
            Node("AXGroup", AXSubrole="AXEmptyGroup"),
            Node("AXGroup", AXSubrole="AXApplicationGroup"),
            Node("AXGroup", [labelled("AXButton", "Unpin chat" if pinned else "Pin chat"), labelled("AXPopUpButton", "Chat actions")]),
        ],
        **attrs,
    )


def project_row(name: str, *, expanded: bool | None) -> Node:
    attrs = {"AXDOMClassList": ["sidebar-item", "sidebar-row-focus"]}
    if expanded is not None:
        attrs["AXExpanded"] = expanded
    return labelled(
        "AXButton",
        name,
        [
            Node("AXGroup"),
            Node("AXGroup", [Node("AXGroup", [labelled("AXPopUpButton", f"Project actions for {name}")]), labelled("AXButton", f"New chat in {name}")]),
        ],
        **attrs,
    )


@dataclass
class FakeProject:
    name: str
    chats: list[str]
    expanded: bool = True
    shown: int | None = None  # chats visible before "Show more"; None = all


class SidebarModel:
    """Mutable sidebar that re-renders after presses, like the live app.

    Presses on rendered nodes are dispatched through ``press(node)``; pressing a
    chat row opens it (``current``) unless ``rows_respond`` is False.
    """

    SHOW_MORE_STEP = 5

    def __init__(
        self,
        projects: list[FakeProject],
        *,
        current: tuple[str, str] | None = None,
        pinned_chats: list[str] | None = None,
        pinned_projects: list[str] | None = None,
        recents: list[str] | None = None,
        section_expanded: bool = True,
        projects_shown: int | None = None,
        rows_respond: bool = True,
        recents_current_too: bool = False,
    ) -> None:
        self.projects = projects
        self.current = current
        self.pinned_chats = pinned_chats or []
        self.pinned_projects = pinned_projects or []
        self.recents = recents or []
        self.section_expanded = section_expanded
        self.projects_shown = projects_shown
        self.rows_respond = rows_respond
        # Live 2026-11-20: the Recents copy of the open chat was seen both with and
        # without aria-current="page".
        self.recents_current_too = recents_current_too
        self.container: Node | None = None
        self.on_navigate = None
        self._actions: dict[int, object] = {}

    def render(self) -> list[Node]:
        self._actions = {}
        pinned_children = [chat_row(title, pinned=True) for title in self.pinned_chats]
        pinned_children += [project_row(name, expanded=False) for name in self.pinned_projects]
        pinned = Node(
            "AXGroup",
            [labelled("AXButton", "Pinned", AXExpanded=True), Node("AXList", pinned_children, AXSubrole="AXContentList")],
            AXDOMClassList=["group/nav-section"],
        )

        header = labelled("AXButton", "Projects", AXExpanded=self.section_expanded)
        self._actions[id(header)] = self._toggle_section
        project_nodes: list[Node] = []
        if self.section_expanded:
            visible = self.projects if self.projects_shown is None else self.projects[: self.projects_shown]
            for project in visible:
                row = project_row(project.name, expanded=project.expanded)
                self._actions[id(row)] = lambda p=project: self._toggle_project(p)
                project_nodes.append(row)
                if not project.expanded:
                    continue
                shown = project.chats if project.shown is None else project.chats[: project.shown]
                items: list[Node] = []
                for title in shown:
                    is_current = self.current == (project.name, title)
                    item = chat_row(title, current=is_current)
                    self._actions[id(item)] = lambda p=project, t=title: self._open(p.name, t)
                    items.append(item)
                if project.shown is not None and project.shown < len(project.chats):
                    more = labelled("AXButton", "Show more")
                    self._actions[id(more)] = lambda p=project: self._more_chats(p)
                    items.append(more)
                project_nodes.append(Node("AXList", items, AXSubrole="AXContentList", AXDescription=f"Chats in {project.name}"))
            if self.projects_shown is not None and self.projects_shown < len(self.projects):
                more = labelled("AXButton", "Show more")
                self._actions[id(more)] = self._more_projects
                project_nodes.append(more)
        projects = Node("AXGroup", [header, Node("AXGroup", project_nodes)], AXDOMClassList=["group/nav-section"])

        recents = Node(
            "AXGroup",
            [
                labelled("AXButton", "Recents", AXExpanded=True),
                Node(
                    "AXList",
                    [chat_row(t, current=self.recents_current_too and self.current is not None and self.current[1] == t) for t in self.recents],
                    AXSubrole="AXContentList",
                ),
            ],
            AXDOMClassList=["group/nav-section"],
        )
        return [pinned, projects, recents]

    def rerender(self) -> None:
        if self.container is not None:
            keep = [child for child in self.container.children if "group/nav-section" not in child.attrs.get("AXDOMClassList", [])]
            self.container.children = keep + self.render()

    def press(self, node: Node):
        action = self._actions.get(id(node))
        if action is None:
            return None
        action()
        self.rerender()
        return 0

    def _toggle_section(self) -> None:
        self.section_expanded = not self.section_expanded

    def _toggle_project(self, project: FakeProject) -> None:
        project.expanded = not project.expanded

    def _more_chats(self, project: FakeProject) -> None:
        project.shown = (project.shown or 0) + self.SHOW_MORE_STEP

    def _more_projects(self) -> None:
        self.projects_shown = (self.projects_shown or 0) + self.SHOW_MORE_STEP

    def _open(self, project: str, chat: str) -> None:
        if not self.rows_respond:
            return
        self.current = (project, chat)
        if self.on_navigate is not None:
            self.on_navigate(chat)


def default_sidebar_model() -> SidebarModel:
    return SidebarModel(
        [FakeProject("<project>", ["<chat 0>", "<chat 1>", "<chat 2>"])],
        current=("<project>", "<chat 0>"),
        recents=["<chat 0>"],
    )


@dataclass
class AppTree:
    app: Node
    window: Node
    web_area: Node
    top_group: Node
    root: Node
    sidebar: Node | None
    banner: Node
    main: Node
    chat_panel: Node
    turn_list: Node | None
    composer: Node
    send: Node
    sidebar_model: SidebarModel | None = None
    extra: dict = field(default_factory=dict)

    def wire(self, backend: "FakeBackend", *, stale_in_background: bool = False) -> "FakeBackend":
        """Make presses on sidebar rows behave like the live app (re-render, navigate).

        ``stale_in_background=True`` reproduces the live finding of 2026-11-20:
        presses take effect, but the AX tree only shows it once the app is
        frontmost (``bring_to_front``).
        """

        model = self.sidebar_model
        self._pending: list = []
        self._stale = stale_in_background

        def navigate(chat: str) -> None:
            self.web_area.attrs["AXTitle"] = chat
            self.window.attrs["AXTitle"] = chat
            for node in self.banner.walk():
                if node.attrs.get("AXRole") == "AXPopUpButton":
                    node.attrs["AXTitle"] = node.attrs["AXDescription"] = chat

        def deferred_navigate(chat: str) -> None:
            if self._stale and self.app.attrs.get("AXFrontmost") is not True:
                self._pending.append(lambda: navigate(chat))
            else:
                navigate(chat)

        def press(node):
            if model is None:
                return None
            if self._stale and self.app.attrs.get("AXFrontmost") is not True:
                original = model.rerender
                model.rerender = lambda: self._pending.append(original)
                try:
                    return model.press(node)
                finally:
                    model.rerender = original
            return model.press(node)

        if model is not None:
            model.on_navigate = deferred_navigate
            backend.on_press = press
        return backend

    def wire_composer(
        self,
        backend: "FakeBackend",
        *,
        presses_needed: int = 1,
        show_stop: bool = True,
        clear_composer: bool = True,
        readback_suffix: str = "",
        update_after_ticks: int = 0,
        drop_trailing_newlines: bool = False,
    ) -> "FakeBackend":
        """Make the composer and Send button behave like the live app (F-4, F-5, F-7).

        - Setting AXValue updates the composer (reading back ``value + readback_suffix``)
          and enables Send when there is text.
        - Pressing Send ``presses_needed`` times sends: a new "You said:" turn with
          the text appears, the composer empties, and Send becomes Stop.
          Earlier presses do nothing (like AXEnhancedUserInterface being off).
        - ``update_after_ticks`` delays the visible effect until ``tick()`` is
          called that many times (tests call it from their fake ``sleep``).
        """

        placeholder = "Ask ChatGPT\n"
        self._ticks_left = 0
        self._deferred: list = []
        presses = {"count": 0}

        def on_set(node: Node, name: str, value: str):
            if drop_trailing_newlines:
                value = value.rstrip("\n")  # what the live composer did on 2026-11-20
            node.attrs[name] = (value + readback_suffix) if value else placeholder
            if node is self.composer:
                self.send.attrs["AXEnabled"] = bool(value.strip())
            return None

        def send_now(text: str) -> None:
            turns = user_turn(text)
            if self.turn_list is None:
                self.turn_list = Node("AXGroup", [], AXDOMClassList=["relative", "shrink-0"])
                self.chat_panel.children.insert(0, self.turn_list)
            self.turn_list.children.extend(turns)
            if clear_composer:
                self.composer.attrs["AXValue"] = placeholder
            if show_stop:
                self.send.attrs["AXDescription"] = self.send.attrs["AXTitle"] = "Stop"
                self.send.attrs["AXEnabled"] = True
            else:
                self.send.attrs["AXEnabled"] = False

        def on_press_send():
            if self.send.attrs.get("AXDescription") != "Send" or self.send.attrs.get("AXEnabled") is not True:
                return None
            presses["count"] += 1
            if presses["count"] < presses_needed:
                # AXPress "succeeds" but nothing happens, as when the app reset
                # AXEnhancedUserInterface (e.g. after a restart).
                self.app.attrs["AXEnhancedUserInterface"] = False
                return None
            text = self.composer.attrs.get("AXValue", "")
            text = text[: -len(readback_suffix)] if readback_suffix and text.endswith(readback_suffix) else text
            if update_after_ticks:
                self._ticks_left = update_after_ticks
                self._deferred.append(lambda: send_now(text))
            else:
                send_now(text)
            return None

        backend.on_set_string = on_set
        backend.press_handlers[id(self.send)] = on_press_send
        self.presses = presses
        return backend

    def tick(self) -> None:
        if getattr(self, "_ticks_left", 0) > 0:
            self._ticks_left -= 1
            if self._ticks_left == 0:
                deferred, self._deferred = self._deferred, []
                for update in deferred:
                    update()

    def bring_to_front(self, bundle_id: str = "com.openai.codex") -> dict:
        """Fake activator: the app becomes frontmost and deferred updates land."""

        self.app.attrs["AXFrontmost"] = True
        pending, self._pending = getattr(self, "_pending", []), []
        for update in pending:
            update()
        return {"activated": True, "bundle_id": bundle_id}


def build_app_tree(
    *,
    mode: str | None = "ChatGPT",
    attached: bool = True,
    overlays: list[Node] | None = None,
    with_sidebar: bool = True,
    turns: list[Node] | None = None,
    composer_count: int = 1,
    legacy_chat_panel_id: bool = False,
    sidebar_model: SidebarModel | None = None,
) -> AppTree:
    """Build a synthetic app. ``turns=None`` gives one finished exchange; ``[]`` gives an empty chat.

    By default the layout matches the 2026-11-20 app build (no ``-chat-panel`` DOM
    id; ``chat_panel`` is the ``group/thread-scroll-layout`` element).
    ``legacy_chat_panel_id=True`` reproduces the 2026-11-18 build, where an outer
    element carried a DOM id ending ``-chat-panel``.
    """

    composers = [
        Node(
            "AXTextArea",
            AXTitle="Ask ChatGPT",
            AXDescription="Ask ChatGPT",
            AXValue="Ask ChatGPT\n",
            AXDOMClassList=["ProseMirror"],
        )
        for _ in range(composer_count)
    ]
    send = Node("AXButton", AXDescription="Send", AXEnabled=False)
    decoy_text_area = Node("AXTextArea", AXTitle="Search", AXDOMClassList=["search-input"])

    nav_header: list[Node] = []
    if mode is not None:
        label = f"Switch mode, current mode: {mode}"
        nav_header.append(Node("AXPopUpButton", AXDescription=label, AXTitle=label))
    nav_header.append(labelled("AXButton", "New chat"))
    model = sidebar_model or default_sidebar_model()
    nav = Node("AXGroup", [*nav_header, *model.render()], AXSubrole="AXLandmarkNavigation", AXDescription="Home")
    model.container = nav
    sidebar = Node("AXGroup", [Node("AXGroup", [nav])], AXDOMIdentifier="app-shell-sidebar")
    current_chat = model.current[1] if model.current else "<chat 0>"
    banner = Node(
        "AXGroup",
        [
            labelled("AXButton", "Back"),
            labelled("AXPopUpButton", current_chat),
            labelled("AXButton", "Share"),
        ],
        AXSubrole="AXLandmarkBanner",
    )

    if turns is None:
        turns = user_turn("<prompt>") + assistant_turn(["<reply paragraph 1>", "<reply paragraph 2>"])
    turn_list = Node("AXGroup", turns, AXDOMClassList=["relative", "shrink-0"]) if turns else None

    composer_group = Node(
        "AXGroup",
        [
            Node("AXGroup", AXSubrole="AXEmptyGroup"),
            labelled("AXButton", "Add files and more", AXExpanded=False),
            *composers,
            labelled("AXButton", "Dictate"),
            send,
        ],
        AXDOMClassList=["_ComposerLayoutBody_15oep_2"],
    )
    dock = Node(
        "AXGroup",
        [
            labelled("AXButton", "Scroll to bottom"),
            Node("AXHeading", AXTitle="Latest response", AXValue=2, AXDOMClassList=["sr-only"]),
            Node("AXGroup", AXSubrole="AXApplicationStatus"),
            composer_group,
        ],
    )
    layout_children = ([turn_list] if turn_list else []) + [text("ChatGPT can make mistakes. Check important info."), dock]
    scroll_layout = Node("AXGroup", layout_children, AXDOMClassList=["group/thread-scroll-layout", "relative", "h-full"])
    panel_attrs = {"AXDOMIdentifier": "_r_1a_-chat-panel"} if legacy_chat_panel_id else {}
    panel_wrapper = Node("AXGroup", [Node("AXGroup", [scroll_layout])], **panel_attrs)
    chat_panel = panel_wrapper if legacy_chat_panel_id else scroll_layout
    main = Node(
        "AXGroup",
        [Node("AXGroup", [Node("AXGroup", [decoy_text_area]), panel_wrapper])],
        AXSubrole="AXLandmarkMain",
        AXDOMIdentifier="_r_9_",
    )

    shell_children: list[Node] = [labelled("AXLink", "Skip to content")]
    if with_sidebar:
        shell_children.append(Node("AXGroup", [sidebar], AXSubrole="AXLandmarkComplementary", AXDOMClassList=["app-shell-left-panel"]))
    shell_children += [banner, main]
    root = Node("AXGroup", [Node("AXGroup", shell_children)], AXDOMIdentifier="root")
    top_group = Node("AXGroup", [root, *(overlays or []), live_region("DndLiveRegion-1"), live_region()])
    web_area = Node("AXWebArea", [top_group] if attached else [], AXTitle=current_chat, AXLoaded=True)
    window = Node(
        "AXWindow",
        [Node("AXGroup", [Node("AXGroup", [Node("AXScrollArea", [web_area])])], AXTitle="ChatGPT")],
        AXSubrole="AXStandardWindow",
        AXTitle=current_chat,  # live 2026-11-20: the window title is the conversation title
    )
    app = Node("AXApplication", [window], AXTitle="ChatGPT", AXFocusedWindow=window, AXWindows=[window], AXEnhancedUserInterface=False)
    return AppTree(
        app=app,
        window=window,
        web_area=web_area,
        top_group=top_group,
        root=root,
        sidebar=sidebar if with_sidebar else None,
        banner=banner,
        main=main,
        chat_panel=chat_panel,
        turn_list=turn_list,
        composer=composers[0] if composers else None,
        send=send,
        sidebar_model=model,
        extra={"composers": composers, "decoy_text_area": decoy_text_area},
    )


def build_app(**kwargs):
    """Compatibility shape used by the preflight tests: (app, web_area, top_group, composer)."""

    tree = build_app_tree(**kwargs)
    return tree.app, tree.web_area, tree.top_group, tree.composer
