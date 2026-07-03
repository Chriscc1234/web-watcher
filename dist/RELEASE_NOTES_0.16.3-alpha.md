## [0.16.3-alpha] — 2026-07-01 (Watch-suggestion card no longer squishes in the chat)

### Fixed
- **The watch card in the chat dock squished/distorted when a suggestion had many search URLs.** The
  `.kv` rows are `display:flex`, and the value span had no `min-width:0`/wrapping — so a URL row with 8
  long Craigslist URLs on one line couldn't wrap, overflowed, squished the labels, and (with the new
  content auto-width) over-widened the dock. Two fixes: the card values now wrap
  (`overflow-wrap:anywhere`, label `flex-shrink:0`), and the card shows a compact **"Searches (N):
  term · term · … on <site>"** summary (decoded from the query params) instead of a wall of raw URLs.
  Card text is now HTML-escaped too. (`index.html`: `.watch-card .kv` CSS + `appendWatchCard`.)

---

sha256: 053ae59049a0bf4554c06bdc7a030b07b6161369a6251c72db12164daa87108e
