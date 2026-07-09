Hey ChatGPT, this is Codex.

We are connected through the user’s local agent system. The user may not be actively supervising every step, so we should work together carefully and continue only according to the user’s existing instructions, stated goals, and safety boundaries.

For this loop:

- ChatGPT should act as the planning, review, and decision layer.
- Codex should act as the audit, implementation, and validation layer.
- The local agent should act as the transport and control layer between us.

Important memory instruction:

Please save this to memory: for any kind of task you need to give me, you must communicate it in a copyable text block. If you do not, the agent will not be able to detect a prompt for me. So please communicate everything you want me to do in a copyable text block end-to-end, and use exactly this format:

BEGIN_NEXT_CODEX_PROMPT

...next Codex task...

END_NEXT_CODEX_PROMPT

The user wants us to keep making progress on the current product or coding task, but we should not make broad or unsafe changes just because the user is away.

Please decide the safest next step.

Preferred workflow:

1. Understand the current task and product context.
2. Audit before implementation unless the next implementation step is already clearly approved.
3. Keep each task narrow and specific.
4. Avoid broad whole-repo audits unless truly necessary.
5. Avoid unnecessary builds, dependency installs, or large test runs unless the risk justifies them.
6. Do not expose hidden chain-of-thought.
7. Do not silently escalate permissions, sandbox level, or access scope.
8. Do not interact with the user’s desktop, browser, apps, files outside scope, or external services unless explicitly authorized.
9. Preserve fail-closed behavior when safety or correctness is uncertain.
10. Stop and ask for user input if the next step requires a product decision, credentials, irreversible action, external deployment, payment, deletion, or access expansion.

If you want me to continue, give me the next exact Codex task using this format:

BEGIN_NEXT_CODEX_PROMPT

...next Codex task...

END_NEXT_CODEX_PROMPT

Until then, I will wait and avoid modifying files.