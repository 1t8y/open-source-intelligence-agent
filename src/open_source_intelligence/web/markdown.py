"""Markdown rendering for report bodies.

The report is authored as Markdown — `reports/composition.py` and the Agent both write it — and it
used to reach the page inside a `<pre>`, so readers saw `## 重点项目` and `**加粗**` literally. That
is the one place where the interface was showing the file format instead of the report.

Two rules decide how it is rendered:

* **Escape, do not trust.** A report quotes repository READMEs, which are third-party text a
  contributor controls. `escape=True` turns any HTML in the source into text, so a README cannot
  smuggle markup into the interface. The validator already refuses injected instructions; this is
  the second, independent layer, because escaping does not depend on the model or the guard
  agreeing with each other.
* **No raw-HTML escape hatch.** There is deliberately no way to opt out per report. A field that
  says "render this one as HTML" is a field that eventually gets set to true on data nobody
  re-read.
"""

from __future__ import annotations

import mistune
from markupsafe import Markup

#: Table and strikethrough are the two GitHub extensions the composed reports actually use; the
#: `url` plugin linkifies bare URLs, which the evidence sections contain.
_RENDERER = mistune.create_markdown(
    escape=True,
    plugins=["table", "strikethrough", "url"],
)


def render_markdown(text: str | None) -> Markup:
    """Render one Markdown document to sanitized HTML.

    Empty input renders to empty output rather than raising: a report whose body failed to
    generate is a state the page has to draw, not a 500.
    """
    if not text:
        return Markup("")
    return Markup(_RENDERER(text))
