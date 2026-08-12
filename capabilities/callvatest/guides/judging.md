# Semantic assertions, and judging the judge

Some claims about a voice agent cannot be settled in code. An agent speaks, so
it says a time as words and a number as a phrase, and no regular expression will
call that the same value as the tool result behind it. A `semantic` assertion
hands one such claim to a judge model.

Run `callvatest help` for the connections and judge declaration surface. This
guide is the method for using one well.

## Give the judge only what code cannot take

Code is exact, free, and never flickers: which tool ran, with which arguments,
how many times, in what order, whether an internal id leaked. Everything in that
list stays a deterministic check, always.

A judge is for the remainder — whether what the agent *said* is faithful to what
the tool *returned*. It is given the tool results of that turn as ground truth,
what the agent said, and the one claim to settle. A call-level claim is given
the whole transcript and every run instead.

## Write a claim that can be refused

A good claim is one sentence, checkable against the evidence the judge is
handed, and false in some plausible run. "The agent offers the earliest slot
from the result" can fail. "The agent is helpful" cannot fail and therefore
proves nothing.

Keep the claim about faithfulness rather than wording. Politeness, word order,
and phrasing are not the agent's contract; answering from the result is.

A judge that cannot answer fails the check. It never passes it, so an
unreachable backend or an unparseable verdict surfaces as a red run rather than
a quiet green one.

## Judging the judge

A judge is a model, so it is a dependency with its own failure mode: it can
agree with everything put in front of it and still look like it is working. The
`fixtures` command exists for that. Each case freezes the tool results, what the
agent said, the claim, and the verdict a competent judge must reach — then the
whole set runs without placing a call.

Because no call is placed, fixtures are free and fast. Run them when a claim is
reworded, when a backend changes, and before trusting a suite whose verdicts you
have not read in a while.

## Half the cases must expect false

A set where every case expects `true` cannot tell a judge that reads from one
that agrees with everything. The false cases are the ones that carry the signal:
the agent said a time that is not in the result, answered a different question,
or invented a detail.

Build the false cases by mutating a true one minimally — change the hour, drop
the qualifier, swap the day — so the set measures discrimination rather than
obviousness.

## Mark a measured limit as a gap, not a failure

`known_gap: true` marks a case every declared judge already fails. It still runs
and is still reported, but it does not fail the run, because a set that is
permanently red gates nothing. The day a judge starts getting it right is
reported too, which is the point of keeping the case.

Reach for it only after every declared backend has failed the case — a gap
recorded for one model's weakness hides a bug in the claim.

## Choosing a backend on material that matters

The model is named per judge in the connections registry, so a backend is
switched there or with `--judge` for one run, never inside a scenario. A
scenario states its claim; which model settles it is configuration.

`--all-judges` runs the whole fixture set past every declared judge. That
comparison is worth more than any published benchmark, because it is measured on
the claims and the language this project actually asserts.

A judge runs after the call has ended, off the critical path, so accuracy
decides the choice and latency does not.
