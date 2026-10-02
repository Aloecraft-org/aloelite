# Repo layout and lockstep

Every path in this repo has one of four jobs.

## The four places

1. `.claude/` is the agent's brain: plans, design work, drafts, notes,
   logs, rules. Write freely here. It is maintainer-grade.
2. Functional files live outside `.claude/`: code, scripts, config, data,
   tests. No product code reads from `.claude/`.
3. Documentation outside `.claude/` is a human surface. All of it.
4. `doc/lockstep/` is the human surface that the human and the agent
   both treat as the source of truth.

Rules for writing documentation, examples and code for a human reader
are in human-surfaces.md.

## Lockstep

- Read `doc/lockstep/` before planning or acting.
- Lockstep wins. If notes in `.claude/` disagree with it, the notes are
  wrong; fix them.
- A change to lockstep is true only once the human has merged it.
  Propose it as its own commit with a one-line reason. The one
  exception is a new queue item under self_merge in authority.yaml: it
  is a request to the owner, not agreed truth.
- If work would make a lockstep statement false, the same pull request
  updates lockstep. Functional files and lockstep never drift.
- If lockstep looks wrong or stale, stop and propose the correction. Do
  not work around it.
- Draft in `.claude/`. Promote to lockstep deliberately, and only the
  conclusion.

## Do not

- Use `doc/` as a scratchpad.
- Copy lockstep content into `.claude/`. Reference it by path.
- Put anything functional under `.claude/`.
- Edit lockstep as a side effect of other work.
