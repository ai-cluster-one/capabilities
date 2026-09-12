"""Which forum topic a Telegram message belongs to.

One question with four wire shapes behind it — a message posted straight into a
topic, a reply threaded inside one, a topic's own root service message, and a
message in a chat that carries no topics at all — and every reader of an
account asks it: the assistant service to decide which room a message arrived
in, a read verb to say which room it is reporting.  The shapes are Telegram's
and they are not guessable from one example, so the answer is derived here once
rather than at each reader.

What the wire does not carry is General.  Telegram lists it as a topic and
marks none of its messages, so a caller has to say what General means where it
stands; each one knows that for itself and passes it in.
"""

GENERAL_TOPIC_ID = 1


def message_topic_id(message, general=None):
    """Canonical forum-topic root id, or None for a message in no topic.

    `general` answers for a message the wire does not mark as a topic message:
    a callable taking the message and returning the topic id it belongs to, or
    None where the caller has no answer.  It is consulted lazily, only on the
    messages that reach that branch.
    """
    reply = getattr(message, "reply_to", None)
    # Telegram marks every message inside a forum topic with forum_topic. The
    # general topic leaves the flag unset and carries reply_to_top_id with the
    # root of a plain reply chain, so the flag alone separates a topic from an
    # ordinary threaded conversation.
    is_topic = bool(
        getattr(message, "forum_topic", False)
        or getattr(reply, "forum_topic", False)
    )
    if not is_topic:
        return general(message) if general is not None else None
    for value in (
        getattr(message, "reply_to_top_id", None),
        getattr(reply, "reply_to_top_id", None),
        getattr(message, "topic_id", None),
    ):
        try:
            if value is not None and int(value) > 0:
                return int(value)
        except (TypeError, ValueError):
            pass
    # A topic's root service message and a direct post into a topic expose only
    # reply_to_msg_id / the message id rather than reply_to_top_id.
    for value in (
        getattr(message, "reply_to_msg_id", None),
        getattr(reply, "reply_to_msg_id", None),
        getattr(message, "id", None),
    ):
        try:
            if value is not None and int(value) > 0:
                return int(value)
        except (TypeError, ValueError):
            pass
    return None
