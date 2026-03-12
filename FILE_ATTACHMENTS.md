# File Attachments Implementation

This document describes the file attachment system for EagleAgent, which allows users to upload images, PDFs, text files, and audio files during conversations.

## Overview

The system stores files in Local File Storage (Local File Storage) with a 30-day retention policy and processes them for use by the AI agent:

- **Images**: Analyzed using Gemini's vision capabilities
- **PDFs**: Text extracted and added to conversation context
- **Text files**: Content read and included in messages
- **Audio**: Metadata stored (transcription to be implemented)

## Architecture

```
User Upload → Chainlit UI → app.py → Local File Storage Upload → Document Processing → Agent Context
                                   ↓
                              PostgreSQL Metadata
```

## Setup Instructions

### 1. Create Local File Storage Bucket in Sydney Region

```bash
# Create bucket in australia-southeast1 (Sydney)

# Verify bucket created
```

**Why Sydney?**
- Lowest latency for Australian users (Eagle Exports + Mooball)
- Data sovereignty - files stay in Australia
- Lower egress costs for local access

### 2. Grant Service Account Permissions

```bash
# Grant storage admin permissions to the service account
  --member=serviceAccount:eagleagent-svc-account@mooballai.iam.gserviceaccount.com \
  --role=roles/storage.objectAdmin
```

### 3. Configure Environment

Ensure your `.env` file has:

```bash

GOOGLE_APPLICATION_CREDENTIALS=./service-account-key.json
GOOGLE_PROJECT_ID=mooballai

# Temporary files upload folder (default: .files)
# This is where Chainlit stores uploaded files before processing
TEMP_FILES_FOLDER=.files
```

**Note**: The `TEMP_FILES_FOLDER` directory stores temporary files during upload. This folder should be added to `.gitignore` to prevent versioning temporary files. You can change this location if needed (e.g., `TEMP_FILES_FOLDER=temp_uploads`).

### 4. Install Dependencies

```bash
poetry install
```

This installs:
- `google-cloud-storage` - Local File Storage client
- `pdfplumber` - PDF text extraction
- `Pillow` - Image processing

### 5. Run the Application

```bash
./run.sh
```

## File Upload Configuration

File uploads are configured in `.chainlit/config.toml`:

```toml
[features.spontaneous_file_upload]
enabled = true
accept = ["image/*", "application/pdf", "text/*", "audio/*"]
max_files = 20
max_size_mb = 50
```

**Supported file types**:
- Images: PNG, JPEG, GIF, WebP, etc.
- Documents: PDF
- Text: TXT, MD, CSV, JSON, etc.
- Audio: MP3, WAV, M4A, etc.

## How It Works

### Upload Flow

1. **User uploads file** via paperclip icon in chat UI
2. **File validation** - type and size checked by Chainlit
3. **Local File Storage upload** - file stored in `gs://eagleagent/uploads/{user_id}/{session_id}/{filename}`
4. **Document processing**:
   - Images → Base64 encoded for Gemini vision
   - PDFs → Text extracted from all pages
   - Text files → Content read and decoded
   - Audio → Metadata stored (transcription pending)
5. **Metadata saved** - File info stored in PostgreSQL `elements` table
6. **Multimodal message** - Created with text + file content
7. **Agent processes** - AI can analyze images or reference document content

### Storage Structure

```
gs://eagleagent/
└── uploads/
    └── {user_email}/
        └── {session_id}/
            ├── image1.jpg
            ├── document.pdf
            └── notes.txt
```

### Database Schema

Files are tracked in the PostgreSQL `elements` table:

```sql
CREATE TABLE elements (
    id UUID PRIMARY KEY,
    threadId UUID,
    type TEXT,
    name TEXT,
    url TEXT,           -- Local File Storage signed URL
    objectKey TEXT,     -- Local File Storage path
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

1. Check file type is in `accept` list (.chainlit/config.toml)
2. Verify file size under 50MB
3. Check logs for processing errors
4. Ensure dependencies installed: `poetry install`

### Vision Not Working for Images

1. Verify Gemini model supports vision (gemini-2.0-flash-exp does)
2. Check image format is supported (PNG, JPEG, GIF, WebP)
3. Review logs for base64 encoding errors

## Security

### Access Control

- Files stored on **local disk** under `DATA_DIR/attachments/`
- Served via Chainlit's mounted `/files` route
- Non-root container user (uid 1000) in production

### File Validation

- File type restrictions in Chainlit config
- Size limits (50MB max)
- MIME type verification during processing

### Data Privacy

- Files stored in **Australian region** (data sovereignty)
- Automatic deletion after **30 days**
- User-specific folders (`uploads/{user_email}/`)

## Future Enhancements

- [ ] Audio transcription using Whisper API
- [ ] OCR for scanned documents
- [ ] Video thumbnail extraction
- [ ] Compressed archives (ZIP) support
- [ ] Real-time file upload progress
- [ ] User file management UI (view/delete uploaded files)
- [ ] Lifecycle policies in Local File Storage (automatic archival)

## Testing

Run the file attachment tests:

```bash
poetry run pytest tests/test_file_attachments.py -v
```

Tests cover:
- Image processing and base64 encoding
- PDF text extraction
- Text file reading
- Multimodal content creation
- Storage utilities (mocked Local File Storage)

## References

- [Local File Storage Documentation](https://cloud.google.com/storage/docs)
- [Chainlit File Upload](https://docs.chainlit.io/advanced-features/multi-modal)
- [LangChain Multimodal](https://python.langchain.com/docs/how_to/multimodal_inputs/)
- [Gemini Vision API](https://ai.google.dev/gemini-api/docs/vision)
