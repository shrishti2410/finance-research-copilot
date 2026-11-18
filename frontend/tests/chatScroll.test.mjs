// node --test frontend/tests/   (also run by tests/test_frontend_logic.py)
// The research chat follows a streaming answer only while the reader is at
// the bottom (live-site feedback, 2026-11-18: it kept jumping down after the
// reader had scrolled up). src/lib/chatScroll.mjs holds the decisions.
import assert from "node:assert/strict";
import { test } from "node:test";

import { NEAR_BOTTOM_PX, followAfterScroll, isNearBottom, shouldShowJump } from "../src/lib/chatScroll.mjs";

const at = (scrollTop, scrollHeight = 2000, clientHeight = 600) => ({ scrollTop, scrollHeight, clientHeight });
const BOTTOM = 2000 - 600;

test("at the bottom is near the bottom", () => {
  assert.equal(isNearBottom(at(BOTTOM)), true);
});

test("within the threshold of the bottom is near the bottom", () => {
  assert.equal(isNearBottom(at(BOTTOM - NEAR_BOTTOM_PX)), true);
  assert.equal(isNearBottom(at(BOTTOM - NEAR_BOTTOM_PX - 1)), false);
});

test("a reader who scrolls up stops the follow", () => {
  assert.equal(followAfterScroll(BOTTOM, at(BOTTOM - 300)), false);
});

test("even a small scroll up near the bottom stops the follow", () => {
  assert.equal(followAfterScroll(BOTTOM, at(BOTTOM - 20)), false);
});

test("scrolling back down to the bottom resumes the follow", () => {
  assert.equal(followAfterScroll(BOTTOM - 300, at(BOTTOM)), true);
});

test("scrolling down but not to the bottom does not resume it", () => {
  assert.equal(followAfterScroll(BOTTOM - 600, at(BOTTOM - 300)), false);
});

test("the page's own scroll to the new bottom keeps following", () => {
  // Content grew by 200 px; the effect scrolled to the new bottom.
  assert.equal(followAfterScroll(BOTTOM, at(BOTTOM + 200, 2200)), true);
});

test("Jump to latest shows only when not following and there is something to show", () => {
  assert.equal(shouldShowJump(false, true), true);
  assert.equal(shouldShowJump(true, true), false);
  assert.equal(shouldShowJump(false, false), false);
});
