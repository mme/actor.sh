# Decision memo: aliases (#33)

## TL;DR

**Don't build aliases now.** Templates (just shipped in #29) already subsume
the core use case; the alias layer adds a new concept, new precedence rules,
and new parser surface in exchange for CLI sugar that lacks user-demand
signal. Revisit in 1–3 months of real template usage. If duplication between
templates turns out to hurt, reach for **template inheritance** — one concept
that compounds — rather than a parallel alias concept.

## Problem

From the ticket (#33, author's own framing):

> Templates say "what kind of actor this is."
> Aliases say "give me THIS specific flavor of that kind."

Sketched shape:

```kdl
alias "max" template="reviewer" prompt="Go deep." config="effort=max"
alias "quick-qa" template="qa" config="effort=min"
```

CLI: `actor new check-1 --alias max` resolves the alias, loads the referenced
template, then overlays the alias's prompt + config on top.

The ticket is explicit that this is an open question, with the author's own
doubts recorded in "Why it might not be":

> - Templates can already carry their own prompt + config, so an alias is a
>   template with extra sugar.
> - Duplicates a mental-model layer users must learn.
> - Might be solvable with multiple templates ("max-reviewer", "quick-qa")
>   without a new concept.

And a decision blocker:

> Observe how templates get used once #29 ships; if users ask for shortcuts,
> revisit.

So the real question is: does a new `alias` concept justify its cost over
what #29 already ships, and over simpler alternatives?

## Context from adjacent tickets

- **#29 (templates) — merged.** Templates already accept `agent`, `prompt`,
  and arbitrary config keys. `actor new foo --template qa` works today, CLI
  flags override template values, and the parser silently ignores unknown
  top-level nodes (`alias` included) for forward compat.
- **#30 (hooks) — open.** Hooks are top-level in settings.kdl, not
  template-scoped. They do not compete with aliases.
- **#31 (per-agent defaults) — open.** Adds an `agent "claude" { default-config { … } }`
  layer below template in the precedence chain. Orthogonal to aliases, but
  each new layer raises the cost of adding one more.
- **#38 (--configure flow) — open.** Asks the user structured questions at
  creation time. Relates to aliases: --configure reduces the pain of picking
  config per-invocation, which is part of what aliases try to shortcut.

## Options considered

### Option A — Build aliases as sketched in #33

New top-level `alias` node. `--alias NAME` on the CLI resolves to
`template + prompt + config` overrides layered at the template step.

**Pros**
- Direct solution to the use case as framed.
- Preserves "templates = kinds, aliases = flavors" as a clean narrative.
- Project-level aliases can sit atop team-shared templates — user gets a
  personal short-name for a shared building block.

**Cons**
- Adds a fifth type of thing to reason about alongside agent defaults,
  templates, CLI flags, and per-actor DB state.
- Precedence chain grows: where does the alias's overlay sit relative to
  `--config` on the CLI? (Presumably between template and CLI, but it's one
  more rule to document and defend.)
- The example in the ticket sketch is indistinguishable from "just write
  another template" — `alias "max" template="reviewer" prompt="Go deep." config="effort=max"`
  is the same information as `template "max-reviewer" { agent="claude"; prompt="Go deep. You're a reviewer."; model "sonnet"; effort "max" }`,
  minus the base-template share.
- Debuggability: "why did my actor end up with `effort=max`?" requires
  walking one extra indirection.
- Forbidding alias-of-alias and alias-without-template are both extra
  validation rules.
- No user-demand signal yet — templates shipped in 2854846 and haven't had
  time to reveal friction.

### Option B — Do nothing; lean on templates

Tell users: "if you want a `max-reviewer` flavor, write a `max-reviewer`
template." A `max-reviewer` template is a handful of KDL lines that copies
`reviewer`'s agent/prompt/model and sets `effort=max`.

**Pros**
- Zero new concepts, zero new grammar, zero new precedence complexity.
- Explicit over implicit: the full resolved behavior is visible at the
  template definition; no indirection to chase.
- Leaves room to observe real usage before adding layers.
- CLI invocation is only marginally longer (`--template max-reviewer` vs.
  `--alias max` — three extra characters worst case).

**Cons**
- Copy/paste between similar templates if the user wants three flavors of
  "reviewer". Changes to the base prompt have to be synced by hand.
- The mental model "template = preset" collapses the "kind vs. flavor"
  distinction the ticket's author finds useful.

### Option C — Template inheritance (`extends`)

If the duplication cost of Option B bites, solve it directly:

```kdl
template "reviewer" {
    agent "claude"
    model "sonnet"
    prompt "You're a code reviewer. Be concise."
}
template "max-reviewer" extends="reviewer" {
    effort "max"
    prompt "Go deep. You're a code reviewer. Be concise."
}
```

**Pros**
- One concept that compounds: templates get richer, no second vocabulary.
- Directly addresses the actual pain (duplication between flavors of the
  same kind), which is also what aliases are really trying to do.
- Maps to familiar inheritance/layering patterns — low teaching cost.
- Keeps the CLI surface unchanged (`--template max-reviewer`).

**Cons**
- Still adds parser complexity (resolve `extends` chains, detect cycles).
- Users might over-index on deep inheritance trees; we'd want a depth limit
  or a "one level only" rule to keep it sane.
- Doesn't help users who want a personal alias on top of a team template
  *without* forking the template name. (Small use case, but real.)

### Option D — Shell aliases / functions

Leave it to the user's shell:

```bash
alias new-max='actor new --template reviewer --config effort=max'
# or a function that plumbs $1 through:
new-max() { actor new "$1" --template reviewer --config effort=max; }
```

**Pros**
- Zero work in actor.sh. Already possible today.
- Users who want this can have it; users who don't, don't pay.

**Cons**
- Not discoverable by the agent side (aliases live in `~/.zshrc`, not in
  `.actor/settings.kdl`).
- Not shareable via project config.
- Shell-specific, doesn't work from the MCP tool calls.

### Option E — Parameterized templates (variable substitution)

```kdl
template "reviewer" {
    agent "claude"
    model "${model:-sonnet}"
    effort "${effort:-medium}"
}
```

With `actor new foo --template reviewer --var model=opus`.

**Pros**
- Most flexible.

**Cons**
- String interpolation is a new sublanguage to design, document, and
  implement. The cost/benefit is clearly wrong for a team that's currently
  debating whether *named shortcuts* are worth it.

## Recommendation

**Ship nothing for #33 right now. Close the discussion as "deferred, pending
usage data." In 1–3 months (or when a real user reports pain), prefer
Option C (template inheritance) over Option A (aliases) if action is needed.**

Reasoning an owner can act on in one sitting:

1. **The ticket already passed the "should we be cautious?" test** — the
   author marked it decision-pending and listed the strongest counter-arguments
   themselves. Those counter-arguments remain correct.
2. **Templates just shipped.** There is no usage signal — no issues, no
   Slack complaints, no "I keep typing the same flags" observation. Building
   on zero signal is how configuration languages bloat.
3. **The concrete example in the sketch is a weak proof.** Rewriting
   `alias "max" template="reviewer" prompt="Go deep." config="effort=max"`
   as a `max-reviewer` template costs five KDL lines and zero new concepts.
   If the user types that a lot, `--template max-reviewer` is barely longer
   than `--alias max`.
4. **If duplication does bite, aliases are the wrong fix.** The real pain
   would be "I have three reviewer flavors that share 90% of a prompt";
   that wants template composition (Option C), not a parallel alias system.
   Shipping aliases now makes the eventual "extends" harder — two
   overlapping concepts to reconcile.
5. **--configure (#38) covers the adjacent need.** Users who want to
   specify flavor per-invocation (without committing to a preset) get an
   interactive path already in flight. The alias pitch is specifically the
   "I want the same combo every time" case, which templates handle.
6. **Precedence surface is already four layers deep** (user → project →
   agent block → template → CLI → DB). Adding a fifth conceptual layer
   for a sugar case is not free — it's documentation, error messages,
   test permutations.

The cost of deferring is negligible: the config parser already ignores
unknown `alias` nodes silently (config.py:167), so no one is blocked on
us, and no forward-compat bridge has to be built if we change our minds.

## Follow-up questions for the owner

1. **Do you have a concrete workflow where templates feel insufficient, or
   is this speculative?** If concrete, what do you find yourself typing
   repeatedly?
2. **How many flavors per kind do you expect in practice?** 2 is fine with
   plain templates; 5+ makes the duplication argument real.
3. **Is the irritation CLI length, KDL duplication, or something else?**
   The fix differs per answer.
4. **Does team/shared-vs-personal layering matter to you?** (Shared
   templates in project config, personal shortcuts in user config.)
   If yes, Option A is the only one that addresses it cleanly.
5. **Would you rather ship --configure (#38) first and see if interactive
   selection reduces demand for canned shortcuts?**

## If we build it

Scope sketch — not a plan, just a landing-pad so the impl ticket doesn't
start from zero.

**Grammar.** New top-level `alias` node in settings.kdl:

```kdl
alias "max" template="reviewer" prompt="Go deep." config="effort=max,model=opus"
```

- Required property: `template=<existing template name>`.
- Optional property: `prompt=<string>`.
- Optional property: `config=<comma-separated key=value pairs>`.
- Positional arg: alias name (non-empty, unique per merged config).
- **Forbid**: alias referencing another alias (no chaining); alias
  referencing a nonexistent template (caught at load time, not resolve
  time, for fast feedback).

**Resolution.** CLI `--alias NAME` mutually exclusive with `--template`.
Alias resolution produces `(template, overlay_prompt, overlay_config)` and
feeds them into the existing template path in `cmd_new` so the rest of the
code is unchanged.

**Precedence.** Alias overlay sits at the **same layer as the referenced
template** (not a new layer). Concretely, CLI `--config` and positional
prompt still win over both template and alias overlay. This avoids
inventing a fifth precedence slot.

**Errors.** `ConfigError("unknown alias: 'X'")`, `ConfigError("alias 'X'
references unknown template 'Y'")`, `ConfigError("alias 'X' cannot
reference another alias")`.

**Testing.** Extend `tests/test_config.py` with: alias parsing, alias
resolution, alias+CLI override precedence, unknown-template error,
alias-of-alias rejection, project alias overriding user alias.

**Docs.** One section in SKILL.md (brief — "aliases are templates with a
short name and extra overrides; use them when you find yourself typing
the same --template/--config combo repeatedly").

**Explicitly out of scope for v1** (leave for later if needed):
- Alias inheritance / chains.
- Aliases that change the `agent` field (overlay agent differs from
  template agent). Require alias's template agent to win; alias cannot
  override agent. Simpler, easier to explain.
- Per-agent aliases. Aliases are template-scoped, not agent-scoped.

**Ballpark**: ~half a day of implementation + tests + doc, assuming the
design above stands on contact with reality. Not a reason to do it; just
noting that the build cost is low — the real cost is conceptual surface
area over the project's lifetime.

---

Refs #33.
