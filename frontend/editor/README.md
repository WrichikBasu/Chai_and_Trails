# The post editor

The visual editor used to write threads and replies. It is built on
[TipTap](https://tiptap.dev) (ProseMirror) and bundled into one file,
`static/js/editor.js`, which Django serves like any other static file.
Nothing here runs on the server; Node is only needed to rebuild the bundle.

## How it fits together

- `src/editor.js` turns each `[data-visual-editor]` block into an editor. The
  form's own text box stays in the page, hidden, and always holds the post as
  **Markdown**, so the server receives and stores exactly what it did before
  (`forum/rendering.py` renders and cleans it). Without JavaScript the text box
  is used directly.
- `src/photo.js` is the photo block: placed from the tray, resized by dragging
  its corner (or with − and +). Its width is saved in the Markdown as
  `![photo](attachment:41){width="60%"}`.
- `src/tray.js` is the "Your photos" tray: uploads to `/photos/upload/`, shows
  the photos, and inserts one at the cursor when you choose **Insert**.
- `src/markdown.js` writes the editor's document out as Markdown.

## Building and testing

The Python project doesn't need Node installed: run it through `uv`.

```sh
cd frontend/editor
uvx --from nodejs-wheel npm install      # once, and after changing package.json
uvx --from nodejs-wheel npm run build    # writes static/js/editor.js and editor.js.LICENSES.txt
uvx --from nodejs-wheel npm test         # the editor's tests, in jsdom
```

Commit the rebuilt `static/js/editor.js` and `editor.js.LICENSES.txt` along with
any change to `src/`. `node_modules/` and `build-meta.json` are not committed.
