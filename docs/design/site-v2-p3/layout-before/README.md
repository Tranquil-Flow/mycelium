# Attributed layout shifts before the zero-CLS correction

These actual observer receipts were captured at `dc87ecf8`, before the final hydration-slot correction. The existing `< 0.01` budget passed, but the maximum captured CLS was `0.0008564557383140713`, not zero.

The observer identified:

- Chat heading's inner DIV: adding the model subtitle changed its height and vertically recentered it.
- `#chat-metrics`: the preceding status text changed width and moved the metrics horizontally.
- `#composer-reason`: right alignment of an intrinsic-width message moved its left edge as the reason changed.
- The static privacy paragraph in the detail "Who runs it" card: empty owner/model paragraphs gained height and displaced it below the viewport.

The correction reserves stable boxes for those fields (and the detail description). `test-results/zero-cls-red.*` retains the exercised failure at `0.0006766068572370114` on the delayed desktop detail route; the four viewport/theme route cases then passed. The complete browser file now requires **CLS == 0**, strengthening the previous budget rather than hiding these small shifts or loosening it. Final full-file stability runs must use the corrected source.
