# Screenshot inspection — scoped P3 candidate

The final capture set contains **98 PNG screenshots** at 1440×900 and 390×844 (full-page captures can be taller). The site is covered in dark and light; the existing Mac app header is covered in its shipped dark appearance. The 88 saved page/state CLS samples have maximum **0**. The complete browser file also checks held owner decisions and app-header updates at exact zero.

## Direct visual review

Representative screenshots were loaded and inspected with vision, not merely checked for file existence:

- `apex-1440-dark.png`, `apex-1440-light.png`, `apex-390-dark.png`, `apex-390-light.png`: readable hero, live-count band, three clear CTAs, honest MVP/Apple-silicon/one-Mac-demo copy, no horizontal overflow or overlapping sections. The Download CTA wraps onto its own row on mobile.
- `owner-held-approve-1440-light.png`, `owner-held-decline-390-dark.png`: Undo occupies the held action's location without the old ghost Approve label; the explanation is inside its panel and above mobile navigation. The text slot remains reserved when the server confirms Saved.
- `mac-header-1440.png`, `mac-header-390.png`: the real pending-request count is visible and separated from the brand/development badge. The rest of this app page is deliberately inert in the test facade; its Connecting message is not a live-device diagnosis.
- `chat-detail-1440-dark.png`, `chat-detail-390-light.png`, and mobile owner-inbox dark/light frames: reserved header/status/reason slots, readable controls and privacy text, and a stationary composer. The gate/conversation is an intentional internal scroll region; a full-page screenshot does not expand that region.

This is representative visual inspection plus the specified automated interaction/geometry checks, not a claim that every screenshot received an independent exhaustive audit or that Safari, Firefox, accessibility-user studies, or physical Mac-app deployment were qualified.

## Defects found and corrected

See `visual-red/README.md` for the Undo overlap, clipped mobile explanation, and delayed Saved-status shift. See `layout-before/README.md` for the exact chat/detail hydration sources. Their original receipts remain separate from the final zero-CLS captures. No threshold was loosened: the browser file's old `< 0.01` assertions were strengthened to `== 0`.
