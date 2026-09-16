// The photo tray under the editor: add photos (button, paste or drop), watch them
// upload, then insert them at the cursor. Photos wait here, on the server, until a
// post uses them; the × button deletes one for good.

const TYPES = ['image/jpeg', 'image/jpg', 'image/pjpeg', 'image/png', 'image/webp'];
const PHOTO_NAME = /\.(jpe?g|png|webp)$/i;

// Browsers don't always know a file's type — camera files often arrive with none — so
// fall back to the name. The server checks what the file really is either way.
function looksLikeAPhoto(file) {
  return TYPES.includes((file.type || '').toLowerCase()) || PHOTO_NAME.test(file.name || '');
}

export function setUpTray(tray, editor, csrfToken) {
  const list = tray.querySelector('[data-tray-list]');
  const input = tray.querySelector('[data-tray-input]');
  const empty = tray.querySelector('[data-tray-empty]');
  const errors = tray.querySelector('[data-tray-errors]');
  const uploadUrl = tray.dataset.uploadUrl;
  const maxMb = Number(tray.dataset.maxMb) || 15;  // the server's limit, so the two never drift apart
  let uploading = 0;

  tray.hidden = false;

  function report(message) {
    const item = document.createElement('li');
    item.textContent = message;  // never HTML: file names and server messages are shown as text
    errors.append(item);
  }

  function refreshEmpty() {
    empty.hidden = list.children.length > 0;
  }

  function post(url, body) {
    return fetch(url, { method: 'POST', body, headers: { 'X-CSRFToken': csrfToken }, credentials: 'same-origin' });
  }

  // Insert where the cursor was. Clicking the button takes focus from the editor,
  // but TipTap keeps its selection, and focus() puts it back first.
  function insert(item) {
    editor.chain().focus().insertContent({
      type: 'photo',
      attrs: { id: Number(item.dataset.photo), src: item.dataset.src, alt: 'photo' },
    }).run();
  }

  async function remove(item) {
    const response = await post(item.dataset.removeUrl);
    if (!response.ok && response.status !== 404) {
      report('That photo couldn’t be removed. Try again.');
      return;
    }
    // Take it out of the post too, wherever it was inserted.
    const id = Number(item.dataset.photo);
    const spots = [];
    editor.state.doc.descendants((node, pos) => {
      if (node.type.name === 'photo' && node.attrs.id === id) { spots.push({ pos, size: node.nodeSize }); }
    });
    if (spots.length) {
      const tr = editor.state.tr;
      spots.reverse().forEach(({ pos, size }) => tr.delete(pos, pos + size));
      editor.view.dispatch(tr);
    }
    item.remove();
    refreshEmpty();
  }

  function wire(item) {
    item.querySelector('[data-tray-insert]').addEventListener('click', () => insert(item));
    item.querySelector('[data-tray-remove]').addEventListener('click', () => remove(item));
  }

  function newItem(file) {
    const item = document.createElement('li');
    item.className = 'tray__item is-uploading';
    const img = document.createElement('img');
    img.alt = '';
    img.src = URL.createObjectURL(file);  // show the photo straight away, while it uploads
    const status = document.createElement('span');
    status.className = 'tray__status';
    status.textContent = 'Uploading…';
    item.append(img, status);
    list.append(item);
    refreshEmpty();
    return { item, img, status };
  }

  function finish(parts, photo) {
    const { item, img, status } = parts;
    URL.revokeObjectURL(img.src);
    img.src = photo.preview_url;
    status.remove();
    item.classList.remove('is-uploading');
    item.dataset.photo = String(photo.id);
    item.dataset.src = photo.display_url;
    item.dataset.removeUrl = photo.remove_url;
    const actions = document.createElement('div');
    actions.className = 'tray__actions';
    const insertButton = document.createElement('button');
    insertButton.type = 'button';
    insertButton.className = 'tray__insert';
    insertButton.dataset.trayInsert = '';
    insertButton.textContent = 'Insert';
    insertButton.setAttribute('aria-label', 'Insert this photo at the cursor');
    const removeButton = document.createElement('button');
    removeButton.type = 'button';
    removeButton.className = 'tray__remove';
    removeButton.dataset.trayRemove = '';
    removeButton.textContent = '×';
    removeButton.setAttribute('aria-label', 'Remove this photo');
    actions.append(insertButton, removeButton);
    item.append(actions);
    wire(item);
  }

  async function upload(file) {
    if (!looksLikeAPhoto(file)) { report(`${file.name} isn’t a JPEG, PNG or WebP photo.`); return; }
    if (file.size > maxMb * 1024 * 1024) { report(`${file.name} is larger than ${maxMb} MB.`); return; }
    const parts = newItem(file);
    uploading += 1;
    try {
      const data = new FormData();
      data.append('photo', file);
      const response = await post(uploadUrl, data);
      const body = await response.json().catch(() => ({}));
      if (!response.ok) { throw new Error(body.error || `The upload failed (${response.status}).`); }
      finish(parts, body);
    } catch (error) {
      URL.revokeObjectURL(parts.img.src);
      parts.item.remove();
      refreshEmpty();
      report(error.message.includes(file.name) ? error.message : `${file.name}: ${error.message}`);
    } finally {
      uploading -= 1;
    }
  }

  function uploadAll(files) {
    errors.textContent = '';
    Array.from(files).forEach(upload);
  }

  list.querySelectorAll('.tray__item').forEach(wire);  // photos already waiting from earlier
  refreshEmpty();

  tray.querySelector('[data-tray-add]').addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    uploadAll(input.files);
    input.value = '';  // picking the same photo again still counts as a change
  });

  // Photos pasted or dropped into the post go to the tray, ready to insert.
  editor.setOptions({
    editorProps: {
      handlePaste: (view, event) => {
        const files = event.clipboardData && event.clipboardData.files;
        if (!files || !files.length) { return false; }
        uploadAll(files);
        return true;
      },
      handleDrop: (view, event, slice, moved) => {
        const files = !moved && event.dataTransfer && event.dataTransfer.files;
        if (!files || !files.length) { return false; }
        event.preventDefault();
        uploadAll(files);
        return true;
      },
    },
  });

  // Posting mid-upload would post without that photo; wait for it.
  tray.closest('form').addEventListener('submit', (event) => {
    if (uploading > 0) {
      event.preventDefault();
      errors.textContent = '';
      report(`Wait a moment: ${uploading} photo${uploading === 1 ? ' is' : 's are'} still uploading.`);
    }
  });
}
