# YouTrack — knowledge-base article authoring

The `youtrack` CLI is the adapter for reading and publishing knowledge-base
articles. Article quality comes from the authored content and its placement
in the article tree, not from the CLI. Run `youtrack help` for the current
command contract; this guide focuses on the reusable authoring method.

## Give the article a home

Every article needs exactly one home before it can be created:

- `--project <db-id>` (for example `0-6`) makes it a top-level article in
  that project's knowledge base.
- `--parent <id|url>` nests it as a sub-article under an existing one.

Choose the parent when the material is a refinement, exception, or detail of
an existing topic; choose the project root only for genuinely new topics.

## Content is Markdown, three ways in

`--content` accepts inline text, a readable file path, or `-` to read stdin.
Author substantial content in a local file so it can be reviewed and diffed
before publication; reserve inline text for short edits.

## Write one clear title, answer first

Give `--summary` one title that names the question or topic precisely — not
a restatement of the parent's title. Lead the body with the answer, then add
the supporting detail, caveats, and examples after it. A reader who stops
after the first sentence should already have the correct takeaway.

## Update in place; don't fork the tree

Before creating a new article, check whether an existing one already owns the
topic. Prefer `article-update` to revise it in place over `article-create`-ing
a near-duplicate: duplicates drift apart as one copy gets corrected and the
other does not, and they compete for the same reader's attention in search
and listings.

## Read the tree before you place new material

`youtrack article ID` returns an article's `parentArticle` and `childArticles`
references alongside its summary and content. Read these before adding a
sibling or a child: a new article belongs under the existing node that owns
its topic, not floating at the project root because that was the easiest
call to make. When a topic has outgrown its current article, split it into a
child rather than duplicating the parent's scope.
