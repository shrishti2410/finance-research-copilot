// When the research chat follows a streaming answer down. Plain JavaScript,
// free of React, so Node's test runner can check it
// (frontend/tests/chatScroll.test.mjs, run by tests/test_frontend_logic.py).
//
// Live-site feedback, 2026-11-18: the chat kept jumping to the bottom while an
// answer streamed, even after the reader had scrolled up to read. It now
// follows only while the reader is at the bottom; any scroll up stops it, and
// a "Jump to latest" button brings the reader back.

/** How close to the bottom, in pixels, still counts as "at the bottom". */
export const NEAR_BOTTOM_PX = 80;

/**
 * @param {{scrollTop: number, scrollHeight: number, clientHeight: number}} el
 * @returns {boolean}
 */
export function isNearBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX;
}

/**
 * Whether to keep following after a scroll event.
 * Any scroll up is the reader's own, so it stops the follow, even near the
 * bottom. The page's own scrolls only go down, to the new bottom.
 * @param {number} previousTop scrollTop at the previous scroll event
 * @param {{scrollTop: number, scrollHeight: number, clientHeight: number}} el
 * @returns {boolean}
 */
export function followAfterScroll(previousTop, el) {
  if (el.scrollTop < previousTop - 1) return false;
  return isNearBottom(el);
}

/**
 * @param {boolean} following
 * @param {boolean} hasContent
 * @returns {boolean}
 */
export function shouldShowJump(following, hasContent) {
  return !following && hasContent;
}
