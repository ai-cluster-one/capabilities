"""The plain text of a message Telegram composed with rich formatting.

Current Telegram apps compose a paragraph followed by a list as a rich message:
the message's own text arrives empty and the words live only in
`rich_message.blocks`, a tree of Instant View page blocks whose leaves are
rich-text nodes.  Every reader of an account needs those words as text - the
assistant service for the worker's tail, the read verbs for what they print -
so the tree is flattened here once rather than at each reader.

The walk reads the tree by shape, not by importing Telethon's classes, so it
answers the same for the layer's types and for any block a later layer adds
that still carries its words in `text`, `blocks` or `items`.
"""

_LIST_BLOCKS = {"PageBlockList", "PageBlockOrderedList"}

# The fields a block keeps its words in, in reading order.  A field holding
# something other than words (an id, a url, a photo) is never read.
_BLOCK_FIELDS = ("title", "text", "source", "blocks", "items", "rows", "caption")


def rich_text(node) -> str:
    """One rich-text node flattened to its string; formatting is dropped."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    texts = getattr(node, "texts", None)
    if isinstance(texts, (list, tuple)):
        return "".join(rich_text(part) for part in texts)
    inner = getattr(node, "text", None)
    if inner is not None:
        return rich_text(inner)
    for field in ("alt", "source"):
        value = getattr(node, field, None)
        if isinstance(value, str):
            return value
    return ""


def _list_lines(block) -> list[str]:
    ordered = type(block).__name__ == "PageBlockOrderedList"
    start = getattr(block, "start", None)
    first = start if isinstance(start, int) else 1
    lines = []
    for index, item in enumerate(getattr(block, "items", None) or (), start=first):
        if ordered:
            num = getattr(item, "num", None) or getattr(item, "value", None) or index
            prefix = f"{num}. "
        else:
            prefix = "- "
        if getattr(item, "checkbox", False):
            prefix += "[x] " if getattr(item, "checked", False) else "[ ] "
        nested = getattr(item, "blocks", None)
        body = (blocks_lines(nested) if isinstance(nested, (list, tuple))
                else [line for line in rich_text(getattr(item, "text", None)).splitlines()
                      if line.strip()])
        if not body:
            continue
        lines.append(prefix + body[0])
        lines.extend("  " + line for line in body[1:])
    return lines


def block_lines(block) -> list[str]:
    """The lines one page block reads as."""
    if type(block).__name__ in _LIST_BLOCKS:
        return _list_lines(block)
    lines = []
    for field in _BLOCK_FIELDS:
        value = getattr(block, field, None)
        if value is None or isinstance(value, bool):
            continue
        if field == "rows":
            for row in value or ():
                cells = [rich_text(getattr(cell, "text", None)).strip()
                         for cell in getattr(row, "cells", None) or ()]
                if any(cells):
                    lines.append(" | ".join(cells))
        elif field in ("blocks", "items") and isinstance(value, (list, tuple)):
            lines.extend(blocks_lines(value))
        else:
            lines.extend(line for line in rich_text(value).splitlines() if line.strip())
    return lines


def blocks_lines(blocks) -> list[str]:
    lines = []
    for block in blocks or ():
        lines.extend(block_lines(block))
    return lines


def rich_message_text(message) -> str:
    """The words of a message's rich content, one line per paragraph and list
    item, or "" for a message that carries none."""
    rich = getattr(message, "rich_message", None)
    blocks = getattr(rich, "blocks", None) if rich is not None else None
    if not isinstance(blocks, (list, tuple)):
        return ""
    return "\n".join(blocks_lines(blocks))
