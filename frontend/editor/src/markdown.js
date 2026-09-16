// The editor's document, written out as Markdown: what the form sends and the
// server stores in Post.body_source, then renders and cleans (forum/rendering.py).

import { DOMSerializer } from '@tiptap/pm/model';
import { MarkdownSerializer, defaultMarkdownSerializer } from 'prosemirror-markdown';

const { nodes, marks } = defaultMarkdownSerializer;

// Markdown can't express text alignment or anything but the simplest tables, so those
// blocks are written as HTML, which posts allow (the server cleans it either way).
function asHtml(state, node) {
  const wrapper = document.createElement('div');
  wrapper.append(DOMSerializer.fromSchema(node.type.schema).serializeNode(node));
  state.write(wrapper.innerHTML);
  state.closeBlock(node);
}

const aligned = (node) => node.attrs.textAlign && node.attrs.textAlign !== 'left';

// prosemirror-markdown names things the ProseMirror way (bullet_list); TipTap's names differ (bulletList).
const serializer = new MarkdownSerializer(
  {
    paragraph: (state, node) => (aligned(node) ? asHtml(state, node) : nodes.paragraph(state, node)),
    text: nodes.text,
    heading: (state, node) => (aligned(node) ? asHtml(state, node) : nodes.heading(state, node)),
    blockquote: nodes.blockquote,
    codeBlock: nodes.code_block,
    horizontalRule: nodes.horizontal_rule,
    hardBreak: nodes.hard_break,
    bulletList: nodes.bullet_list,
    listItem: nodes.list_item,
    orderedList(state, node) {
      const start = node.attrs.start || 1;
      const widest = String(start + node.childCount - 1).length;
      state.renderList(node, ' '.repeat(widest + 2), (i) => {
        const number = String(start + i);
        return `${' '.repeat(widest - number.length)}${number}. `;
      });
    },
    table: asHtml,
    photo(state, node) {
      const { id, alt, width } = node.attrs;
      const size = width < 100 ? `{width="${width}%"}` : '';
      state.write(`![${state.esc(alt || 'photo')}](attachment:${id})${size}`);
      state.closeBlock(node);
    },
  },
  {
    bold: marks.strong,
    italic: marks.em,
    code: marks.code,
    link: marks.link,
    strike: { open: '~~', close: '~~', mixable: true, expelEnclosingWhitespace: true },
  },
  { hardBreakNodeName: 'hardBreak' },
);

export function toMarkdown(doc) {
  return serializer.serialize(doc, { tightLists: true });
}
