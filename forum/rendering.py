"""Turn what a member typed (Markdown) into the HTML a post page shows.

Two layers, so a slip in one is caught by the other:
1. markdown-it renders CommonMark with raw HTML switched off, so <script> in a
   post comes out as visible text, and links with unsafe schemes such as
   javascript: are not turned into links.
2. nh3 then removes every tag and attribute not listed below.
"""

from typing import Final

import nh3
from markdown_it import MarkdownIt
from markdown_it.rules_core import StateCore

ALLOWED_TAGS: Final[set[str]] = {
    'p', 'br', 'strong', 'em', 's', 'a', 'code', 'pre', 'blockquote', 'hr',
    'ul', 'ol', 'li', 'h3', 'h4', 'h5', 'h6', 'table', 'thead', 'tbody', 'tr', 'th', 'td',
}
ALLOWED_ATTRIBUTES: Final[dict[str, set[str]]] = {'a': {'href', 'title'}, 'ol': {'start'}}
ALLOWED_URL_SCHEMES: Final[set[str]] = {'http', 'https', 'mailto'}
# Member links get no search-engine credit (nofollow, ugc) and can't reach back into this tab.
LINK_REL: Final[str] = 'nofollow ugc noopener noreferrer'


def _shift_headings(state: StateCore) -> None:
    """Render # as <h3>: the page's <h1> is the thread title and <h2> the section headings."""
    for token in state.tokens:
        if token.type in ('heading_open', 'heading_close'):
            token.tag = f'h{min(int(token.tag[1]) + 2, 6)}'


_markdown: Final[MarkdownIt] = (
    MarkdownIt('commonmark', {'html': False, 'breaks': True})
    .enable(['table', 'strikethrough'])
    .disable('image')  # photos arrive with attachments; ![...](url) shows as a plain link until then
)
_markdown.core.ruler.push('shift_headings', _shift_headings)


def render_body(source: str) -> str:
    """Markdown in, sanitised HTML out. Safe to mark as safe in templates."""
    html = _markdown.render(source)
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=ALLOWED_URL_SCHEMES,
        link_rel=LINK_REL,
    )
