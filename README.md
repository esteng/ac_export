# ac_export

Export your OpenReview area-chair assignments to a folder of static HTML you
can read offline — for when the OpenReview web app is down but the API is fine.


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
python openreview_ac_export.py --no-pdf              # skip the PDF downloads
```

Defaults to your Area Chair assignments for `EMNLP/2026/Conference`; `--venue`
and `--role` override that.

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
    paper.pdf         the submission PDF
    metareview.html   the venue AC metareview page
    forum.html        the review discussion thread
```

**index.html** — every assignment, each tagged with whether your recommendation
is filled in and the AC score (`overall_assessment`) from the review-forum
metareview. Sort on either one, or on submission number, with the dropdown, and
flip the order with the button next to it. Papers with no AC score yet stay at
the bottom whichever way the list is sorted.

**metareview.html** — your metareview (recommendation, confidence, comment to
authors, message to PCs), the authors' response, a reviewer score table with
issue reports flagged, and the review-forum metareview.

**forum.html** — the discussion threaded like OpenReview shows it, with type
filtering, collapsing, and markdown rendering. Styling downloads OpenReview's
own stylesheets and webfonts; `--no-css` uses a bundled look-alike instead.

The PDF is linked from the nav bar on both pages, from the forum header, and
from the index. It is stored in the paper's own directory, so the whole export
— pages, styling, fonts, papers — works with no network.

Both viewers parse `data.json` in the browser, so anything they don't show is
still in the file.

## Notes

- Assignments come from the `<venue>/<role>/-/Assignment` edges, the same
  source the AC console uses, with a fallback to role-group membership.
- OpenReview rate limits hard; the client paces itself and backs off on 429.
  A 50-paper export takes a few minutes and lands at roughly 5 MB per paper,
  almost all of it PDF. `--skip-existing` will fetch a PDF that is missing from
  an otherwise-cached paper, so an interrupted run resumes cheaply.
- EMNLP is a commitment venue: reviews live on the ARR forum named in the
  submission's `paper_link`, so both forums are fetched. The score table reads
  the ARR review fields (`overall_assessment`, `confidence`, `soundness`,
  `excitement`).
