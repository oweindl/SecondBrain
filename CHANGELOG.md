# Changelog

## 0.5.1

- Replaced the once-per-session GitHub fetch limit with a persisted source-keyed minimum 60-minute interval across sessions and brain/root switches.
- Defined UTC eligibility, pre-fetch concurrent reservation, failure/crash throttling, clock-skew handling and explicit persistence limitations.
- Defined dated cached results/candidate bytes, next-eligible-time reporting and approval-based installation without redundant discovery fetches.
- Manual checks bypass prompt opt-out/suppression but not the network interval; HELP and bare navigation remain non-checking.
- Preserved one automatic prompt per session and all existing update/rollback safeguards. Checks occur on invocation, not via background polling.

## 0.5.0

- Added non-mutating HELP with examples and available root/active-brain state; HELP never starts setup or update checks.
- Intentionally changed bare invocation to explicit Continue/Switch/Create navigation, with setup/help fallbacks and no automatic activation.
- Added guided creation for missing inputs, optional purpose, location preview and one assembled-plan confirmation.
- Made SHOW strictly preview-only and ACTIVATE explicitly switching; retained shorthand activation.
- Added actionable Healthy/Needs attention/Blocked STATUS output and approved repair review.
- Reduced redundant confirmations for safe, explicit private operations while retaining consequential-change safeguards.
- Limited automatic update checks/prompts per session, made Skip session-scoped, and retained manual-check override and persistent opt-out.
- Standardized concise outcomes and operation-relevant warnings; expanded adapter acceptance scenarios. No executable runtime adapter added.

## 0.4.0

- Defined per-user runtime-local root/active-brain state, validation and session-only fallback without exporting machine paths.
- Added guarded-write, readback and partial-failure protocols; brain files are authoritative and index updates happen last.
- Extended read-only STATUS with previewed, approved repairs for incomplete setup and stale/malformed registries.
- Hardened proposed folder naming, lookup ambiguity, Markdown display-name escaping and canonical path/link containment while preserving valid legacy folders.
- Defined package validation, change preview, verified backups, package-only replacement and conflict-aware rollback, preserving update preferences.
- Retained optional MD-Browser compatibility and existing command/startup-reminder behavior.
- Documented adapter acceptance scenarios and explicit limits: instruction rules do not themselves provide filesystem or updater guarantees. No executable helper or mandatory dependency added.

## 0.3.0

- Generalized the repository and package wording to be agent-agnostic.
- Removed runtime-specific installation and permission wording from the core spec.
- Replaced product-specific terminology with generic agent-runtime language.
- Kept the same Markdown storage contract and update-check behavior.

## 0.2.1

- Bumped local development version.

## 0.2.0

- Added semantic skill version metadata.
- Added GitHub update-check behavior.
- Added manual update-check command support.
- Added persisted preference for disabling automatic update prompts.

## 0.1.0

- Initial generic Markdown SecondBrain creation and context switching.
