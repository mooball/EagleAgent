# File Attachments Implementation

This document describes the file attachment system for EagleAgent, which allows users to upload images, PDFs, text files, and audio files during conversations.

## Overview

The system stores files in Google Cloud Storage (GCS) with a 30-day retention policy and processes them for use by the AI agent:

- **Images**: Analyzed using Gemini's vision capabilities
- **PDFs**: Text extracted and added to conversation context
- **Text files**: Content read and included in messages
- **Audio**: Metadata stored (transcription to be implemented)

## Architecture

```
User Upload → Chainlit UI → app.py → GCS Upload → Document Processing → Agent Context
                                   ↓
                              SQLite Metadata
```

## Setup Instructions

### 1. Create GCS Bucket in Sydney Region

```bash
# Create bucket in australia-southeast1 (Sydney)
gcloud storage buckets create gs://eagleagent --location=australia-southeast1

# Verify bucket created
gcloud storage buckets describe gs://eagleagent
```

**Why Sydney?**
- Lowest latency for Australian users (Eagle Exports + Mooball)
- Data sovereignty - files stay in Australia
- Lower egress costs for local access

### 2. Grant Service Account Permissions

```bash
# Grant storage admin permissions to the service account
gcloud storage buckets add-iam-policy-binding gs://eagleagent \
  --member=serviceAccount:eagleagent-svc-account@mooballai.iam.gserviceaccount.com \
  --role=roles/storage.objectAdmin
```

### 3. Configure Environment

Ensure your `.env` file has:

```bash
GCP_BUCKET_NAME=eagleagent
GOOGLE_APPLICATION_CREDENTIALS=./service-account-key.json
GOOGLE_PROJECT_ID=mooballai
```

### 4. Install Dependencies

```bash
poetry install
```

This installs:
- `google-cloud-storage` - GCS client
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
3. **GCS upload** - file stored in `gs://eagleagent/uploads/{user_id}/{session_id}/{filename}`
4. **Document processing**:
   - Images → Base64 encoded for Gemini vision
   - PDFs → Text extracted from all pages
   - Text files → Content read and decoded
   - Audio → Metadata stored (transcription pending)
5. **Metadata saved** - File info stored in SQLite `elements` table
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

Files are tracked in the SQLite `elements` table:

```sql
CREATE TABLE elements (
    id UUID PRIMARY KEY,
    threadId UUID,
    type TEXT,
    name TEXT,
    url TEXT,           -- GCS signed URL
    objectKey TEXT,     -- GCS path
    mime TEXT,
    createdAt TIMESTAMP
);
```

## Processing Modules

### storage_utils.py

Functions for GCS operations:

- `upload_file_to_gcs(file_path, bucket_name, object_key)` → Returns GCS URL
- `download_file_from_gcs(bucket_name, object_key)` → Returns file bytes
- `delete_file_from_gcs(bucket_name, object_key)` → Removes file
- `generate_object_key(user_id, session_id, filename)` → Creates unique path

### document_processing.py

Document processing utilities:

- `process_image(file_bytes, mime_type)` → Base64 for Gemini vision
- `extract_pdf_text(file_bytes)` → Text from all PDF pages
- `extract_text_from_file(file_bytes, mime_type)` → Generic text extraction
- `process_audio(file_path, mime_type)` → Metadata (transcription TBD)
- `create_multimodal_content(text, images)` → LangChain message format

## File Retention & Cleanup

Files are automatically deleted after 30 days.

### Manual Cleanup

```bash
# Dry run - see what would be deleted
./scripts/cleanup_old_files.py --dry-run

# Delete files older than 30 days
./scripts/cleanup_old_files.py

# Custom retention period (e.g., 7 days)
./scripts/cleanup_old_files.py --days 7
```

### Automated Cleanup (Production)

Schedule the cleanup script via cron:

```bash
# Edit crontab
crontab -e

# Run daily at 2 AM
0 2 * * * cd /path/to/EagleAgent && poetry run python scripts/cleanup_old_files.py
```

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

### Check GCS Bucket Contents

```bash
# List all files
gsutil ls -r gs://eagleagent

# Check bucket location
gcloud storage buckets describe gs://eagleagent

# View bucket size
gsutil du -sh gs://eagleagent
```

### View Upload Logs

Logs are written to console during file uploads:

```
INFO - includes.storage_utils - Uploaded file to GCS: uploads/user@example.com/session-123/image.jpg
INFO - includes.document_processing - Extracted text from PDF: 1234 characters
```

### Database Queries

```bash
# View all uploaded files
sqlite3 chainlit_datalayer.db "SELECT name, mime, createdAt FROM elements ORDER BY createdAt DESC LIMIT 10;"

# Count files by type
sqlite3 chainlit_datalayer.db "SELECT mime, COUNT(*) FROM elements GROUP BY mime;"
```

## Troubleshooting

### "Bucket does not exist" Error

```bash
# Verify bucket exists
gcloud storage buckets describe gs://eagleagent

# If not, create it
gcloud storage buckets create gs://eagleagent --location=australia-southeast1
```

### "Permission denied" Error

```bash
# Check service account has permissions
gcloud storage buckets get-iam-policy gs://eagleagent

# Re-grant permissions if needed
gcloud storage buckets add-iam-policy-binding gs://eagleagent \
  --member=serviceAccount:eagleagent-svc-account@mooballai.iam.gserviceaccount.com \
  --role=roles/storage.objectAdmin
```

### Files Not Processing

1. Check file type is in `accept` list (.chainlit/config.toml)
2. Verify file size under 50MB
3. Check logs for processing errors
4. Ensure dependencies installed: `poetry install`

### Vision Not Working for Images

1. Verify Gemini model supports vision (gemini-2.0-flash-exp does)
2. Check image format is supported (PNG, JPEG, GIF, WebP)
3. Review logs for base64 encoding errors

## Cost Considerations

### GCS Pricing (Sydney Region)

- **Storage**: ~$0.023 per GB/month (Standard class)
- **Operations**: Minimal (few uploads/downloads per day)
- **Network**: Free for australia-southeast1 → australia-southeast1

**Example**: 1000 files × 1MB each = 1GB = ~$0.023/month

### Optimization Tips

1. **30-day retention** - Reduces long-term storage costs
2. **Sydney region** - Lower egress costs for Australian users
3. **Standard storage class** - Best for frequent access (vs Nearline/Coldline)

## Security

### Access Control

- Files stored in **private bucket** (not publicly accessible)
- Service account has **least-privilege permissions** (objectAdmin only)
- Signed URLs used for **temporary access** (if needed)

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
- [ ] Lifecycle policies in GCS (automatic archival)

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
- Storage utilities (mocked GCS)

## References

- [Google Cloud Storage Documentation](https://cloud.google.com/storage/docs)
- [Chainlit File Upload](https://docs.chainlit.io/advanced-features/multi-modal)
- [LangChain Multimodal](https://python.langchain.com/docs/how_to/multimodal_inputs/)
- [Gemini Vision API](https://ai.google.dev/gemini-api/docs/vision)
