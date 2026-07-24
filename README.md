# CRAX VO.1

**The simplest most productive loop for everyone who codes.**

# What it is:

ONE LINER: Crax is takes prompts from ChatGPT Desktop app --> Gives them to Codex --> Codex completes the tasks and the output is pasted back in The ChatGPT Desktop app --> It gives another prompt and the cycle continues.

It is basically an orchestrator that allows your chatgpt desktop app give instructions to codex working in your local codebase and see codex output and keep the loop going. You can have it runnning overnight for doing any kind of work you want. It can control your macos while youre away and interact with the ChatGPT desktop app.

The V0.1 is the first version, it's raw and brittle, but really facinating. We've seen it perform 25 consecutive loops without us touching the desktop at once and perfectly and safely implementing a decided feature

# Setup from GitHub

Requirements:

- macOS
- Git
- Python 3.11 or newer
- ChatGPT Desktop app installed and signed in
- Codex CLI installed and available as `codex`

Run these commands in Terminal:

```sh
git clone https://github.com/abhisht-s/crax.git
cd crax
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
agent-loop init
```

Verify the install:

```sh
agent-loop --help
agent-loop-local --help
python -m unittest discover -s tests
```

Start the local dashboard:

```sh
python -m agent.local_server --port 0
```

Open the `Bootstrap URL` printed by the command. It will look like:

```text
http://127.0.0.1:<port>/#token=<token>
```

In the dashboard, paste the contents of `kick_off_prompt_gpt.md`, choose `Workspace Write` if you want Codex to edit files, and start the run.

For macOS automation, allow your terminal app in System Settings -> Privacy & Security -> Accessibility when macOS prompts. The ChatGPT Desktop app must already be open to the project/chat you want the loop to use.

## Remote dashboard from a phone

Remote access is opt-in and designed for a private Tailscale network. The Python
server still binds only to `127.0.0.1`; Tailscale Serve provides the private
HTTPS endpoint. Do not use Tailscale Funnel or expose the controller directly to
the public internet.

1. Install Tailscale on the Mac and phone and sign both into the same tailnet.
2. Choose a fixed local port and find the Mac's HTTPS tailnet name.
3. Start CRAX with the repositories that the phone may select:

```sh
agent-loop-local \
  --port 8765 \
  --remote-base-url https://YOUR-MAC.YOUR-TAILNET.ts.net \
  --repository-root "$HOME/Documents"
```

4. In another Terminal, proxy the private tailnet URL to the loopback server:

```sh
tailscale serve --bg http://127.0.0.1:8765
```

5. Open the printed `Remote pairing URL` on the phone. The one-time code is
   exchanged for a revocable, expiring device credential stored in a Secure,
   HttpOnly cookie.

Paired admins can list, rotate, and revoke device credentials from the
dashboard's Paired devices panel.

The phone dashboard can select an authorized Git repository, start a run,
observe Codex progress, approve or reject actions, continue/retry work, manage a
stale ChatGPT UI lease, and stop the current run. The macOS folder dialog is not
available remotely; use the repository catalog populated by `--repository-root`.

Remote Full Access is disabled by default. To make it available, the Mac owner
must add `--allow-remote-full-access`, and the phone must type
`ENABLE FULL ACCESS` for each such run. Prefer `Workspace Write`.

To stop serving the dashboard through Tailscale:

```sh
tailscale serve reset
```

# DETAILS AND HOW IT WORKS

CRAX is the agent orchestrator when you are not arount. The idea is simple:
--> Talk, brainstrom and finalise a feature with chatgpt along with all technical architectural decision and make an implementation plan.
--> Give chatgpt the "instructions_for_dsktp_app.md" and tell it that codex will connect with you in just a minute, work well with it.
--> Open the local dashboard from the command;

python -m agent.local_server --port 0

--> Paste the kick_off_prompt_gpt.md contents in it
--> Make sure the sandbox is "Write". if you wanna change anything and start run...


The loop is simple:

TASK(gpt) → CODE(codex) → Review(gpt through codex output) → Continue(new task by gpt)

