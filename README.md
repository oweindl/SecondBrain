# secondBrain

`secondBrain` contains the `/second-brain` Microsoft Scout skill.

The skill creates, maintains, lists, shows, and switches between named folder-based Markdown knowledge spaces called **SecondBrains**. It is intentionally generic: it scaffolds and activates knowledge contexts, but does not include built-in email, Teams, calendar, web, or file scan workflows.

## What it creates

By default, brains live under a local `SecondBrain` folder:

```text
<RootFolder>\SecondBrain\<BrainName>\context.md
<RootFolder>\SecondBrain\<BrainName>\startup.md
<RootFolder>\SecondBrain\index.md
```

- `context.md` stores the brain purpose, scope, context, and durable guidance.
- `startup.md` stores optional user-defined activation notes/reminders.
- `index.md` is the root registry of known brains.

## Commands

- `/second-brain` — list available brains and ask which one to activate.
- `/second-brain <BrainNameOrFolder>` — activate a brain by display name or folder name.
- `/second-brain create <BrainName>` — create a new brain and switch into it.
- `/second-brain list` — list known brains.
- `/second-brain show <BrainName>` — show or summarize brain files.
- `/second-brain update context <BrainName> ...` — append or merge context.
- `/second-brain update startup <BrainName> ...` — append or merge startup notes.
- `/second-brain status [BrainName]` — report health for the root or a brain.

## Install

Copy `SKILL.md` into a Microsoft Scout local skill folder named `second-brain`, for example:

```text
C:\Users\<you>\.scout\m-skills\second-brain\SKILL.md
```

Then enable or reload custom skills in Microsoft Scout.

## Version

Initial version: generic Markdown SecondBrain creation and context switching.
