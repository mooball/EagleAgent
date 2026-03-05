# Google Cloud Run Deployment Guide

This guide covers deploying EagleAgent to Google Cloud Run with persistent SQLite storage using GCSFuse volume mounts and Firestore for checkpoints. Secrets are managed via GitHub and passed as environment variables during deployment.

**💡 Quick tip**: For daily development workflow, see [DEVELOPMENT_WORKFLOW.md](DEVELOPMENT_WORKFLOW.md)

## Architecture Overview

```
┌─────────────────────────────────────────────────┐
│           Google Cloud Run (Gen2)               │
│  ┌─────────────────────────────────────────┐   │
│  │  EagleAgent Container                   │   │
│  │  - Python 3.12 + uv                     │   │
│  │  - Node.js 20 (MCP servers & Browser)   │   │
│  │  - Playwright Chromium Dependencies     │   │
│  │  - Chainlit Web UI                      │   │
│  │  - Environment variables (from GitHub)  │   │
│  └─────────────────────────────────────────┘   │
│              │                │                  │
│         /data mount      /tmp/files             │
│              ▼              (ephemeral)          │
└──────────────┼──────────────────────────────────┘
               │
               ▼
    ┌──────────────────────┐
    │  GCS Bucket (GCSFuse) │
    │  ├─ database/         │
    │  │   └─ chainlit_*.db │
    │  ├─ uploads/          │
    │  └─ mcp_credentials/  │
    └──────────────────────┘
               │
    ┌──────────┴──────────────────┐
    ▼                             ▼
┌─────────┐              ┌──────────────┐
│Firestore│              │ OAuth 2.0    │
│(Checkpts)│              │ (Google Auth)│
└─────────┘              └──────────────┘
```

**Region**: All resources deployed to `australia-southeast1` (Sydney)

## Prerequisites

### 1. GCP Project Setup

1. Create or select a GCP project:
   ```bash
   gcloud projects create YOUR_PROJECT_ID --name="EagleAgent"
   # OR
   gcloud config set project YOUR_PROJECT_ID
   ```

2. Enable billing for the project (required for Cloud Run)

3. Enable required APIs:
   ```bash
   gcloud services enable \
     run.googleapis.com \
     storage.googleapis.com \
     firestore.googleapis.com \
     artifactregistry.googleapis.com \
     cloudbuild.googleapis.com
   ```

### 2. Install gcloud CLI

- **macOS**: `brew install --cask google-cloud-sdk`
- **Linux**: Follow [official instructions](https://cloud.google.com/sdk/docs/install)
- **Authenticate**: `gcloud auth login`

## Step-by-Step Deployment

### 1. GCS Bucket Setup

Create a bucket for persistent storage (database, uploads, MCP credentials):

```bash
# Create bucket in Sydney region
gcloud storage buckets create gs://eagleagent \
  --location=australia-southeast1 \
  --uniform-bucket-level-access

# Create directory structure with .keep files
echo -n "" | gcloud storage cp - gs://eagleagent/database/.keep
echo -n "" | gcloud storage cp - gs://eagleagent/uploads/.keep
echo -n "" | gcloud storage cp - gs://eagleagent/mcp_credentials/google_workspace/.keep
```

**Optional**: Set lifecycle policy to auto-delete old uploads:
```bash
cat > lifecycle.json <<EOF
{
  "lifecycle": {
    "rule": [
      {
        "action": {"type": "Delete"},
        "condition": {
          "age": 30,
          "matchesPrefix": ["uploads/"]
        }
      }
    ]
  }
}
EOF

gcloud storage buckets update gs://eagleagent-data --lifecycle-file=lifecycle.json
```

### 2. Firestore Setup

```bash
# Create Firestore database in Native mode
gcloud firestore databases create --region=australia-southeast1

# Firestore will be used for LangGraph checkpoints
```

### 3. Service Account Setup

Create a service account with minimal required permissions:

```bash
# Create service account
gcloud iam service-accounts create eagleagent-runner \
  --display-name="EagleAgent Cloud Run Service Account"

# Get project ID
PROJECT_ID=$(gcloud config get-value project)
SA_EMAIL="eagleagent-runner@${PROJECT_ID}.iam.gserviceaccount.com"

# Grant required roles
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/datastore.user"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectAdmin"
```

### 4. GitHub Secrets Configuration

All secrets are managed in GitHub and passed as environment variables during deployment. This is secure—Cloud Run encrypts environment variables at rest and in transit.

Configure these secrets in your GitHub repository (Settings → Secrets and variables → Actions):

**API Keys & Auth:**
- `GOOGLE_API_KEY` - Your Google AI API key
- `CHAINLIT_AUTH_SECRET` - Random secret for session auth (generate: `openssl rand -hex 32`)
- `OAUTH_GOOGLE_CLIENT_ID` - From Google Cloud Console OAuth 2.0 setup
- `OAUTH_GOOGLE_CLIENT_SECRET` - From Google Cloud Console OAuth 2.0 setup

**GCP Configuration:**
- `GCP_PROJECT_ID` - Your Google Cloud project ID
- `GCS_BUCKET_NAME` - Your GCS bucket name (e.g., `eagleagent`)
- `GCP_WORKLOAD_IDENTITY_PROVIDER` - Full resource name from Workload Identity setup
- `GCP_SERVICE_ACCOUNT` - Service account email (e.g., `eagleagent-runner@PROJECT.iam.gserviceaccount.com`)

**Note**: These secrets are automatically passed to Cloud Run during deployment and are not visible in logs.

### 5. OAuth Configuration

#### Initial Setup (Google Cloud Console)

1. Go to [Google Cloud Console](https://console.cloud.google.com) → APIs & Services → Credentials
2. Create OAuth 2.0 Client ID (Web application)
3. Add temporary redirect URI: `http://localhost:8000/auth/oauth/google/callback`
4. Save Client ID and Client Secret to GitHub Secrets (see step 4 above)

#### Post-Deployment Update

After first deployment, update redirect URIs:

```bash
# Get Cloud Run URL
SERVICE_URL=$(gcloud run services describe eagleagent \
  --region=australia-southeast1 \
  --format='value(status.url)')

echo "Add this to Google Console Authorized redirect URIs:"
echo "${SERVICE_URL}/auth/oauth/google/callback"
```

Then manually add in Google Console → Credentials → OAuth 2.0 Client → Authorized redirect URIs.

### 6. MCP Server OAuth Credentials (Google Workspace)

For the `google-workspace-mcp` server, perform initial OAuth flow locally:

```bash
# Install MCP server locally
npm install -g @modelcontextprotocol/server-google-workspace

# Run OAuth flow (opens browser)
npx @modelcontextprotocol/server-google-workspace auth

# Credentials saved to ~/.google_workspace_mcp/credentials/

# Upload to GCS
gcloud storage cp -r ~/.google_workspace_mcp/credentials/* \
  gs://eagleagent-data/mcp_credentials/google_workspace/
```

**Note**: Refresh tokens auto-renew indefinitely, so no runtime OAuth callbacks needed.

### 7. Manual Deployment

**Note**: Manual deployment is optional. The recommended approach is to use GitHub Actions which automatically deploys when you push to the `main` branch.

If you need to deploy manually, use the following commands. You'll need to set environment variables with your secret values:

```bash
# Set variables
PROJECT_ID=$(gcloud config get-value project)
REGION=australia-southeast1
SERVICE_NAME=eagleagent
GCS_BUCKET=eagleagent

# Set secrets (replace with actual values)
GOOGLE_API_KEY="your-google-api-key"
CHAINLIT_AUTH_SECRET="your-chainlit-auth-secret"
OAUTH_CLIENT_ID="your-oauth-client-id"
OAUTH_CLIENT_SECRET="your-oauth-client-secret"
OAUTH_ALLOWED_DOMAINS="yourdomain.com"

# Deploy
gcloud run deploy $SERVICE_NAME \
  --source . \
  --region $REGION \
  --platform managed \
  --execution-environment gen2 \
  --memory 2Gi \
  --cpu 2 \
  --min-instances 0 \
  --max-instances 10 \
  --timeout 300s \
  --service-account eagleagent-runner@${PROJECT_ID}.iam.gserviceaccount.com \
  --allow-unauthenticated \
  --add-volume name=gcs-data,type=cloud-storage,bucket=$GCS_BUCKET \
  --add-volume-mount volume=gcs-data,mount-path=/data \
  --set-env-vars "GOOGLE_API_KEY=${GOOGLE_API_KEY}" \
  --set-env-vars "CHAINLIT_AUTH_SECRET=${CHAINLIT_AUTH_SECRET}" \
  --set-env-vars "OAUTH_GOOGLE_CLIENT_ID=${OAUTH_CLIENT_ID}" \
  --set-env-vars "OAUTH_GOOGLE_CLIENT_SECRET=${OAUTH_CLIENT_SECRET}" \
  --set-env-vars "OAUTH_ALLOWED_DOMAINS=${OAUTH_ALLOWED_DOMAINS}" \
  --set-env-vars "GOOGLE_PROJECT_ID=${PROJECT_ID}" \
  --set-env-vars "GCP_BUCKET_NAME=${GCS_BUCKET}" \
  --set-env-vars "DATABASE_URL=sqlite+aiosqlite:////data/database/chainlit_datalayer.db" \
  --set-env-vars "TEMP_FILES_FOLDER=/tmp/files" \
  --set-env-vars "WORKSPACE_MCP_CREDENTIALS_DIR=/data/mcp_credentials/google_workspace"

# Get service URL
SERVICE_URL=$(gcloud run services describe $SERVICE_NAME \
  --region $REGION \
  --format='value(status.url)')

# Update CHAINLIT_URL env var
gcloud run services update $SERVICE_NAME \
  --region $REGION \
  --update-env-vars "CHAINLIT_URL=${SERVICE_URL}"

echo "🚀 Deployed to: $SERVICE_URL"
```

### 8. CI/CD Setup (GitHub Actions)

#### Workload Identity Federation

Set up keyless authentication for GitHub Actions:

```bash
PROJECT_ID=$(gcloud config get-value project)
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')

# Create Workload Identity Pool
gcloud iam workload-identity-pools create github-pool \
  --location=global \
  --display-name="GitHub Actions Pool"

# Create Workload Identity Provider
gcloud iam workload-identity-pools providers create-oidc github-provider \
  --location=global \
  --workload-identity-pool=github-pool \
  --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository_owner=='YOUR_GITHUB_USERNAME'"

# Get Workload Identity Provider resource name
WIP_NAME=$(gcloud iam workload-identity-pools providers describe github-provider \
  --location=global \
  --workload-identity-pool=github-pool \
  --format='value(name)')

# Grant service account permissions to GitHub
gcloud iam service-accounts add-iam-policy-binding \
  eagleagent-runner@${PROJECT_ID}.iam.gserviceaccount.com \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/${WIP_NAME}/attribute.repository/YOUR_GITHUB_USERNAME/EagleAgent"
```

#### GitHub Secrets

Add these secrets to your GitHub repository (Settings → Secrets and variables → Actions):

- `GCP_PROJECT_ID`: Your GCP project ID
- `GCP_WORKLOAD_IDENTITY_PROVIDER`: Full resource name from `$WIP_NAME`
- `GCP_SERVICE_ACCOUNT`: `eagleagent-runner@PROJECT_ID.iam.gserviceaccount.com`
- `GCP_SERVICE_ACCOUNT_EMAIL`: Same as above
- `GCS_BUCKET_NAME`: `eagleagent-data`
- `GOOGLE_API_KEY`: Your Gemini API key
- `CHAINLIT_AUTH_SECRET`: Random 64-char hex string
- `OAUTH_GOOGLE_CLIENT_ID`: OAuth client ID from Google Console
- `OAUTH_GOOGLE_CLIENT_SECRET`: OAuth client secret
- `OAUTH_ALLOWED_DOMAINS`: Comma-separated list (e.g., `yourdomain.com`)

### 9. Custom Domain (Optional)

Map a custom domain to your Cloud Run service:

```bash
# Add domain mapping
gcloud run domain-mappings create \
  --service eagleagent \
  --domain app.yourdomain.com \
  --region australia-southeast1

# Follow instructions to add DNS records
```

Update OAuth redirect URIs to include custom domain.

## Environment Variables Reference

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `GOOGLE_API_KEY` | Yes | Gemini API key | `AIza...` |
| `CHAINLIT_AUTH_SECRET` | Yes | Session secret (64+ chars hex) | `abc123...` |
| `OAUTH_GOOGLE_CLIENT_ID` | Yes | Google OAuth client ID | `123-abc.apps.googleusercontent.com` |
| `OAUTH_GOOGLE_CLIENT_SECRET` | Yes | Google OAuth client secret | `GOCSPX-...` |
| `OAUTH_ALLOWED_DOMAINS` | Yes | Allowed email domains | `yourdomain.com` |
| `CHAINLIT_URL` | Yes | Public URL of service | `https://eagleagent-xyz.run.app` |
| `GOOGLE_PROJECT_ID` | Yes | GCP project ID | `your-project-123` |
| `GCP_BUCKET_NAME` | Yes | GCS bucket name | `eagleagent-data` |
| `DATABASE_URL` | Yes | SQLite database path | `sqlite+aiosqlite:////data/database/chainlit_datalayer.db` |
| `TEMP_FILES_FOLDER` | Yes | Ephemeral upload folder | `/tmp/files` |
| `WORKSPACE_MCP_CREDENTIALS_DIR` | No | Google Workspace MCP creds | `/data/mcp_credentials/google_workspace` |
| `PORT` | No | HTTP port (set by Cloud Run) | `8080` |

## Monitoring and Troubleshooting

### View Logs

```bash
# Stream logs
gcloud run services logs tail eagleagent --region=australia-southeast1

# View recent logs in Cloud Console
# https://console.cloud.google.com/run > eagleagent > Logs
```

### Common Issues

#### 1. GCSFuse Mount Not Ready

**Symptom**: Startup script fails with "GCSFuse mount at /data is not writable"

**Solution**: 
- Ensure Gen2 execution environment is enabled
- Verify service account has `storage.objectAdmin` role
- Check bucket exists and is in same region

#### 2. OAuth Redirect URI Mismatch

**Symptom**: OAuth login fails with "redirect_uri_mismatch"

**Solution**:
- Get exact Cloud Run URL: `gcloud run services describe eagleagent --region=australia-southeast1 --format='value(status.url)'`
- Add `{URL}/auth/oauth/google/callback` to Google Console

#### 3. Database Locked Errors

**Symptom**: SQLite errors about locked database

**Solution**:
- Ensure `DATABASE_URL` uses `sqlite+aiosqlite://` (not `sqlite://`)
- Check only one instance writing at a time (normal with min-instances=0)

#### 4. MCP Server Not Found

**Symptom**: "npx: command not found" in logs

**Solution**:
- Verify Dockerfile installs Node.js 20.x
- Rebuild container: `docker build --no-cache -t eagleagent .`

### Performance Tuning

```bash
# Increase resources for high load
gcloud run services update eagleagent \
  --region=australia-southeast1 \
  --memory=4Gi \
  --cpu=4 \
  --max-instances=20

# Enable minimum instances to reduce cold start
gcloud run services update eagleagent \
  --region=australia-southeast1 \
  --min-instances=1
```

### Cost Estimation

**Typical monthly costs** (light usage, ~100 hours/month, 2Gi/2CPU):
- Cloud Run: ~$25
- GCS Storage (1GB): ~$0.02
- Firestore (1GB, 50k reads/writes): ~$0.50
- Secret Manager: ~$0.30
- **Total**: ~$26/month

**Zero to minimal usage** (scales to zero, min-instances=0):
- ~$5-10/month (mostly Firestore, GCS)

## Rollback

```bash
# List revisions
gcloud run revisions list --service=eagleagent --region=australia-southeast1

# Rollback to previous revision
gcloud run services update-traffic eagleagent \
  --region=australia-southeast1 \
  --to-revisions=eagleagent-00002-abc=100
```

## Cleanup

```bash
# Delete Cloud Run service
gcloud run services delete eagleagent --region=australia-southeast1

# Delete GCS bucket
gcloud storage rm -r gs://eagleagent-data

# Delete secrets
for SECRET in GOOGLE_API_KEY CHAINLIT_AUTH_SECRET OAUTH_GOOGLE_CLIENT_ID OAUTH_GOOGLE_CLIENT_SECRET OAUTH_ALLOWED_DOMAINS; do
  gcloud secrets delete $SECRET --quiet
done

# Delete service account
gcloud iam service-accounts delete eagleagent-runner@${PROJECT_ID}.iam.gserviceaccount.com
```

## Additional Resources

- [Cloud Run Documentation](https://cloud.google.com/run/docs)
- [GCSFuse Volume Mounts](https://cloud.google.com/run/docs/configuring/services/cloud-storage-volume-mounts)
- [Workload Identity Federation](https://cloud.google.com/iam/docs/workload-identity-federation)
- [Secret Manager Best Practices](https://cloud.google.com/secret-manager/docs/best-practices)
