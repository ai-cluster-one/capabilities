# Keeping memory current

How the memory service keeps each project's block rendered and consolidated, which mode to run it in, and how to review what a grooming pass did.

## Why a service

The block a session reads is a file in the project's envelope, rendered from the store. Every write through the command line renders it again, but a write made on another machine reaches this machine only through the store, and the window moves with the clock. The service follows each project's change version in the store, about once a second, and renders each project it serves when that project's memory changes, so the file stays current without anyone asking.

## Choosing a mode

On a workstation that holds several projects, run the service once for the machine and place each project on it in mode shared (`capabilities service start memory` from inside the project); the node manager runs that one process, puts the projects so placed on its list, and each project's trouble stays its own. On a server that runs one project, run the service in that project. A project is served by one process at a time, so a project that joined the machine service does not also run its own. Keeping either running under a supervisor is the deployment capability's domain: start at `deployment help`.

## Consolidation

A project's memory grows by appends. The window is its recent, unpinned entries, and it fits while it stays within the entry count and the characters the settings allow. When it no longer fits, the service runs one grooming pass for the project: an agent turn reads the entries and proposes merges of related or contradicting entries and drops of entries that no longer hold, and the capability checks every proposal before applying any of it. Pinned entries are never touched, quotations survive word for word, and nothing is deleted.

Passes run at most once per project in the configured interval, only after the project has been quiet for a while, one machine at a time, and only where the profile's harness is installed and logged in headless. A project that wants no passes turns consolidation off in its settings; `memory service doctor` says whether consolidation is on and why not.

## Reviewing a pass

Every pass appears in the trail under one pass id, with the entries it merged and dropped and the reason it gave for each. Read it with the log, run a pass by hand as a dry run to see what the next one would propose, and undo a pass that flattened something it should not have: undoing reactivates what the pass superseded or dropped and drops what it made. `memory help` states the verbs and the settings that govern all of this.
