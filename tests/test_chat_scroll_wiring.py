"""The research page uses src/lib/chatScroll.mjs (tested by Node in
frontend/tests/chatScroll.test.mjs) and no longer scrolls on every update."""

from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "app" / "research" / "page.tsx").read_text(encoding="utf-8")


def test_the_page_follows_only_while_at_the_bottom():
    assert 'from "@/lib/chatScroll.mjs"' in PAGE
    assert "followAfterScroll(lastTopRef.current, el)" in PAGE
    assert "if (followingRef.current) toBottom();" in PAGE
    assert 'scrollIntoView({ behavior: "smooth" })' not in PAGE


def test_there_is_a_jump_to_latest_button_and_asking_resumes_the_follow():
    assert "Jump to latest" in PAGE and "shouldShowJump(following" in PAGE
    send = PAGE[PAGE.index("async function send("):][:400]
    assert "follow(true)" in send
