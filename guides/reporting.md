# Reporting something

Use this guide before opening an issue. Nothing here is required, and no report is turned away for its shape - but a report that answers these lands sooner, because the first thing anyone does with a report is try to reproduce it.

Say which of two things it is. Either the product does not do what it says it does, or it does exactly that and you think it should do something else. Both are welcome and they travel differently: the first is fixed without anyone being consulted, the second is a decision about what this product is, and a person makes it.

## If something is broken

- The version - the commit you are on, or what `capabilities --version` prints. Reports frequently describe something fixed weeks earlier, and this is the cheapest way to find that out.
- The command you ran, exactly, and what it printed, exactly. A paraphrase of an error is a description of what you concluded, and the conclusion is the part most often wrong.
- What you expected, **and where we said so** - a line of a guide, a sentence of help output, a contract. This matters more than everything above it: it is what separates the product breaking a promise from the product never having made one.
- Whether it still happens on a clean install, if you can check.

## If something should be different

- What the product does today.
- Why that is not enough - the case you actually hit, rather than the improvement in the abstract.
- What would be different afterwards for someone using it.

## Two things worth knowing

A mechanism you are not sure about is still worth sending. Say what you observed and mark the guess as a guess. A confident wrong explanation costs more to unpick than an honest "I don't know why".

Do not paste credentials, tokens, or anything from a real account. Redact before you send; we cannot unsee a published secret, and neither can anyone else.
