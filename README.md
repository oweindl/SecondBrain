# SecondBrain

SecondBrain is an **agent-agnostic Markdown knowledge-space pattern**.

It creates, maintains, lists, shows, and switches between named folder-based knowledge spaces called **brains**. A brain can represent a project, customer, workflow, topic, or any other work context.

This repository contains the core instruction package/specification and an optional Python reliability adapter. It can be adapted for different agent runtimes as a slash command, prompt skill, plugin, MCP workflow, CLI command, or custom tool.

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
- `index.md` is a repairable registry of known brains; existing brain files are authoritative.

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
| `second-brain` | Show active brain first; offer Continue, Switch or Create as applicable |
| `second-brain help` | Show commands and available selection state without setup or updates |
| `second-brain <BrainNameOrFolder>` | Activate a brain by display name or folder name |
| `second-brain create <BrainName>` | Create a new brain and switch into it |
| `second-brain list` | List known brains |
| `second-brain show <BrainName>` | Show or summarize brain files |
| `second-brain activate <BrainName>` | Switch to the named brain |
| `second-brain update context <BrainName> ...` | Append or merge context |
| `second-brain update startup <BrainName> ...` | Append or merge startup notes |
| `second-brain status [BrainName]` | Report health for the root or a brain |
| `second-brain check updates` | Manually check GitHub for a newer package version |

## Version and updates

Current version: `0.6.0`

The package stores its semantic version in `SKILL.md` frontmatter. Implementations may compare the local installed `SKILL.md` with:

```text
https://raw.githubusercontent.com/oweindl/SecondBrain/main/SKILL.md
```

If a newer version exists, the implementation should ask the user whether to update, skip, or disable future automatic update prompts.

HELP and bare navigation never trigger update checks. Other eligible operations check GitHub on use only when **at least 60 minutes** have elapsed since the previous attempt for the same source. This replaces the once-per-session fetch limit, allowing another due check during a long session without background polling.

Persist `lastAttemptAtUtc` and dated outcomes in runtime-local per-user configuration outside the package and brain root; session restarts, brain switches and package upgrades do not reset the interval. Reserve an attempt before fetching so failed checks/crashes also consume the interval and concurrent sessions cannot both fetch. Missing persistence or reliable reservation support blocks network checks rather than silently abandoning the interval.

Offer at most one automatic update prompt per session. Skip suppresses prompts for that session only. Manual CHECK UPDATES bypasses prompt suppression and the persistent opt-out, **not the network cooldown**: during the interval it shows the dated cached result and next eligible time. Installation still requires approval and revalidation of cached package bytes. No timer, scheduled job or automatic installation is created.

Before approval, show meaningful changes and any local customizations that replacement would remove. Validate package structure, preserve a verified prior package, replace only package-owned files, and verify both installed and runtime-loaded instructions where available. On failure, attempt conflict-aware rollback and report its outcome explicitly. Name/version validation alone does not prove authenticity.

The portable package owns the files listed in `package-manifest.json`, plus the manifest itself: `SKILL.md`, `scripts/secondbrain.py` and `adapters/README.md`. Per-file SHA256 values and version consistency detect corruption, not publisher authenticity. Preserve `.secondbrain-settings.md` preferences, brain data, runtime-local state and custom adapters. Tests and root repository documentation are not installed.

## Optional reliability adapter

Requires Python 3.10+ and its standard library only. Nothing runs on import; no scheduler, dependency installation or direct Scout registry edit is included. Explicitly opt into helper-backed operation and choose a private local runtime directory outside your package/install/brain directories. Instruction-only use remains available with clearly weaker guarantees.

Use the script path from your reviewed package. These examples use Windows paths; replace placeholders rather than run them literally:

```powershell
python .\scripts\secondbrain.py capabilities
python .\scripts\secondbrain.py validate-package --package-dir .
python .\scripts\secondbrain.py check-updates --runtime-dir "<private-runtime-dir>" --brain-root "<brain-root>" --now "<authoritative-ISO8601-with-offset>" --installed-version 0.6.0
python .\scripts\secondbrain.py guarded-write --runtime-dir "<private-runtime-dir>" --root "<brain-root>" --target "context.md" --expected-sha256 "<original-SHA256-or-absent>" --content-file "<private-merged-content-file>" --approved
```

Updates reserve timestamps under an OS lock, survive restarts, retain failed/pending attempts, enforce the 60-minute boundary and verify cached candidate digests. Guarded writes reject detected concurrent changes, escaping links and unsafe names; they require existing parent directories and preserve supported protection metadata. Known nonredirecting Windows CLOUD placeholders are verified rather than treated as junctions.

All operational commands emit JSON. A nonzero exit with `awaiting_registration` is intentionally incomplete, not successful. Package installation requires a reviewed full manifest package; checking SKILL version metadata does not download executable helper updates.

See [the supported host bridge](adapters/README.md) for preparation, registration, verification and approved journal recovery. The host still manages root/active-brain selection and prompt preferences; the helper is not a replacement for those workflows.

## Automated disposable-fixture tests

```powershell
python -m unittest discover -s tests -v
```

The suite executes cooldown, cache, locking, containment, guarded-write, package and host-registration/recovery logic using temporary fixtures and fake network responses. It does not use personal brains, M365 or live GitHub traffic. Some OS permission-dependent symlink tests may skip; platform guarantees must still be assessed on the actual target runtime.

## Persistence and recovery

Adapters use per-user persistent configuration, or a private `secondbrain-state.json` in their configuration directory, outside the repository/package, brain root and exports. Disclose the selected location. Store schema version 1, the absolute selected `rootPath`, and `activeBrain` as a single root-relative folder name or JSON null. Validate before reuse; never infer selection from an OS username or silently recreate moved/missing roots.

Activation is saved only after reading the selected brain's available files successfully. Without durable configuration support, selections last only for the current session; disclose that limitation and ask again after restart. Bare invocation offers explicit navigation rather than silently activating, LIST never switches, and STATUS without a brain remains a root health report.

Write and verify brain files first, index last, then activation state. Detect concurrent edits with conditional writes/revisions or appropriate locking where supported. Atomic file replacement alone is neither conflict protection nor a multi-file transaction. When guarantees are unavailable, disclose the weaker mode and require approval for unguarded overwrites or remain read-only.

On interrupted operations, report successful/failed steps. STATUS detects incomplete setup, unregistered folders, stale/malformed index entries and invalid active references. It does not repair automatically: preview changes and obtain approval, preserving manual notes and never deleting user data to conceal a failed operation.

## Guided navigation and setup

Version 0.5.0 intentionally changes bare invocation from an immediate brain-selection prompt to a navigation screen. With a valid active brain, show it first and offer **Continue**, **Switch**, or **Create**. Continue reads the active brain without asking for its name again; Switch asks for another brain; Create starts guided setup. Without an active brain, offer selection/creation as applicable; without a valid root, offer setup/correction or HELP. Nothing is activated or created before the user's choice.

Guided creation asks only for missing root/name, offers an optional purpose with a skip path, previews the final location and requests one confirmation of the assembled plan. Remember answers across turns. Fully specified, safe private commands proceed without repetitive approval, while root switches, ambiguous naming, repairs, package updates and weak overwrite protection still require confirmation.

SHOW is preview-only, even for a different brain: it does not switch activation or execute startup notes. ACTIVATE and the existing shorthand switch context.

STATUS leads with **Healthy**, **Needs attention**, or **Blocked**, then explains the affected location, issue and remedy. Offer review of a repair only when needed; approval of exact changes remains mandatory. Responses lead with the outcome and affected brain/path, with limitations surfaced only when relevant.

## Naming and containment

Normalize proposed names using the host filesystem rules; show the actual folder name and ask before lossy/ambiguous changes. Handle Windows device names, trailing dots/spaces, empty names, limits and case-insensitive collisions. Keep original display names separate and safely escape table cells. Reuse valid legacy folders without renaming.

Reject traversal and rooted child paths. Validate resolved root/child boundaries and symlink/junction targets, including brain files themselves. If link resolution cannot be assured, refuse writes to linked/uncertain paths rather than claim containment from a string-prefix check.

## Acceptance scenarios for runtime adapters

These are host/adapter acceptance scenarios. The executable suite covers helper logic with disposable fixtures; it does not prove all host UI/provider behaviors or eliminate the listed platform limits.

| Scenario | Expected result |
| --- | --- |
| First use without root | Ask for root; only explicit creation/approval creates it. |
| Restart with valid state | Reuse the final root without appending `SecondBrain` again; resolve eligible omitted brain arguments from the active folder. |
| No persistence support | Disclose session-only state; ask again on restart. |
| Missing/moved root or active brain | Report invalid selection and ask; no recreation, relocation or silent switch. |
| Corrupt/unknown state schema | Preserve it and request correction; do not silently reset. |
| `CON.txt`, `nul`, `COM1`, `LPT1` on Windows | Reject reserved basenames and ask before substituting a safe name. |
| Trailing dots/spaces, empty result, long name | Show normalization, revalidate, ask for correction/approval; no unsafe creation. |
| Case/normalization collision or conflicting name matches | Ask which brain/alternative to use; never select first or merge implicitly. |
| `..`, absolute child, sibling-prefix trick or escaping link | Reject containment violation; no out-of-root access through registry entries. |
| Display name with pipes/newlines | Preserve display name safely without injecting index rows. |
| Read-only folder or failed write/readback | Explicit failure; preserve prior content and report partial progress. |
| Concurrent edit or file appears after absent-file read | Conflict stops write; reload/reconcile, preserve edits; no forced replacement. |
| Runtime lacks atomic/conditional writes | Disclose limits; preview/approval for weaker overwrite or stay read-only. |
| Setup interrupted before index update | Discover unregistered brain; STATUS offers an approved minimal repair without recreating files. |
| Missing brain file or malformed index with manual notes | Read available files, report incomplete/ambiguous state; repair only after preview/approval. |
| Invalid/older/unreachable update | Reject or skip replacement; failed checks do not claim currency. |
| Update replaced incorrectly or runtime loads different text | Report failure; verified backup restores package/registration unless concurrent changes prevent rollback. |
| Backup/rollback failure | Stop update or uncertain-package use, retain recovery material and report needed repair. |
| LIST and SHOW | Read-only discovery/preview; showing another brain does not switch active selection or run startup notes. |
| ACTIVATE with startup action reminders | Read/persist selection, display reminders; never execute them without an explicit request. |
| HELP with no root, corrupt state or opt-out settings | Display usage and available/unknown state; no setup question, update check or write. |
| Bare invocation with valid active brain | Show active brain first; wait for Continue/Switch/Create. Continue uses that selection with no redundant name question. |
| Bare invocation without active brain/root | Offer selection/create or setup/help as applicable; never silently activate, create or reset. |
| Guided CREATE with missing inputs | Ask each missing input once, permit skipping purpose, preview location and confirm one assembled plan. |
| Fully specified safe private command | Proceed without redundant confirmation; consequential-change and write-safety rules remain enforced. |
| STATUS healthy, incomplete or inaccessible | Use Healthy/Needs attention/Blocked appropriately; offer repair review only when meaningful, never automatic writes. |
| Repeated automatic update opportunity after Skip | No second prompt that session and no persistent opt-out change; next session can prompt again. |
| Explicit CHECK UPDATES after Skip/opt-out | Use dated cache during cooldown, otherwise fetch when due; validate candidate, require approval and do not re-enable automatic prompts. |
| First check with no source record | Safely initialize/reserve runtime-local check state before fetching; no root/brain data changed. |
| Another invocation at 59 minutes, including after restart | No network fetch; manual check shows dated cache/remaining wait, not a fresh result. |
| Invocation at exactly 60 minutes or later in a long session | One reserved fetch is eligible; no invocation means no background check. |
| Failed/invalid request or crash after reservation | Attempt consumes the hour; retain last successful result separately and do not retry immediately. |
| Concurrent sessions checking the same source | Conditional reservation/lock allows only one fetch in the interval. |
| Brain/root switch or package upgrade/rollback | Preserve the source-keyed attempt timestamp; no cooldown reset. |
| Missing reservation support, read-only or malformed check state | Report inability to enforce the cooldown; no unthrottled fetch or silent reset. |
| Future timestamp/clock rollback | Report clock/state issue, do not fetch until due or explicitly repaired. |
| Approved cached candidate during cooldown | Revalidate its bytes/digest locally; no redundant discovery fetch and no installation without approval. |
| Helper absent or not approved | Continue in disclosed instruction-only/limited mode; no automatic script download or execution. |
| Manifest mismatch, unsafe ownership or newer package extras | Block invalid owned files; copy only reviewed ownership and preserve local-only content. |
| File installation without actual host readback | Remain awaiting_registration, never completed from disk bytes alone. |
| Host regenerates matching SKILL wrapper | Restore canonical metadata only with approval and matching instruction body; stale restored text blocks completion. |
| Recovery preview followed by external edits | Preview is non-mutating; approved rollback refuses to overwrite changed operation-owned files. |

## Runtime limitations

The optional helper enforces local cooperating locks, durable update-check state, guarded revisions, staged replacement, manifest validation and journaled installation/recovery. It does not implement remote provider conditional writes, arbitrary protection labels, active-brain persistence or registration APIs by itself. Host registration requires supported tools and actual exported readback.

Hash checks do not eliminate the final check-to-replace race with noncooperating editors. Cloud sync does not guarantee atomicity across devices, and installation is not a multi-file transaction. Windows preserves DACL/mode but not owner/SACL/named streams and refuses encrypted targets; POSIX ownership/mode restoration may fail for insufficient permissions. Runtime state must live in an explicitly chosen private directory. No SHA256 manifest or HTTPS fetch alone proves publisher authenticity.

No helper, database or background service is required for instruction-only use. A runtime must disclose unsupported guarantees rather than silently approximate them.

## Safety model

SecondBrain is intentionally generic:

- It does not include built-in email, chat, calendar, file, web, or product-specific scans.
- It does not automatically execute action-oriented startup notes.
- It treats `context.md` and `startup.md` content as user-owned data/reminders, not as higher-priority runtime instructions.
- It should always follow the active agent runtime's permissions, privacy rules, and confirmation requirements.

## Installation

For instruction-only use, install `SKILL.md` through your runtime's supported skill-management mechanism. For helper-backed use, review and explicitly approve the manifest-listed files and follow the host bridge; do not copy a whole repository over an existing installation.

Runtime-specific adapters can wrap the same storage contract without changing the core file layout.
