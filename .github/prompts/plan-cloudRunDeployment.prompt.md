# Plan: Google Cloud Run Deployment with GCSFuse for SQLite

**TL;DR**: Containerize EagleAgent with Docker supporting both Python (uv) and Node.js for MCP servers, deploy to Google Cloud Run with Firestore/GCS backend using GCSFuse volume mounts for persistent SQLite database storage, manage secrets via GitHub Secrets, and automate deployment via GitHub Actions. Temp files use ephemeral `/tmp` (acceptable for file uploads), while SQLite database persists on GCS-mounted volume.

**Key architectural changes**: Mount GCS bucket as persistent volume for SQLite database at `/data`, configure dynamic port binding, implement Application Default Credentials for GCP services, and configure OAuth redirect URIs for Cloud Run domain. This provides true database persistence across restarts and deployments without Cloud SQL complexity. All resources deployed to **Australia (Sydney) region** (`australia-southeast1`).

## Steps

### 1. Create Multi-Stage Dockerfile

- Create `Dockerfile` with:
  - Base image: `python:3.12-slim` (matches `pyproject.toml` requirement)
  - Install Node.js 20.x LTS (for npx/MCP servers): `curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs`
  - Install `uv` package manager: `pip install --no-cache-dir uv`
  - Copy `pyproject.toml` and sync dependencies via `uv sync --frozen --no-dev`
  - Copy application code (`app.py`, `includes/`, `chainlit.md`, `config/*.example`)
  - Create `/tmp/files` directory for ephemeral uploads
  - Create `/data` directory as mount point for GCSFuse volume
  - Set working directory `/app`
  - Expose port 8080 (Cloud Run standard)
  - Use non-root user `appuser` for security
  - Set `PYTHONUNBUFFERED=1` for proper logging

### 2. Create .dockerignore File

- Create `.dockerignore` excluding:
  - `.venv/`, `__pycache__/`, `*.pyc`, `*.pyo` (local virtual env/cache)
  - `.env`, `service-account-key.json`, `*.json` (secrets - never in container)
  - `config/mcp_servers.yaml` (contains tokens, use env vars in Cloud Run)
  - `.chainlit/`, `.files/`, `temp_files/` (local runtime data)
  - `chainlit_datalayer.db` (will be on GCS mount)
  - `.git/`, `.github/` (Git metadata, CI configs) 
  - `tests/`, `scripts/` (development only)
  - `.DS_Store`, `*.md` (except `chainlit.md` - use negative pattern: `!chainlit.md`)
  - `README.md`, `*.prompt.md` (documentation)

### 3. Create Cloud Run Startup Script

- Create `start-cloudrun.sh` that:
  - Validates required environment variables (`GOOGLE_API_KEY`, `CHAINLIT_AUTH_SECRET`, `OAUTH_GOOGLE_CLIENT_ID`, `OAUTH_GOOGLE_CLIENT_SECRET`)
  - Creates `/tmp/files` directory for ephemeral temp files
  - Ensures `/data` directory exists (GCSFuse mount point)
  - Waits for GCSFuse mount to be ready (check `/data` is writable)
  - Executes: `uv run chainlit run app.py --host 0.0.0.0 --port ${PORT:-8080} --files-upload-folder /tmp/files`
  - Uses production mode (no `-w` watch flag)
  - Binds to `0.0.0.0:$PORT` (Cloud Run requirement)
- Make executable: `chmod +x start-cloudrun.sh`
- Set as Dockerfile `CMD ["./start-cloudrun.sh"]`

### 4. Update .gitignore for Docker Artifacts

- Ensure `.gitignore` includes:
  - Secrets: `service-account-key.json`, `.env`, `config/mcp_servers.yaml`
  - Local data: `.chainlit/`, `.files/`, `temp_files/`, `chainlit_datalayer.db`
- Verify NOT ignored: `Dockerfile`, `.dockerignore`, `start-cloudrun.sh` (needed for deployment)

### 5. Create GitHub Actions Deployment Workflow

- Create `.github/workflows/deploy-cloud-run.yml`:
  - **Trigger**: Push to `main` branch, manual workflow_dispatch
  - **Authentication**: Workload Identity Federation (no service account keys)
  - **Steps**:
    1. Checkout code
    2. Authenticate to GCP via `google-github-actions/auth@v2`
    3. Set up Cloud SDK
    4. Build Docker image: `docker build -t gcr.io/$PROJECT_ID/eagleagent:$GITHUB_SHA .`
    5. Push to Artifact Registry
    6. Deploy to Cloud Run with configuration:
       - Service name: `eagleagent`
       - Region: `australia-southeast1` (Sydney)
       - Memory: `2Gi`, CPU: `2`, Timeout: `300s`
       - Min instances: `0` (scale to zero), Max instances: `10`
       - **Volume mount**: GCS bucket as `/data` volume
       - Service account: `eagleagent-runner@PROJECT_ID.iam.gserviceaccount.com`
       - Allow unauthenticated: `--allow-unauthenticated` (for OAuth callbacks)
       - Set environment variables from GitHub Secrets
  - **GCSFuse configuration in deploy command**:
    ```bash
    --execution-environment gen2 \
    --add-volume name=gcs-data,type=cloud-storage,bucket=${GCS_BUCKET_NAME} \
    --add-volume-mount volume=gcs-data,mount-path=/data
    ```

### 6. Create Cloud Run Deployment Guide

- Create `CLOUD_RUN_DEPLOYMENT.md` documenting:
  - **Prerequisites**: 
    - GCP project setup with billing enabled
    - Enable APIs: Cloud Run Admin API, Artifact Registry API, Cloud Firestore API, Cloud Storage API
    - Install gcloud CLI locally
  - **GCS Bucket Setup**:
    - Create bucket: `gcloud storage buckets create gs://eagleagent-data --location=australia-southeast1`
    - Structure: `database/chainlit_datalayer.db`, `uploads/...`, `mcp_credentials/` (for MCP OAuth tokens)
    - Set lifecycle policy: 30-day retention for `uploads/` prefix (optional)
  - **Service Account Setup**:
    - Create SA: `gcloud iam service-accounts create eagleagent-runner`
    - Grant roles: `roles/datastore.user`, `roles/storage.objectAdmin`
    - For GCSFuse: Ensure SA has `storage.objects.get`, `storage.objects.create`, `storage.objects.delete` on bucket
  - **Secrets Configuration**:
    - All secrets managed in GitHub and passed as environment variables
    - Configure in GitHub repository (Settings → Secrets and variables → Actions):
      - `GOOGLE_API_KEY`, `CHAINLIT_AUTH_SECRET`, `OAUTH_GOOGLE_CLIENT_ID`, `OAUTH_GOOGLE_CLIENT_SECRET`
      - `GCP_PROJECT_ID`, `GCS_BUCKET_NAME`, `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT`
  - **OAuth Configuration**:
    - Get Cloud Run URL after first deployment: `gcloud run services describe eagleagent --region=australia-southeast1 --format='value(status.url)'`
    - Add to Google Console Authorized redirect URIs: `https://YOUR-SERVICE.run.app/auth/oauth/google/callback`
  - **MCP Server OAuth Credentials** (for google-workspace-mcp):
    - Perform initial OAuth flow locally or via temporary Cloud Shell session
    - Credentials stored at `~/.google_workspace_mcp/credentials/agent@yourdomain.com.json`
    - Upload to GCS: `gcloud storage cp ~/.google_workspace_mcp/credentials/* gs://eagleagent-data/mcp_credentials/google_workspace/`
    - In Cloud Run, MCP server reads from `/data/mcp_credentials/google_workspace/` (GCSFuse mount)
    - Set `WORKSPACE_MCP_CREDENTIALS_DIR=/data/mcp_credentials/google_workspace` in Cloud Run env vars
    - **No runtime OAuth callbacks needed** - refresh tokens auto-renew indefinitely
  - **Environment Variables for Cloud Run**:
    ```bash
    GOOGLE_API_KEY=<from-github-secrets>
    CHAINLIT_AUTH_SECRET=<from-github-secrets>
    OAUTH_GOOGLE_CLIENT_ID=<from-github-secrets>
    OAUTH_GOOGLE_CLIENT_SECRET=<from-github-secrets>
    OAUTH_ALLOWED_DOMAINS=yourdomain.com
    CHAINLIT_URL=https://YOUR-SERVICE.run.app
    DATABASE_URL=sqlite+aiosqlite:////data/database/chainlit_datalayer.db
    GCP_BUCKET_NAME=eagleagent-data
    TEMP_FILES_FOLDER=/tmp/files
    GOOGLE_PROJECT_ID=your-project-id
    ```
  - **Manual Deployment Command**:
    ```bash
    gcloud run deploy eagleagent \
      --source . \
      --region australia-southeast1 \
      --execution-environment gen2 \
      --memory 2Gi \
      --cpu 2 \
      --min-instances 0 \
      --max-instances 10 \
      --timeout 300s \
      --service-account eagleagent-runner@PROJECT.iam.gserviceaccount.com \
      --allow-unauthenticated \
      --add-volume name=gcs-data,type=cloud-storage,bucket=eagleagent-data \
      --add-volume-mount volume=gcs-data,mount-path=/data \
      --set-env-vars CHAINLIT_URL=https://eagleagent-HASH.run.app,DATABASE_URL=sqlite+aiosqlite:////data/database/chainlit_datalayer.db,WORKSPACE_MCP_CREDENTIALS_DIR=/data/mcp_credentials/google_workspace
    ```
  - **CI/CD Setup**: Workload Identity Federation configuration for GitHub Actions
  - **Custom Domain**: Domain mapping instructions for vanity URLs
  - **Monitoring**: Cloud Logging queries for errors, Cloud Trace for performance

### 7. Update storage_utils.py for Cloud Run ADC

- Modify `includes/storage_utils.py` credential initialization:
  - Add conditional: if `GOOGLE_APPLICATION_CREDENTIALS` env var exists, load from file (local dev); otherwise use Application Default Credentials (Cloud Run)
  - Pattern:
    ```python
    import os
    import google.auth
    from google.oauth2 import service_account
    
    def get_gcp_credentials():
        if creds_file := os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            return service_account.Credentials.from_service_account_file(creds_file)
        else:
            credentials, project = google.auth.default()
            return credentials
    ```
  - Update `storage_client` and `drive_client` initialization to use `get_gcp_credentials()`

### 8. Create Environment Template for Cloud Run

- Create `.env.cloudrun.example` with Cloud Run-specific configuration:
  ```bash
  # Google Cloud Run Configuration
  # These should be set via GitHub Secrets and passed as Cloud Run env vars
  
  # LLM
  GOOGLE_API_KEY=<from-github-secrets>
  
  # GCP Project
  GOOGLE_PROJECT_ID=your-project-id
  
  # Authentication
  CHAINLIT_AUTH_SECRET=<from-github-secrets>
  OAUTH_GOOGLE_CLIENT_ID=<from-github-secrets>
  OAUTH_GOOGLE_CLIENT_SECRET=<from-github-secrets>
  OAUTH_ALLOWED_DOMAINS=yourdomain.com
  
  # Deployment
  CHAINLIT_URL=https://eagleagent-xyz.run.app
  PORT=8080
  
  # Database (GCSFuse mounted volume)
  DATABASE_URL=sqlite+aiosqlite:////data/database/chainlit_datalayer.db
  
  # Storage
  GCP_BUCKET_NAME=eagleagent-data
  TEMP_FILES_FOLDER=/tmp/files
  
  # DO NOT SET (uses Application Default Credentials)
  # GOOGLE_APPLICATION_CREDENTIALS=<not-needed>
  ```
- Document in `CLOUD_RUN_DEPLOYMENT.md`

### 9. Create Cloud Build Configuration (Alternative)

- Create `cloudbuild.yaml` for teams preferring GCP-native CI/CD:
  ```yaml
  steps:
    - name: 'gcr.io/cloud-builders/docker'
      args: ['build', '-t', 'gcr.io/$PROJECT_ID/eagleagent:$SHORT_SHA', '.']
    - name: 'gcr.io/cloud-builders/docker'
      args: ['push', 'gcr.io/$PROJECT_ID/eagleagent:$SHORT_SHA']
    - name: 'gcr.io/google.com/cloudsdktool/cloud-sdk'
      entrypoint: gcloud
      args:
        - 'run'
        - 'deploy'
        - 'eagleagent'
        - '--image=gcr.io/$PROJECT_ID/eagleagent:$SHORT_SHA'
        - '--region=australia-southeast1'
        - '--platform=managed'
        - '--execution-environment=gen2'
        - '--add-volume=name=gcs-data,type=cloud-storage,bucket=${_GCS_BUCKET}'
        - '--add-volume-mount=volume=gcs-data,mount-path=/data'
  substitutions:
    _GCS_BUCKET: 'eagleagent-data'
    _REGION: 'australia-southeast1'
  ```
- Optional alternative to GitHub Actions

### 10. Update Documentation

- Add **Deployment** section to `README.md`:
  - Link to `CLOUD_RUN_DEPLOYMENT.md`
  - Architecture diagram: Cloud Run ↔ Firestore + GCS (GCSFuse) + GitHub Secrets
  - Development vs Production configuration table
  - Quick start for local development vs cloud deployment
- Update `GOOGLE_OAUTH_SETUP.md`:
  - Add Cloud Run redirect URI configuration section
  - Note about wildcard not supported (must use exact Cloud Run URL)
  - Instructions for updating redirect URIs after deployment

## Verification

- **Local Docker Build**: `docker build -t eagleagent:local .` → Succeeds, image size <500MB
- **Local Docker Run with Volume**: `docker run -v /tmp/test-data:/data -p 8080:8080 --env-file .env eagleagent:local` → App starts, creates SQLite at `/data/database/`
- **Cloud Run Deployment**: Deploy to Cloud Run → Service shows Running, health check passes
- **GCSFuse Mount**: SSH to Cloud Run (if possible) or check logs → `/data` mounted, writable
- **SQLite Persistence**: Send messages → Restart service → Chat history persists in sidebar
- **GCS Bucket Verification**: `gcloud storage ls gs://eagleagent-data/database/` → Shows `chainlit_datalayer.db`
- **OAuth Flow**: Access Cloud Run URL → Complete login → OAuth callback works, session persists
- **File Uploads**: Upload PDF → Verify stored in `gs://eagleagent-data/uploads/`
- **MCP Servers**: Configure STDIO MCP server → Verify npx works in container, tools available
- **CI/CD Pipeline**: Push to `main` → GitHub Actions builds and deploys, service updates

## Decisions

- **Chose GCSFuse over Cloud SQL**: Simpler setup, lower cost ($0 vs $30-200/month), adequate performance for SQLite chat history; uses existing GCS bucket
- **Chose GCSFuse over Filestore**: Filestore overkill for small SQLite database (~$200/month minimum vs $0 incremental for GCS)
- **Chose `/data` mount over `/tmp`**: Provides persistence across restarts, keeps temp files ephemeral as intended
- **Chose Gen2 execution environment**: Required for volume mounts (Gen1 doesn't support GCSFuse)
- **Chose multi-container support**: Single Dockerfile with Python + Node.js vs separate MCP server deployments
- **Chose Workload Identity over service account keys**: More secure, no key rotation needed
- **Chose allow-unauthenticated**: Required for OAuth callbacks to work; app has own authentication layer
- **Chose SQLite at `/data/database/` path**: Separates database from file uploads in GCS bucket structure
- **Chose ephemeral `/tmp` for file uploads**: Files already persisted to GCS via upload workflow, don't need double persistence
- **Chose Sydney region (australia-southeast1)**: Lower latency for Australia-based users, data residency compliance
- **Chose credential pre-population for MCP OAuth**: Simpler than runtime OAuth callbacks; credentials stored on GCSFuse mount, auto-refresh handles token renewal
