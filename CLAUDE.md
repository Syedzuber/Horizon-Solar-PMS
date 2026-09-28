## Report format — mandatory

Every build or fix session ends its report with a section headed exactly:

COMMIT

containing:
- `git restore staticfiles/`
- one `git add` line naming every changed or new file individually
  (never `git add -A` or `git add .`; never include `.env1`,
  `railway_backup.dump` or any `.env*` file)
- the `git commit -m "..."` command

Nothing may follow the COMMIT section. A report without it is incomplete.
Audit-only sessions that change no files write "COMMIT: nothing to commit".

## Code comments

Follow `code commenting standard.txt` in the repository root. It is the
authority; this section only summarises it.

Must-follow rules:
- Every view function has a docstring: what it does and who can access it.
- Every permission check has a comment saying who is allowed and why others are blocked.
- Every filter().update() has a race-condition comment.
- Every lazy import (import inside a function) has a circular-import comment.
- Every non-obvious queryset explains what it fetches and why.
- Comments on non-obvious logic explain why the code is written this way, not what it does.
- Non-obvious model fields get an inline comment on the same line.
- Flag known issues with a TODO (issue + why deferred); never fix unrelated things in the same session.
