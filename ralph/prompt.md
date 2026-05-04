# ISSUES

Local issue files from `.scratch/kursbuchungs-bot/issues/` are provided at start of context. Parse them to understand the open issues.

You will work on the AFK issues only, not the HITL ones.

Only pick issues whose blockers are already in `.scratch/kursbuchungs-bot/issues/done/`.

Do not work on HITL issues. Do not implement around a blocker. If an issue is blocked, add a short comment explaining the blocker and pick another unblocked AFK issue.

You've also been passed a file containing the last few commits. Review these to understand what work has been done.

If all AFK tasks are complete, output <promise>NO MORE TASKS</promise>.

# TASK SELECTION

Pick the next task. Prioritize tasks in this order:

1. Critical bugfixes
2. Development infrastructure

Getting development infrastructure like tests and types and dev scripts ready is an important precursor to building features.

3. Tracer bullets for new features

Tracer bullets are small slices of functionality that go through all layers of the system, allowing you to test and validate your approach early. This helps in identifying potential issues and ensures that the overall architecture is sound before investing significant time in development.

TL;DR - build a tiny, end-to-end slice of the feature first, then expand it out.

4. Polish and quick wins
5. Refactors

# EXPLORATION

Explore the repo.

# IMPLEMENTATION

Use the `tdd` skill to complete the task.

# FEEDBACK LOOPS

Before committing, run the feedback loops:

- `uv run pytest` to run the tests, if the project has been scaffolded
- `uv run ruff check` to run linting, if ruff has been configured
- `uv run pyright` or `uv run mypy` to run type checking, if either has been configured

If a feedback loop is not available yet because the project has not been scaffolded or the tool is not configured, note that explicitly in the final response and in the issue update.

# COMMIT

Make a git commit. The commit message must:

1. Include key decisions made
2. Include files changed
3. Blockers or notes for next iteration

# THE ISSUE

If the task is complete:

- Change `Status:` to `done`
- Mark completed acceptance criteria with `[x]`
- Move the issue file to `.scratch/kursbuchungs-bot/issues/done/`

If the task is not complete, add a note to the issue file with what was done.

# FINAL RULES

ONLY WORK ON A SINGLE TASK.
