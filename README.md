# ac_export

Export your OpenReview area-chair assignments to a folder of static HTML you
can read offline — for when the OpenReview web app is down but the API is fine.

No LLM, no build step, no server.

## Setup

```bash
pip install requests
cp .keys.example .keys      # add your OpenReview login
chmod 600 .keys
```

## Run

```bash
python openreview_ac_export.py                       # everything, into ./export
python openreview_ac_export.py --limit 3             # try it on 3 papers
python openreview_ac_export.py --only 502,746        # specific submissions
python openreview_ac_export.py --skip-existing       # resume without refetching
python openreview_ac_export.py --venue COLM/2026/Conference --role Senior_Area_Chairs
```

Then open `export/index.html`.

## Output

```
export/
  index.html          all your papers
  assignments.json    numbers, note ids, titles, forum URLs
  assignments.txt     just the numbers
  assets/             OpenReview's CSS + the viewer JS
  papers/502/
    data.json         every note, verbatim from the API
    data.js           the same JSON, wrapped so file:// can load it
    metareview.html   the venue AC metareview page
    forum.html        the review discussion thread
```

**metareview.html** — your metareview (recommendation, confidence, comment to
authors, message to PCs), the authors' response, a reviewer score table with
issue reports flagged, and the review-forum metareview.

**forum.html** — the discussion threaded like OpenReview shows it, with type
filtering, collapsing, and markdown rendering. Styling downloads OpenReview's
own stylesheets; `--no-css` uses a bundled look-alike instead.

Both viewers parse `data.json` in the browser, so anything they don't show is
still in the file.

## Notes

- Assignments come from the `<venue>/<role>/-/Assignment` edges, the same
  source the AC console uses, with a fallback to role-group membership.
- OpenReview rate limits hard; the client paces itself and backs off on 429.
  A 50-paper export takes a couple of minutes.
- For a commitment venue like EMNLP, reviews live on the ARR forum named in the
  submission's `paper_link` — both forums are fetched. Other venues work too,
  though the score table expects ARR-style fields (`overall_assessment`,
  `confidence`, `soundness`, `excitement`).
