# assets/

`cover.jpg` — podcast artwork. Square, 1400–3000px, JPEG or PNG.

Referenced by the feed as `<itunes:image>` and copied to
`/f/<FEED_TOKEN>/cover.jpg` at publish time. Override the path with
`FEED_ARTWORK` in `.env`. If the file is absent, publishing still succeeds and
logs a warning — podcast apps then show a blank tile.
