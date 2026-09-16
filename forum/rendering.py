"""Turn what a member typed (Markdown) into the HTML a post page shows.

Members may write HTML as well as Markdown, so cleaning is what keeps posts safe:
1. markdown-it renders CommonMark and passes HTML through. Links with unsafe
   schemes such as javascript: are not turned into links.
2. nh3 then removes every tag and attribute not listed below, and throws away
   <script> and <style> along with what they contain. What survives is the
   allowlist here: text formatting, lists, quotes, tables, links, photos, and
   two style properties (a photo's width and text alignment).

Photos are placed with Markdown image syntax pointing at an uploaded photo,
optionally with a width (the share of the text column, as the editor's resize
handle sets it; Pandoc uses the same attribute syntax):
    ![Chandratal at dawn](attachment:42){width="60%"}
Only photos passed in to render_body() are shown; the text in the brackets
becomes the image's alt text. Pictures from other sites are not loaded (they
would tell that site who is reading); they become ordinary links.
"""

import re
from collections.abc import Mapping, Sequence
from html import escape
from typing import TYPE_CHECKING, Final

import nh3
from django.core.files.storage import default_storage
from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.rules_core import StateCore
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict
from mdit_py_plugins.attrs import attrs_plugin

if TYPE_CHECKING:
    from .models import Attachment

PHOTO_PREFIX: Final[str] = 'attachment:'
# The post text column is at most about 60rem wide; on phones a photo spans the screen.
PHOTO_SIZES: Final[str] = '(max-width: 48rem) 100vw, 60rem'
MIN_PHOTO_WIDTH: Final[int] = 20  # percent of the text column; the editor's handle stops here too
WIDTH: Final[re.Pattern[str]] = re.compile(r'(\d{1,3})%')

ALLOWED_TAGS: Final[set[str]] = {
    'p', 'br', 'div', 'span', 'hr', 'blockquote', 'pre', 'code',
    'strong', 'b', 'em', 'i', 'u', 's', 'del', 'ins', 'mark', 'small', 'sub', 'sup',
    'ul', 'ol', 'li', 'dl', 'dt', 'dd',
    'h3', 'h4', 'h5', 'h6',  # h1 is the thread title and h2 the section headings
    'table', 'caption', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td',
    'a', 'figure', 'figcaption', 'img',
}
# Only these two properties survive in a style attribute (see filter_style_properties):
# a photo's width, and text alignment.
STYLED_TAGS: Final[set[str]] = {'p', 'div', 'span', 'h3', 'h4', 'h5', 'h6', 'figure', 'table', 'th', 'td', 'blockquote'}
ALLOWED_STYLE_PROPERTIES: Final[set[str]] = {'width', 'text-align'}
ALLOWED_ATTRIBUTES: Final[dict[str, set[str]]] = {
    tag: attributes | ({'style'} if tag in STYLED_TAGS else set())
    for tag, attributes in {
        **{tag: set() for tag in STYLED_TAGS},
        'a': {'href', 'title'},
        'ol': {'start', 'type'},
        'img': {'src', 'srcset', 'sizes', 'width', 'height', 'alt', 'loading', 'decoding', 'data-photo'},
        'th': {'colspan', 'rowspan', 'scope'},
        'td': {'colspan', 'rowspan'},
    }.items()
}
ALLOWED_CLASSES: Final[dict[str, set[str]]] = {'a': {'photo'}, 'figure': {'photo'}}
ALLOWED_URL_SCHEMES: Final[set[str]] = {'http', 'https', 'mailto'}
# Member links get no search-engine credit (nofollow, ugc) and can't reach back into this tab.
LINK_REL: Final[str] = 'nofollow ugc noopener noreferrer'


def photo_markdown(photo_id: int) -> str:
    """The line the editor inserts to place a photo; members can edit the alt text in the brackets."""
    return f'![photo]({PHOTO_PREFIX}{photo_id})'


def photo_ids(source: str) -> list[int]:
    """Ids of the uploaded photos the text places, in order, each once."""
    found: list[int] = []
    for token in _markdown.parse(source):
        for child in token.children or []:
            photo_id = _photo_id(child) if child.type == 'image' else None
            if photo_id is not None and photo_id not in found:
                found.append(photo_id)
    return found


def render_body(source: str, photos: Mapping[int, 'Attachment'] | None = None) -> str:
    """Markdown in, sanitised HTML out. Safe to mark as safe in templates."""
    html = _markdown.render(source, {'photos': photos or {}})
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        allowed_classes=ALLOWED_CLASSES,
        attribute_filter=_only_uploaded_photos,
        filter_style_properties=ALLOWED_STYLE_PROPERTIES,
        url_schemes=ALLOWED_URL_SCHEMES,
        link_rel=LINK_REL,
    )


def _photo_id(token: Token) -> int | None:
    src = str(token.attrGet('src') or '')
    number = src.removeprefix(PHOTO_PREFIX)
    return int(number) if src.startswith(PHOTO_PREFIX) and number.isdigit() else None


def _shift_headings(state: StateCore) -> None:
    """Render # as <h3>: the page's <h1> is the thread title and <h2> the section headings."""
    for token in state.tokens:
        if token.type in ('heading_open', 'heading_close'):
            token.tag = f'h{min(int(token.tag[1]) + 2, 6)}'


def _photo_paragraphs(state: StateCore) -> None:
    """A paragraph holding only photos becomes the photos themselves, as full-width <figure>s.

    Paragraph text is kept to a readable line length; a photo on its own line
    should use the whole column instead.
    """
    tokens = state.tokens
    for i in range(1, len(tokens) - 1):
        inline = tokens[i]
        if inline.type != 'inline' or tokens[i - 1].type != 'paragraph_open' or not inline.children:
            continue
        images = [child for child in inline.children if child.type == 'image' and _photo_id(child) is not None]
        blanks = [child for child in inline.children
                  if child.type in ('softbreak', 'hardbreak') or (child.type == 'text' and not child.content.strip())]
        if images and len(images) + len(blanks) == len(inline.children):
            tokens[i - 1].hidden = tokens[i + 1].hidden = True
            inline.children = images
            for image in images:
                image.meta['figure'] = True


def _render_image(self: RendererHTML, tokens: Sequence[Token], idx: int, options: OptionsDict, env: EnvType) -> str:
    token = tokens[idx]
    alt = self.renderInlineAsText(token.children or [], options, env).strip()
    src = str(token.attrGet('src') or '')
    photo_id = _photo_id(token)
    photo = env['photos'].get(photo_id) if photo_id is not None else None
    if photo is None:
        if src.startswith(PHOTO_PREFIX):
            return ''  # not one of this post's photos (another member's, or deleted)
        return f'<a href="{escape(src)}">{escape(alt or src)}</a>'  # an outside picture: linked, never loaded

    image = (
        f'<img src="{escape(photo.display_url)}" srcset="{escape(photo.srcset)}" sizes="{PHOTO_SIZES}" '
        f'width="{photo.width}" height="{photo.height}" alt="{escape(alt or "Photo")}" loading="lazy" '
        f'decoding="async" data-photo="{photo_id}">'  # data-photo lets the editor load the post back in
    )
    # Each photo links to its full-resolution original.
    if token.meta.get('figure'):
        width = _width_percent(token)
        style = f' style="width: {width}%"' if width < 100 else ''
        return f'<figure class="photo"{style}><a href="{escape(photo.file.url)}">{image}</a></figure>\n'
    return f'<a class="photo" href="{escape(photo.file.url)}">{image}</a>'


def _width_percent(token: Token) -> int:
    """The photo's width as a share of the column: {width="60%"} gives 60. Anything odd means full width."""
    match = WIDTH.fullmatch(str(token.attrGet('width') or ''))
    return min(100, max(MIN_PHOTO_WIDTH, int(match[1]))) if match else 100


def _only_uploaded_photos(element: str, attribute: str, value: str) -> str | None:
    """Let <img> load only from this site's photo storage, whatever else gets through."""
    if element == 'img' and attribute in ('src', 'srcset'):
        urls = [candidate.strip().split(' ')[0] for candidate in value.split(',')] if attribute == 'srcset' else [value]
        return value if all(url.startswith(default_storage.url('')) for url in urls) else None
    return value


_markdown: Final[MarkdownIt] = (
    # html: members may write HTML; nh3 above is what makes that safe.
    MarkdownIt('commonmark', {'html': True, 'breaks': True})
    .enable(['table', 'strikethrough'])
)
# {width="60%"} after an image, and nothing else: no attributes on links or code, and no other keys.
attrs_plugin(_markdown, after=('image',), allowed=('width',))
_markdown.core.ruler.push('shift_headings', _shift_headings)
_markdown.core.ruler.push('photo_paragraphs', _photo_paragraphs)
_markdown.add_render_rule('image', _render_image)
