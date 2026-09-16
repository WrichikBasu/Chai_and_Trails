// A photo in the visual editor: a block of its own, placed at the cursor from the
// photo tray, resized by dragging its corner (or with its − and + buttons).
//
// Its width is a share of the text column, so a resized photo keeps its
// proportions on any screen. In Markdown it becomes
//     ![photo](attachment:41){width="60%"}
// which is what the server renders.

import { Node, mergeAttributes } from '@tiptap/core';

export const MIN_WIDTH = 20;   // percent; the server clamps to the same range
export const STEP = 5;         // widths snap to 5%, and − / + change them by 10%

export function clampWidth(percent) {
  const snapped = Math.round(percent / STEP) * STEP;
  return Math.min(100, Math.max(MIN_WIDTH, snapped));
}

// A width from the server's HTML: <figure class="photo" style="width: 60%"><a><img data-photo …>
function widthFrom(img) {
  const figure = img.closest('figure');
  const match = /([\d.]+)%/.exec((figure && figure.style.width) || '');
  return match ? clampWidth(Number(match[1])) : 100;
}

function button(label, text) {
  const element = document.createElement('button');
  element.type = 'button';
  element.textContent = text;
  element.setAttribute('aria-label', label);
  element.title = label;
  return element;
}

export const Photo = Node.create({
  name: 'photo',
  group: 'block',
  atom: true,
  draggable: true,
  selectable: true,

  addAttributes() {
    return {
      id: {
        default: null,
        parseHTML: (img) => Number(img.getAttribute('data-photo')) || null,
        renderHTML: (attrs) => ({ 'data-photo': attrs.id }),
      },
      src: { default: null },
      alt: { default: 'photo' },
      width: {
        default: 100,
        parseHTML: widthFrom,
        renderHTML: (attrs) => (attrs.width < 100 ? { style: `width: ${attrs.width}%` } : {}),
      },
    };
  },

  parseHTML() {
    return [{ tag: 'img[data-photo]' }];
  },

  renderHTML({ HTMLAttributes }) {
    return ['img', mergeAttributes(HTMLAttributes)];
  },

  addNodeView() {
    return ({ node, getPos, editor }) => {
      let current = node;

      const dom = document.createElement('figure');
      dom.className = 'editor-photo';
      dom.contentEditable = 'false';

      const img = document.createElement('img');
      img.draggable = false;

      const tools = document.createElement('div');
      tools.className = 'editor-photo__tools';
      const smaller = button('Make the photo smaller', '−');
      const size = document.createElement('span');
      size.className = 'editor-photo__size';
      const larger = button('Make the photo larger', '+');
      const remove = button('Remove the photo from the post', '×');
      tools.append(smaller, size, larger, remove);

      const handle = document.createElement('span');
      handle.className = 'editor-photo__handle';
      handle.title = 'Drag to resize';
      handle.setAttribute('aria-hidden', 'true');  // − and + do the same for keyboards

      dom.append(img, tools, handle);

      function show(photo) {
        img.src = photo.attrs.src || '';
        img.alt = photo.attrs.alt || '';
        dom.style.width = `${photo.attrs.width}%`;
        size.textContent = `${photo.attrs.width}%`;
      }
      show(node);

      function update(attrs) {
        const pos = getPos();
        if (typeof pos !== 'number') { return; }
        editor.view.dispatch(editor.state.tr.setNodeMarkup(pos, undefined, { ...current.attrs, ...attrs }));
      }

      smaller.addEventListener('click', () => update({ width: clampWidth(current.attrs.width - 2 * STEP) }));
      larger.addEventListener('click', () => update({ width: clampWidth(current.attrs.width + 2 * STEP) }));
      remove.addEventListener('click', () => {
        const pos = getPos();
        if (typeof pos === 'number') {
          editor.view.dispatch(editor.state.tr.delete(pos, pos + current.nodeSize));
          editor.commands.focus();
        }
      });

      // Drag the corner: the new width is the photo's width plus how far the pointer moved,
      // as a share of the text column. The document only changes when the drag ends.
      handle.addEventListener('pointerdown', (event) => {
        event.preventDefault();
        event.stopPropagation();
        const startX = event.clientX;
        const startWidth = dom.getBoundingClientRect().width;
        const column = editor.view.dom.clientWidth || startWidth || 1;
        let width = current.attrs.width;
        handle.setPointerCapture(event.pointerId);
        dom.classList.add('is-resizing');

        const move = (moveEvent) => {
          width = clampWidth(((startWidth + moveEvent.clientX - startX) / column) * 100);
          dom.style.width = `${width}%`;
          size.textContent = `${width}%`;
        };
        const stop = () => {
          handle.removeEventListener('pointermove', move);
          handle.removeEventListener('pointerup', stop);
          handle.removeEventListener('pointercancel', stop);
          dom.classList.remove('is-resizing');
          update({ width });
        };
        handle.addEventListener('pointermove', move);
        handle.addEventListener('pointerup', stop);
        handle.addEventListener('pointercancel', stop);
      });

      return {
        dom,
        update(updated) {
          if (updated.type !== current.type) { return false; }
          current = updated;
          show(updated);
          return true;
        },
        // Clicks on the size buttons and the handle belong to them, not to the editor.
        stopEvent: (event) => event.target instanceof Element
          && event.target.closest('.editor-photo__tools, .editor-photo__handle') !== null,
        ignoreMutation: () => true,
        selectNode: () => dom.classList.add('is-selected'),
        deselectNode: () => dom.classList.remove('is-selected'),
      };
    };
  },
});
