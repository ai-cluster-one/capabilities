# Writing a monitor

Use this guide when an automation's job is to watch something and speak only when it matters - an upstream, a market, a page that will announce a date, a system whose health nobody is looking at. A monitor is the most common automation and the easiest to get quietly wrong, because a broken one and a calm one produce the same output: nothing.

Everything here assumes the general rules in `automations guide authoring` - profiles, the schema call, and where detection sits for a given surface. This guide is what a monitor adds on top.

## The script owns everything except the looking

Give the agent the judgement and keep the machinery, in this order every run: decide whether this run is due, gather or let the agent gather, judge, deduplicate against what was already reported, deliver, then record.

The agent never decides whether to send. It reports what it found and whether it believes the owner needs to know; the script compares that against state and makes the call. An agent that both judges and delivers has no witness, and the first thing to disappear is the run where it decided to say nothing.

Cadence belongs in the script too, not in the schedule alone. Declare the cron at the finest interval the monitor will ever need and let the script narrow it - weekly now, daily in the month that matters, always on a manual run. A run that is not due exits in milliseconds and costs nothing, and the cadence stays readable next to the logic it governs rather than encoded in a cron expression nobody re-reads.

## The standing brief

A monitor written once decays, because the thing it watches moves and the instructions do not. Give it a brief: a Markdown file beside the script holding what is already known - what is being watched and why, the criteria that separate a real signal from noise, the sources to check, what has already been reported, and what has not been explored yet.

The agent reads it first and edits it last. That is what turns a monitor from a fixed query into something that gets better at looking: sources it checked get annotated, sources it discovered get added, criteria sharpen whenever the watched thing states its own rules, and closed items leave.

This is the one place a monitor may need a write profile. Fence it in the prompt to that single file, and then verify rather than trust: hash the brief before and after the turn, compare against what the answer claimed, and raise the mismatch. A worker that stops maintaining its brief is invisible from the outside - it looks exactly like a period with nothing to report.

## Make the answer a schema, and make it prove itself

Pass `--schema` and require at least four things: whether anything is worth raising, the findings themselves, the message to send, and **which sources were actually reached this run**.

That last field is the difference between quiet and blind, and without it the two are the same JSON. A run that reached nothing and found nothing must not be able to report itself the same way as a run that read everything and found nothing.

Require evidence on every finding - a URL, and the sentence that carries the claim - and say in the prompt that a date, a price or a deadline is reported only where it is stated. This is where a monitor is most tempting to a model: the whole job is to come back with a date, and a plausible one is always available. Constrain it in the schema, and prefer an enum over free text wherever the script will branch on the value.

Ask for the message in the schema too, and let the agent write it in the voice its reader expects. Then send that text unchanged. Rewriting the message in code puts the judgement in one place and the wording in another, and they drift.

## Report a finding once

Deduplicate on the finding, not on the run. Give each one a stable key - an id where the source has one, otherwise a hash of the claim itself - and keep the reported keys in state.

Hashing the claim rather than the page is what makes this work: a page that is edited without changing what it says produces the same key and stays silent, while the same page finally naming a date produces a new one and speaks.

Baseline the first run. Record what is currently true, send nothing, and mark the state initialised - otherwise a monitor announces the world as it found it on the day it was born.

## A monitor that cannot fail loudly is not a monitor

Count consecutive failures per source and alert on a threshold matched to the cadence, not to a number that sounds right. A weekly monitor alerting on the second consecutive failure is silent for a fortnight; at daily cadence the same rule is a day. Say when it recovers, too, so a fixed source is known to be fixed.

Never let a monitor end silently. If it has a window, closing that window is itself an event worth a message - a hard end date buried in a constant is how a watch stops without anyone noticing, and the moment it was built for is usually just past it.

## Follow the event, not the calendar

The cadence that finds an announcement is not the cadence that catches what the announcement is about. Once a date is known, the monitor's job changes: tighten the interval as the moment approaches, and say so ahead of time rather than only when it arrives.

Write that transition in when you write the monitor. The run that discovers the date is the one least likely to be watched by a person, and a monitor that keeps its original cadence through it answers the question it was asked and misses the one that was meant.

## What a monitor costs

One turn a day is nothing next to the thing it protects, and this is why an agent-led monitor is usually the right choice at daily cadence and below. Judgement per run stops being reasonable somewhere around the interval where a person would also stop re-reading, so a monitor running every few minutes wants a diff and an alert, not a turn.

Record the cost the turn reports alongside the run's outcome. A monitor whose spend climbs while its findings do not is a prompt that has started searching instead of looking.
