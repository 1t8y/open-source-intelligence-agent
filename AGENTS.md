# Project Rules

## Local User Manual

The project root must contain a local file named `使用手册.md`. This file is intentionally ignored by Git and is not uploaded to GitHub.

After completing every project stage, update `使用手册.md` before declaring the stage complete. Each stage section must include at least:

1. The purpose of every new or materially changed file, including web pages, logs, databases, configuration, scripts, and tests.
2. Structural relationships between files and modules, including parent/child classes, callers, triggers, queues, and data flow.
3. Concrete operation instructions explaining which files or commands start each module and how to verify it.
4. Current validation status, known limitations, and the next stage entry point.

Keep the manual current rather than replacing historical stage knowledge unless an older instruction is no longer valid.

## Document Layers

Three root documents are the fact baseline: REQUIREMENTS.md (what is built and how it is accepted), INTERFACES.md (contracts), ARCHITECTURE.md (design decisions and invariants). docs/*.md expands the baseline; docs/adr/ keeps one decision per file. None of them carries development progress, task checklists or changelogs.

Development progress, stage verification records and task logs stay in local, gitignored files: 使用手册.md (five sections per stage) and 开发进度流水.md (archived task log). Commit-level history belongs to git log. Never add a Changelog or task checklist back into the tracked baseline documents.
