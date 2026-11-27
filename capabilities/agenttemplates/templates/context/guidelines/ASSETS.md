---
title: Assets
description: Load before keeping any file or record that should outlast the session - evidence, plans, research, session records, documents, attachments, pages, images - for where it belongs.
load: stub
order: 120
---

# Assets

This file maps the project's evidence and kept files. All of them live in the assets store, never in the agent's folder.

## What Goes Where

- `sessions/` - session records and handoffs.
- `plans/` - plans, options, and discarded approaches.
- `research/` - research, source notes, and raw findings.

These are examples, not the whole list. Every other file the work produces or receives and that has to be kept goes into assets too: documents and PDFs such as tickets, contracts, and receipts, mail attachments, pages and sites the agent builds, images, exports, and data files. Choose a name that says what it is, such as `documents/<what-it-is>.pdf` or `site/index.html`.

## How

- Markdown is saved as a versioned document: `assets save`.
- Every other file is kept byte for byte in the project's file store: `assets files add`.
- `assets help` gives every command.

The agent's folder holds only what a running session needs for a moment; a file saved there to keep it is moved into assets in the same session. A tool that saves a file to disk, such as a mail attachment or a rendered PDF, saves it to a temporary place, and the file then goes into assets.

## Promotion

Promote durable conclusions into the owning live context document. Keep assets as evidence.
