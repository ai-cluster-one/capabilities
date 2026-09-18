# Exploring with a model in the caller's chair

A scenario is written before the call and a session is decided during it, by you. An exploration is decided during the call by a driver model: you declare where the call must go, and the driver invents every caller line after reading the agent's last one.

Run `callvatest help` for the setting that declares explorations, personas, the driver and the budget, and for the report and its verdicts. This guide is the method: when to explore, how to declare one that finds something, and what to do with what it finds.

## Explorations find, scenarios prove

A scenario's input is byte-identical on every run, so red-then-green is evidence that something changed. An exploration cannot give that: two runs of one exploration are two conversations, and the driver's wording differs each time on purpose. So an exploration never gates, its verdict vocabulary is not pass and fail, and nothing downstream should read it as either.

What it gives instead is breadth you would not have typed. A caller who mumbles, wanders, insists, or answers a different question than the one asked is exactly the caller a scenario author never writes, because the author knows what the agent expects. The driver does not, and a persona makes it worse on purpose.

Reach for a session when you have one specific question and want to ask it yourself. Reach for an exploration when the question is what a real caller would do to this agent, and you want the answer several times over without spending your own attention on each line.

## Declare direction, never dialogue

An intent is the caller's purpose and the facts they hold, in one or two sentences of prose. Stages are the points the call must pass through, in order, each one line. Neither says what the caller will say; the driver invents that, which is the point. An intent that reads like a script, or a stage that quotes a line, has turned the exploration back into a scenario with extra cost.

Keep stages coarse and few. Four stages that a real call would pass through is a good exploration; twelve that pin the agent's every reply is a scenario wearing a costume.

## A persona changes how, never what

A persona is one paragraph describing how a caller speaks and behaves. It never changes what the caller wants: that is the intent's job, and a persona that smuggles a different goal produces a run nobody can read.

Write personas for the callers who cost you: the ones who trail off, who give the answer to the previous question, who push back twice before accepting anything, who say everything at once. Personas are the project's own; the harness ships none and plays no particular caller.

## Spend the budget, not money

The budget is counted in turns and calls, and both are required. Turns are the most caller lines a run may send across every call it places; calls are the most calls it may place. A run whose first call ends under the caller before the stages are done dials again while both allow, and the driver carries the conversation into the next call.

The harness does not know what a call costs, so it never counts money and never guesses at it. Choose the ceiling from what a stage should take a real caller, then leave room for one wrong turn per stage.

## Read the report as observations

The report says which stages were reached and where, what stopped the run, and one record per call: the transcript, the tool runs with their arguments, and the post-call fields, the same evidence a `run` report holds without its checks. The driver's notes are one sentence per turn where the agent did something a caller would find wrong, odd or unhelpful. Treat them as a caller's impression to check against the evidence, never as a verdict.

`stalled` says a budget ran out before the stages were reached. Read the turns before raising the ceiling: an agent that loops is a finding, and a bigger budget only makes the loop longer.

## Turn a finding into a scenario

An exploration that found something has done its job when the finding becomes the smallest scenario that fails on it. Take the caller lines from the report's transcript, keep only the turns that reach the fault, and assert the decision the agent got wrong. That scenario is what gates; the exploration is what found it.
