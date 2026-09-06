# 09 — Attachments (photos, files, email threads, clipboard)

One concept for everything Arsen wants to hand Jarvis besides text. V1 had separate upload
routes for images and files, a thread viewer, paste_content and read_clipboard tools; V2 has
one `attachment` with one ingestion path and one place in the prompt.

## Stories

1. **From the phone**, I tap "+" in the composer, take a photo or pick one from the gallery, add a
   sentence, send — and Jarvis answers about *that picture* (the model on vader sees images).
2. **From the laptop**, I drop a PDF / Word / Excel / PowerPoint / text file onto the chat, or paste
   a screenshot or a block of text with Ctrl+V. Jarvis reads it; a long document is trimmed to a
   budget and Jarvis says so rather than pretending it read all of it.
3. **An email thread**: I type a few words of the subject, pick the thread from the matches, and
   the whole thread (in order, with senders and dates) is attached — so "reply to this" or
   "summarise where we are" works without Jarvis searching.
4. **A page from the side panel** is the same kind of thing: the extension already sends it; it
   shows up as an attachment chip like the others.
5. **What I attached is visible**: thumbnails for images, chips with name and size for files and
   threads, on the message they belong to; clicking opens the full image / the text.
6. **Nothing is silently dropped.** If the active model cannot see images, the message carries
   "[image attached: name, 1.2 MB — this model cannot see images]" and Jarvis tells me.
7. **The prompt cache survives.** Text extracted from files and threads goes into the persisted
   `context` message after my input, never into the system prefix.
8. **Uploads are bounded**: 25 MB per file, chunked so a phone on a bad link can finish; the
   store lives under `JARVIS_HOME/attachments/` and is deleted with the conversation.

## Definition of done

- `attachments` table + blob store; `POST/GET/DELETE /api/attachments`, `?thumb=1` for images.
- `run.create.attachment_ids`; the user `Message` gains `attachments: [AttachmentRef]`.
- `attachment_to_context()` for image (multimodal part), pdf/docx/xlsx/pptx/txt/md/csv (text),
  email thread (host `outlook_thread`), page; per-attachment text budget from settings.
- Vision probe per role adapter (1×1 image once, cached) → structural gate.
- Composer "+" menu (photo/camera, file, paste, email thread picker); drag-and-drop; chips and
  thumbnails on messages; works in the side panel too.
- Tests: ingestion per kind with fakes; API round trip; a real image through vader; a real
  Outlook thread through the host.
