# Consolidating memory

How a project's memory is consolidated as it grows, the service that runs the grooming pass that does it, and how to review what a pass did.

## The block does not change

The block a session reads is constant: it says how to record and how to search, and carries no entry, count or moment. The capabilities manager composes it from memory's declaration, so nothing renders it, nothing keeps it current, and the context a session starts with is the same in every session. What changes is the store, and every entry is one search away.

## Consolidation

A project's memory grows by appends. The window is its recent, unpinned entries, and it fits while it stays within the entry count and the characters the settings allow. When it no longer fits, one grooming pass for the project consolidates it: an agent turn reads the entries and proposes merges of related or contradicting entries and drops of entries that no longer hold, and the capability checks every proposal before applying any of it. Pinned entries are never touched, quotations survive word for word, and nothing is deleted.

## The service that runs the pass

Nobody schedules grooming. Memory bundles a long-lived service whose only work is to start `memory groom --if-due` in a served agent's folder, as the agent would run it: soon after the agent is admitted, again after its memory changes in the store, and on its own recurring timer, so a pass that falls due by time alone still runs. One pass per agent runs at a time, and only a few at once on the machine.

`memory groom --if-due` keeps deciding whether a pass is due: consolidation is on, the window no longer fits, no pass ran for the project within the configured interval on any machine, nothing was written for the quiet period, no other pass holds the project, and the profile's harness is installed and logged in headless. Otherwise it exits successfully and says why not, spending no model turn, so the service may ask often and a person may run it by hand at any time.

In machine mode one process on a node serves every agent there that has memory enabled, whether enabled for the agent itself or inherited from the global default; the node manager puts each such agent on the service's list with no placement needed, and keeping the process running is the manager's (`capabilities node supervise memory`). Before every action for an agent the service checks again that the agent is on the list, still has memory enabled, that memory is not quarantined on the machine, and that the folder's project id has not changed. An agent it cannot serve is reported with the reason and the others are served. `memory service status` shows each agent, the next ask and the last pass, and `memory service doctor` whether the process runs and reaches its stores; `memory help service run` gives the whole contract.

More than one machine may serve the same agent: the store's lease keeps a second pass out, and the interval counts passes on every machine. A project that wants no passes turns consolidation off in its settings; `memory doctor` says whether consolidation is on and why not.

## Reviewing a pass

Every pass appears in the trail under one pass id, with the entries it merged and dropped and the reason it gave for each. Read it with the log, run a pass by hand as a dry run to see what the next one would propose, and undo a pass that flattened something it should not have: undoing reactivates what the pass superseded or dropped and drops what it made. `memory help` states the verbs and the settings that govern all of this.
