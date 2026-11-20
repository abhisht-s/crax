"""Accessible names used to automate the new ChatGPT desktop app (``com.openai.codex``).

Every label the new-app code matches lives here, so an app update or a locale
change is a one-file review. Values are the English UI strings observed on
2026-11-18 (en_GB). ``i18n_id`` is the app's message id from
``docs/new-chatgpt-app/tools/handoff_label_catalog.tsv`` (extracted from the app's
``app.asar``), or ``None`` when the string was observed in the AX tree but its id
was not recorded.

Only English is supported for now. Code that cannot find a needed label must
fail closed with a reason code rather than guess.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Label:
    text: str
    i18n_id: str | None = None

    def prefix(self) -> str:
        """For templates such as ``"Project: {project}"``: the text before the first ``{``."""

        return self.text.split("{", 1)[0]

    def parse(self, value: str) -> str | None:
        """Return the single template argument from ``value``, or None if it doesn't match.

        Supports templates with exactly one placeholder at the end.
        """

        prefix = self.prefix()
        if "{" not in self.text or not value.startswith(prefix):
            return None
        argument = value[len(prefix):].strip()
        return argument or None


# --- shell -------------------------------------------------------------------------
MODE_TRIGGER = Label("Switch mode, current mode: {mode}", "sidebarElectron.productMode.trigger")
PRODUCT_MODE_CHATGPT = "ChatGPT"
NEW_CHAT = Label("New chat", "chatgptConversations.newChat")
BANNER_PROJECT = Label("Project: {project}")
PROJECT_CHATS_LIST = Label("Chats in {project}")
SHOW_MORE = Label("Show more")

# --- composer ----------------------------------------------------------------------
COMPOSER = Label("Ask ChatGPT", "chatgptConversations.composer.placeholder")
SEND = Label("Send", "chatgptConversations.composer.send.ariaLabel")
STOP = Label("Stop", "chatgptConversations.composer.stop.ariaLabel")
ADD_FILES_AND_MORE = Label("Add files and more", "composer.contextButton.ariaLabel")
MODEL_PICKER = Label("Select ChatGPT model", "chatgptConversations.modelPicker.ariaLabel")
DICTATE = Label("Dictate", "composer.dictation.aria")

# --- thread ------------------------------------------------------------------------
USER_TURN_HEADING = Label("You said:", "codex.conversation.roleHeading.user")
ASSISTANT_TURN_HEADING = Label("ChatGPT said:", "codex.conversation.roleHeading.assistant")
TURN_HEADINGS = frozenset({USER_TURN_HEADING.text, ASSISTANT_TURN_HEADING.text})
SCROLL_TO_BOTTOM = Label("Scroll to bottom")
LATEST_RESPONSE = Label("Latest response", "codex.conversation.latestResponse")

# --- reply footer --------------------------------------------------------------------
COPY_RESPONSE = Label("Copy")
COPY_MESSAGE = Label("Copy message")
READ_ALOUD = Label("Read aloud", "chatgptConversations.composer.readAloud.title")
REGENERATE = Label("Regenerate response", "chatgptConversations.regenerate.label")
RATE_RESPONSE = Label("Rate response")

# --- status ------------------------------------------------------------------------
RESPONDING = Label("ChatGPT is responding", "chatgptConversations.loadingMessage")
RESPONSE_COMPLETE = Label("Response complete", "chatgptConversations.responseComplete")
STOP_DIALOG_TITLE = Label("Stop this response?", "chatgptConversations.keyboardStop.title")
