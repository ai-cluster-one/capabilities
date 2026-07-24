"""Assemble the worker prompt: soft context.md + channel/participant state +
current request + the conversation tail. The worker reads its situation from the
prompt, not by inferring it from chat history."""


def build_prompt(context_md: str, state: dict, tail) -> str:
    st = state or {}
    lines = []
    if st.get("now"):
        lines.append(f"Time: {st['now']}")
    if st.get("conversation"):
        bits = [f"conversation={st['conversation']}", f"kind={st.get('kind')}",
                f"connection={st.get('connection')}", f"worker={st.get('worker')}"]
        lines.append("Channel: " + ", ".join(b for b in bits if not b.endswith("=None")))
    if st.get("sender_name"):
        lines.append(f"From: {st['sender_name']} (role: {st.get('sender_role')})")
    if st.get("authority_summary"):
        lines.append(f"Tool authority: {st['authority_summary']}")
    lines.append("Delivery: your final stdout is posted by the daemon as the reply; "
                 "use `slack post` only for intermediate progress (it routes to this chat).")
    state_block = "--- Channel state ---\n" + "\n".join(lines) + "\n\n"

    req = st.get("request_text") or ""
    request_block = ("--- Current request ---\n" + req + "\n\n") if req else ""

    history = "\n".join(f'{m["sender"]}: {m["text"]}' for m in (tail or []))
    body = f"{state_block}{request_block}--- Conversation ---\n{history}"

    ctx = (context_md or "").strip()
    return f"{ctx}\n\n{body}" if ctx else body
