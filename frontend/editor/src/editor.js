// Chai & Trails: the visual post editor.
//
// Turns each [data-visual-editor] into a TipTap editor. The page's own text box
// stays in the form, hidden, and always holds the post as Markdown, so the
// server receives exactly what it did before. Without JavaScript nothing here
// runs and the text box is used directly.
//
// Photos: members add them to the tray below the editor, where they upload
// straight away. Then they put the cursor where a photo should go and choose
// Insert on it. In the post, a photo's corner can be dragged to resize it.

import { Editor } from '@tiptap/core';
import { Placeholder } from '@tiptap/extensions';
import StarterKit from '@tiptap/starter-kit';

import { toMarkdown } from './markdown.js';
import { Photo } from './photo.js';
import { setUpTray } from './tray.js';

const VISUAL_HINT = 'Select text and use the buttons to format it, or type Markdown shortcuts: '
  + '**bold**, _italic_, "> " for a quote, "- " for a list.';

export function createEditor(element, content = '') {
  return new Editor({
    element,
    content,
    extensions: [
      StarterKit.configure({
        underline: false,  // not part of Markdown
        link: { openOnClick: false, autolink: true, defaultProtocol: 'https' },
        heading: { levels: [1, 2, 3] },
      }),
      Placeholder.configure({ placeholder: element.dataset.placeholder || '' }),
      Photo,
    ],
  });
}

function start(container) {
  const box = container.querySelector('textarea');
  const form = container.closest('form');
  if (!box || !form) { return; }

  const surface = document.createElement('div');
  surface.className = 'editor__surface';
  surface.dataset.placeholder = box.placeholder;
  box.after(surface);

  const saved = container.querySelector('template[data-editor-html]');
  const editor = createEditor(surface, saved ? saved.innerHTML.trim() : '');

  // The text box stays in the form but out of sight; it carries the Markdown.
  box.hidden = true;
  box.required = false;  // the server still insists on a post; a hidden required field would block submitting
  const sync = () => { box.value = toMarkdown(editor.state.doc); };
  editor.on('update', sync);
  form.addEventListener('submit', sync);
  sync();

  // Screen readers should announce the editor by the field's label.
  const label = document.querySelector(`label[for="${box.id}"]`);
  const view = editor.view.dom;
  view.setAttribute('role', 'textbox');
  view.setAttribute('aria-multiline', 'true');
  if (label) {
    label.id = label.id || `${box.id}_label`;
    view.setAttribute('aria-labelledby', label.id);
    label.addEventListener('click', () => editor.commands.focus());
  }
  const hint = container.parentElement.querySelector('[data-editor-hint]');
  if (hint) {
    hint.textContent = VISUAL_HINT;
    view.setAttribute('aria-describedby', hint.id);
  }

  container.classList.add('editor--visual');  // tells site.js to leave the buttons and Quote to this file

  const actions = {
    bold: () => editor.chain().focus().toggleBold().run(),
    italic: () => editor.chain().focus().toggleItalic().run(),
    quote: () => editor.chain().focus().toggleBlockquote().run(),
    list: () => editor.chain().focus().toggleBulletList().run(),
    link: () => {
      const current = editor.getAttributes('link').href || 'https://';
      const href = window.prompt('Link address', current);
      if (href === null) { return; }
      const chain = editor.chain().focus().extendMarkRange('link');
      (href.trim() && href.trim() !== 'https://' ? chain.setLink({ href: href.trim() }) : chain.unsetLink()).run();
    },
  };
  container.querySelectorAll('[data-md]').forEach((button) => {
    button.addEventListener('click', () => (actions[button.dataset.md] || (() => {}))());
  });

  // Quote on someone's post: add what they wrote to the end of the reply as a quote.
  if (box.id === 'reply-body') {
    document.querySelectorAll('[data-quote]').forEach((button) => {
      button.addEventListener('click', (event) => {
        event.preventDefault();
        const source = document.getElementById(button.dataset.quoteFrom);
        const paragraphs = [`${button.dataset.quote} wrote:`, ...(source ? source.innerText.trim().split(/\n{2,}/) : [])]
          .filter((text) => text.trim())
          .map((text) => ({ type: 'paragraph', content: [{ type: 'text', text: text.trim() }] }));
        editor.chain().focus('end').insertContent([{ type: 'blockquote', content: paragraphs }, { type: 'paragraph' }]).run();
      });
    });
  }

  const tray = form.querySelector('[data-photo-tray]');
  if (tray) {
    const fallback = form.querySelector('[data-photo-fallback]');
    if (fallback) { fallback.hidden = true; }  // the tray takes over from the plain file input
    setUpTray(tray, editor, form.querySelector('[name="csrfmiddlewaretoken"]').value);
  }
  return editor;
}

export function startAll() {
  document.querySelectorAll('[data-visual-editor]').forEach(start);
}
