# Consolidating memory

How a project's memory is consolidated as it grows, how to schedule the grooming pass that does it, and how to review what a pass did.

## The block does not change

The block a session reads is constant: it says how to record and how to search, and carries no entry, count or moment. The capabilities manager composes it from memory's declaration, so nothing renders it, nothing keeps it current, and the context a session starts with is the same in every session. What changes is the store, and every entry is one search away.

## Consolidation

A project's memory grows by appends. The window is its recent, unpinned entries, and it fits while it stays within the entry count and the characters the settings allow. When it no longer fits, one grooming pass for the project consolidates it: an agent turn reads the entries and proposes merges of related or contradicting entries and drops of entries that no longer hold, and the capability checks every proposal before applying any of it. Pinned entries are never touched, quotations survive word for word, and nothing is deleted.

## Scheduling the pass

`memory groom --if-due` runs a pass only when one is due: consolidation is on, the window no longer fits, no pass ran for the project within the configured interval on any machine, nothing was written for the quiet period, no other pass holds the project, and the profile's harness is installed and logged in headless. Otherwise it exits successfully and says why not, spending no model turn, so it is safe to run often.

Schedule it as an automation of the project, hourly for instance, from inside the project:

```sh
printf '#!/bin/sh\nexec memory groom --if-due\n' > memory-groom.sh
automations set memory-groom --body memory-groom.sh \
    --schedule "0 * * * *" --timezone UTC \
    --description "consolidate memory when its window no longer fits"
```

The body is kept in the store and runs wherever the project's automations service runs. More than one machine may run it: the store's lease keeps a second pass out, and the interval counts passes on every machine. A project that wants no passes turns consolidation off in its settings; `memory doctor` says whether consolidation is on and why not. Keeping the automations service running is the deployment capability's domain: start at `deployment help`.

## Reviewing a pass

Every pass appears in the trail under one pass id, with the entries it merged and dropped and the reason it gave for each. Read it with the log, run a pass by hand as a dry run to see what the next one would propose, and undo a pass that flattened something it should not have: undoing reactivates what the pass superseded or dropped and drops what it made. `memory help` states the verbs and the settings that govern all of this.
