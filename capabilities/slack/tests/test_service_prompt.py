from prompt import build_prompt


def test_includes_context_request_and_tail_in_order():
    out = build_prompt(
        "You are the project assistant.",
        {
            "now": "2026-07-23T00:00:00Z",
            "conversation": "D1",
            "kind": "im",
            "connection": "workspace",
            "worker": "claude",
            "assistant_name": "Project Assistant",
            "sender_name": "Alice",
            "sender_role": "default",
            "authority_summary": "youtrack",
            "request_text": "What is the schema?",
        },
        [{"sender": "Alice", "text": "hi"}, {"sender": "assistant", "text": "hello"}],
    )
    assert out.index("You are the project assistant.") < out.index("Current request")
    assert out.index("Current request") < out.index("Conversation")
    assert "What is the schema?" in out
    assert "Alice: hi" in out
    assert "assistant: hello" in out
    assert "Tool authority: youtrack" in out
    assert "Assistant: Project Assistant" in out


def test_works_without_context():
    out = build_prompt("", {"request_text": "q"}, [{"sender": "A", "text": "t"}])
    assert "Conversation" in out
    assert "A: t" in out


def test_empty_tail_ok():
    out = build_prompt("ctx", {"request_text": "q"}, [])
    assert "ctx" in out
