# Multi-session CRAX

CRAX already does one thing extremely well: it keeps a single ChatGPT
conversation in a loop with Codex on a local repo, overnight, without you
sitting at the desk.

This feature is about not being stuck at one.

## The idea

A CRAX session is one ChatGPT chat, paired with one repository, running the
loop you already know:

ChatGPT decides the next task → Codex does the work → CRAX pastes the result
back into that same chat → ChatGPT reviews and issues the next task.

Today you can run exactly one of those sessions at a time. If you are already
looping on `craxii` / `Dev Internal App`, you cannot also loop on
`PTG Assistant` / `Moderation Backend` until the first session is finished or
stopped.

After this feature, you can.

You start session one. It keeps going. You start session two. It keeps going
too. Each chat stays in its own conversation with Codex. Starting the second
session does not kill the first. Stopping one does not take the other down.

## Why it matters

The current loop is already useful enough to leave running while you sleep.
The limit is not the idea. The limit is that life is not one project.

You might be implementing an internal app in one ChatGPT chat and a backend
in another. You might have two chats inside the same ChatGPT project, each
owning a different workstream. You might even point both sessions at the same
repository because both chats are about the same codebase.

Right now that means choosing. This feature means not choosing.

## What a session is

A session is not “CRAX in general.” It is one live loop with three bindings:

1. **A ChatGPT chat** — the conversation Codex reports to, and the
   conversation that issues the next prompt.
2. **A ChatGPT project** — the project that chat lives in. Two sessions may
   share a project.
3. **A repository** — the local codebase Codex works in. Two sessions may
   share a repository.

The chat is the identity of the session. That is the conversation CRAX must
never mix with another loop.

## What you should be able to do

While one loop is already running, start another.

Typical cases we want to support:

- Two different ChatGPT projects, two different chats, two different repos.
- Two different chats in the **same** ChatGPT project, each with its own
  repo — or both on the **same** repo.
- Two chats about the same codebase, running at the same time, because you
  decided both workstreams belong in that repo.

In every case you can watch each session, approve or reject when a session
asks for it, and stop one session without touching the other.

The loop you already use, with a single chat, must keep working the way it
works today. Multi-session is additive. It is not a new product that replaces
the one you rely on.

## The one hard rule

**Two sessions cannot share a ChatGPT chat.**

If `Dev Internal App` is already in a live loop, you cannot start a second
loop into `Dev Internal App`. That conversation is taken. CRAX would be
pasting two Codex lives into one thread, capturing the wrong reply, and
asking the wrong ChatGPT to direct the wrong work.

**The ChatGPT project may be the same.** A project is a folder of chats, not
a session. Two chats under `craxii` can both be looping.

**The repository may be the same.** If you want two chats driving Codex in
one codebase, that is allowed. You are choosing to let two loops edit the
same tree. CRAX should not refuse that. It should still keep the chats
strictly separate.

## What it should feel like

You are not managing infrastructure. You are running work.

You open CRAX, fill in a project, a chat, and a repo, and start. That is
session one, same as today.

Later — or immediately — you start another session with a different chat.
You can see that both are alive: which chat, which project, which repo, and
whether each one is with Codex, waiting on ChatGPT, or waiting on you.

When ChatGPT is talking in one session, the other session is not erased. It
waits its turn at the ChatGPT window and keeps its own Codex work moving.
You should not have to babysit the switch. You should not have to close one
loop to feed the other.

If something needs your judgment, it is obvious which session is asking.
If you hit emergency stop, it is obvious which session you are stopping.

## What this is not

This is not two ChatGPT apps. There is still one ChatGPT Desktop on the Mac.
Sessions take turns speaking to it. From your point of view they still feel
like two ongoing collaborations, each in its own chat.

This is not one ChatGPT chat supervising two repos, or two chats collapsed
into one thread. Mixing conversations is the failure mode we are avoiding.

This is not a requirement to use two repositories. Same repo is a valid
choice, not a workaround.

This is not a rewrite of CRAX. The single-session loop stays the thing you
can run tonight. Multi-session is the ability to run another one beside it
without tearing the first down.

## Success

The feature is done when this is true in real use, not as a demo:

You start a loop on one ChatGPT chat and one repo. While it is still
running, you start a second loop on a different chat — same project or
not, same repo or not. Both keep making progress. Each Codex report lands
in the chat it belongs to. Each next prompt comes from that chat only.
You can stop either one. The loop you already depend on, run by itself,
still behaves as it does now.

Until that is true, the daily driver stays the current single session.
This feature has to become something we actually use, not something we
only describe.
