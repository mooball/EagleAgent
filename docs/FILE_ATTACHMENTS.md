# File Attachments Implementation

This document describes the file attachment system for EagleAgent, which allows users to upload images, PDFs, text files, and audio files during conversations.

## Overview

The system stores files on **local disk** and processes them for use by the AI agent:

- **Images**: Analyzed using Gemini's vision capabilities
- **PDFs**: Text extracted and added to conversation context
- **Text files**: Content read and included in messages
- **Audio**: Metadata stored (transcription to be implemented)

## Architecture

```
User Upload → Chat UI (POST /chat-ui/upload) → data/attachments/{object_key}
                                                            ↓
                                        transcript.create_element() → elements row
                                                            ↓
                                    Document Processing → Agent Context
```

Files are saved to `DATA_DIR/attachments/` (default `./data/attachments/`) and served back to the browser via a Starlette `StaticFiles` mount at `/files`.

## Setup Instructions

### 1. Configure Environment

Ensure your `.env` file has:

```bash
# Root directory for persistent data (default: ./data)
DATA_DIR=./data

# Temporary files upload folder (default: .files)
# Scratch space for downloads etc. — NOT where chat uploads are stored
TEMP_FILES_FOLDER=.files
```

**Note**: Chat uploads live under `DATA_DIR/attachments/`. `TEMP_FILES_FOLDER` is unrelated scratch space (browser downloads and similar) and should be gitignored.

### 2. Install Dependencies

```bash
uv sync
```

Key dependencies for file processing:
- `pdfplumber` - PDF text extraction
- `Pillow` - Image processing
- `aiofiles` - Async file I/O in the upload route

### 3. Run the Application

```bash
./run.sh
```

The `data/attachments/` directory is created automatically on startup.

## File Upload Configuration

Uploads are handled by `POST /chat-ui/upload` (`includes/dashboard/routes/chat_ui.py`).
The observed MIME type comes from the browser's `content_type`; **there is no
accept-list or size cap enforced server-side today** — the old `.chainlit/config.toml`
limits went away with Chainlit. If you need limits, add them to that route.

**Supported file types**:
- Images: PNG, JPEG, GIF, WebP, etc.
- Documents: PDF
- Text: TXT, MD, CSV, JSON, etc.
- Audio: MP3, WAV, M4A, etc.

## How It Works

### Upload Flow

1. **User uploads file** via paperclip icon in chat UI
2. **Ownership check** — the thread must belong to the caller
3. **Local storage** - file saved to `data/attachments/{object_key}`, where
   `object_key = {user_id}/{element_id}/{safe_name}`
4. **Document processing**:
   - Images → Base64 encoded for Gemini vision
   - PDFs → Text extracted from all pages
   - Text files → Content read and decoded
   - Audio → Metadata stored (transcription pending)
5. **Metadata saved** - an `elements` row is written with `forId = NULL`, i.e. **pending**
6. **Multimodal message** - sending the message attaches the element row to its step
7. **Agent processes** - AI can analyze images or reference document content

### Storage Structure

```
data/attachments/
└── {user_id}/
    └── {element_id}/
        ├── image1.jpg
        ├── document.pdf
        └── notes.txt
```

Files are served at `/files/{object_key}` via the Starlette `StaticFiles` mount.

### Database Schema

Files are tracked in the PostgreSQL `elements` table:

```sql
CREATE TABLE elements (
    id UUID PRIMARY KEY,
    threadId UUID,
    type TEXT,
    name TEXT,
    url TEXT,           -- Relative URL path (e.g. /files/{thread_id}/{element_id}/name)
    objectKey TEXT,     -- Storage path relative to attachments dir
    mime TEXT,
    createdAt TIMESTAMP
);
```

## Processing Modules

### document_processing.py

Document processing utilities:

- `process_image(file_bytes, mime_type)` → Base64 for Gemini vision
- `extract_pdf_text(file_bytes)` → Text from all PDF pages
- `extract_text_from_file(file_bytes, mime_type)` → Generic text extraction
- `process_audio(file_path, mime_type)` → Metadata (transcription TBD)
- `create_multimodal_content(text, images)` → LangChain message format

## Usage Examples

### Upload and Analyze an Image

1. Click paperclip icon in chat
2. Select an image (e.g., product photo, diagram)
3. Type: "What do you see in this image?"
4. Agent uses Gemini vision to analyze

### Upload and Extract PDF Content

1. Upload a PDF document
2. Type: "Summarize this document"
3. Agent reads extracted text and provides summary

### Upload Text File for Context

1. Upload a TXT/CSV file
2. Type: "Based on this data, what trends do you see?"
3. Agent analyzes file content

## Monitoring & Debugging

### View Upload Logs

Logs are written to console during file uploads:

```
INFO - includes.document_processing - Extracted text from PDF: 1234 characters
```

### Database Queries

```bash
# View all uploaded files
psql $DATABASE_URL -c "SELECT name, mime, createdAt FROM elements ORDER BY createdAt DESC LIMIT 10;"

# Count files by type
psql $DATABASE_URL -c "SELECT mime, COUNT(*) FROM elements GROUP BY mime;"
```

## Troubleshooting

### Files Not Processing

1. Check the MIME type was detected correctly (the browser supplies it)
2. Check logs for processing errors
3. Ensure dependencies installed: `uv sync`

### Vision Not Working for Images

1. Verify Gemini model supports vision (gemini-2.0-flash-exp does)
2. Check image format is supported (PNG, JPEG, GIF, WebP)
3. Review logs for base64 encoding errors

## Security

### Access Control

- Files stored on **local disk** under `DATA_DIR/attachments/`
- Served via Starlette `StaticFiles` mounted at `/files`
- Non-root container user (uid 1000) in production

### File Validation

- MIME type comes from the upload and drives how the file is processed
- **No server-side accept-list or size cap today** (the Chainlit config limits
  are gone). Add them in `POST /chat-ui/upload` if needed.
- Path traversal is prevented by keeping only `os.path.basename()` of the
  supplied filename when building the `object_key`

### Data Privacy

- Files stored on the **Railway application volume** (Singapore region)
- User-specific folders prevent cross-tenant access, and every upload/download
  is checked against thread ownership
- No external cloud storage — files never leave the application host

## Future Enhancements

- [ ] Audio transcription using Whisper API
- [ ] OCR for scanned documents
- [ ] Video thumbnail extraction
- [ ] Compressed archives (ZIP) support
- [ ] Real-time file upload progress
- [ ] User file management UI (view/delete uploaded files)
- [ ] Automatic cleanup of old attachments (retention policy)

## Testing

Run the file attachment tests:

```bash
uv run pytest tests/test_file_attachments.py -v
```

Tests cover:
- Image processing and base64 encoding
- PDF text extraction
- Text file reading
- Multimodal content creation

## References

- [LangChain Multimodal](https://python.langchain.com/docs/how_to/multimodal_inputs/)
- [Gemini Vision API](https://ai.google.dev/gemini-api/docs/vision)
- [Chat UI](./CHAT_UI.md) — the upload route and how attachments attach to a message
