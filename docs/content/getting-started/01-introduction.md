---
title: "Introduction"
description: "What actor.sh is and why it exists."
slug: "introduction"
weight: 1
---

actor.sh lets your main Claude Code session spin up other coding agents — full Claude or Codex agents, each on its own git branch in its own worktree — and bring their results back into the same conversation. You stay in one place. The actors do the work in parallel. Their answers come to you.

Most multi-agent setups quietly turn you into the middle manager: opening terminals, spinning up branches, copying prompts around, checking back on each, deciding what's next. actor.sh moves that coordination into the conversation you're already having with your main agent. The dispatcher loop runs in the chat. You stay focused on intent.

## How it works

You launch one main session with `actor main`. From there, your agent can spawn actors, hand them focused tasks, and let them run. Each actor is a real coding agent — `claude` or `codex` — executing on its own branch inside `~/.actor/worktrees/<actor-name>/`, isolated from your main checkout and from every other actor.

When an actor finishes, a channel notification arrives in the main conversation: "the auth-refactor actor is done; here's what it changed." The main agent reads the result and decides what's next — review, follow up, ask another actor a related question, or hand it back to you.

## What you can do with it

Things people actually do, today:

- **Investigate in parallel.** Three actors read different parts of a codebase; the main agent synthesizes their findings.
- **Try a few approaches at once.** Two actors take the same bug and fix it differently. You compare diffs and ship the better one.
- **Refactor and document side by side.** One actor restructures `src/auth/`; another writes the matching docs update. They report back; you merge.
- **Hand off the long jobs.** Big tests, slow installs, multi-file scans — an actor chews on it while you keep moving in the main session.
- **Build the docs site.** This site was built by actors — content, layout, deploy workflow — three actors running in parallel from one conversation.

## Step in when it matters

Actors aren't fire-and-forget. When one hits a decision only you can make, or you want to push it through a sticky bit yourself, you can take it over: `actor watch` opens a live terminal into the running session. You work directly with the actor, hand control back when you're done, and the main conversation keeps humming.

That last part matters. The actors do most of the work; the main agent does most of the wrangling; you step in only when something genuinely needs you.

## Actors vs subagents

Claude Code has subagents — short-lived helpers a single session dispatches for parallel throughput within one job (drafting four sections at once, fanning out four searches). They run, return their result, and dissolve.

Actors are different. Each actor is a peer-level coding agent with its own working tree, its own conversational thread, and its own life. You can come back to an actor an hour later — or a week later — and it picks up where you both left off. Subagents are how a single collaborator parallelises within their scope; actors are how distinct workstreams divide labor at the peer level.

## What's next

[Installation](/getting-started/installation/) gets actor.sh on your machine and registered with Claude Code. Then [Your first actor](/getting-started/first-actor/) walks you through spawning one end-to-end and inspecting the work it did.
