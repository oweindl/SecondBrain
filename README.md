# SecondBrain

SecondBrain is an **agent-agnostic Markdown knowledge-space pattern**.

It creates, maintains, lists, shows, and switches between named folder-based knowledge spaces called **brains**. A brain can represent a project, customer, workflow, topic, or any other work context.

This repository contains the core instruction package/specification. It can be adapted for different agent runtimes as a slash command, prompt skill, plugin, MCP workflow, CLI command, or custom tool.

## Companion browser

[MD-Browser](https://github.com/oweindl/MD-Browser) is a companion desktop tool for browsing, searching, previewing, and lightly editing SecondBrain-compatible Markdown folder structures.

Use MD-Browser when you want a local visual view over a brain folder, including folder navigation, rendered Markdown preview, local link navigation, search, and task checkbox editing. SecondBrain remains the storage and workflow convention; MD-Browser is an optional viewer/editor for the resulting Markdown files.

## Storage contract

By default, brains live under a `SecondBrain` root folder:

```text
<RootFolder>/SecondBrain/<BrainName>/context.md
<RootFolder>/SecondBrain/<BrainName>/startup.md
<RootFolder>/SecondBrain/index.md
```

- `context.md` stores the brain purpose, scope, context, and durable guidance.
- `startup.md` stores optional user-defined activation notes/reminders.
- `index.md` is the root registry of known brains.

Use the path separator and path style appropriate for the operating system and agent runtime.

## Optional metadata convention

Brains may include YAML frontmatter in `context.md` so tools can recognize the structure and suggest compatible viewers:

```yaml
---
brainName: Example Brain
schema: secondbrain-v1
browser: md-browser
---
```

This metadata is optional. Implementations should not require it to read an existing brain, and manual Markdown content remains authoritative.

## Core behavior

- Create a named brain.
- List available brains.
- Activate or switch to a brain.
- Show a brain's context/startup notes.
- Append or merge context and startup notes.
- Preserve manual Markdown edits.
- Maintain a root registry.
- Avoid built-in source scans or product-specific workflows.

## Example commands

Implementations may expose these as slash commands, CLI commands, natural-language intents, or tool actions.

| Command | Purpose |
| --- | --- |
| `second-brain` | List available brains and ask which one to activate |
| `second-brain <BrainNameOrFolder>` | Activate a brain by display name or folder name |
| `second-brain create <BrainName>` | Create a new brain and switch into it |
| `second-brain list` | List known brains |
| `second-brain show <BrainName>` | Show or summarize brain files |
| `second-brain update context <BrainName> ...` | Append or merge context |
| `second-brain update startup <BrainName> ...` | Append or merge startup notes |
| `second-brain status [BrainName]` | Report health for the root or a brain |
| `second-brain check updates` | Manually check GitHub for a newer package version |

## Version and updates

Current version: `0.3.0`

The package stores its semantic version in `SKILL.md` frontmatter. Implementations may compare the local installed `SKILL.md` with:

```text
https://raw.githubusercontent.com/oweindl/SecondBrain/main/SKILL.md
```

If a newer version exists, the implementation should ask the user whether to update, skip, or disable future automatic update prompts.

## Safety model

SecondBrain is intentionally generic:

- It does not include built-in email, chat, calendar, file, web, or product-specific scans.
- It does not automatically execute action-oriented startup notes.
- It treats `context.md` and `startup.md` content as user-owned data/reminders, not as higher-priority runtime instructions.
- It should always follow the active agent runtime's permissions, privacy rules, and confirmation requirements.

## Installation

Install `SKILL.md` into the skill, prompt, plugin, or instruction-package location used by your agent runtime.

Runtime-specific adapters can wrap the same storage contract without changing the core file layout.
