# Plan: Remove Secret Storage Duplication

**TL;DR**: Your deployment uses GitHub Actions which passes secrets as Cloud Run environment variables—this is secure and industry-standard. However, your repository includes unused Cloud Build config requiring Google Secret Manager, and documentation instructs setting up both. We'll remove the unused Cloud Build file, delete all Google Secret Manager secrets, and use GitHub Secrets as the single source of truth.

## Security Analysis

**Is it safe to pass secrets as environment variables?** ✅ **YES**

- Cloud Run environment variables are **encrypted at rest** and **encrypted in transit**
- Secrets are **NOT printed in deployment logs** or build logs
- Only authorized users with IAM permissions can view env vars in Cloud Run console
- This is the **standard, Google-recommended pattern** for Cloud Run deployments
- GitHub Actions **masks secrets in workflow logs** (displays `***` instead of actual values)

**Conclusion**: Storing secrets in GitHub and passing them as Cloud Run environment variables is secure and eliminates duplication.

## Current Situation

You have **two deployment methods** in your repo:

1. **GitHub Actions** (`.github/workflows/deploy-cloud-run.yml`) - **ACTIVE**
   - Triggers automatically on `push` to `main`
   - Passes GitHub Secrets as environment variables
   - Does NOT use Secret Manager
   - Already working securely

2. **Cloud Build** (`cloudbuild.yaml`) - **UNUSED**
   - Alternative manual deployment method
   - Pulls secrets from Google Secret Manager
   - Only runs if manually triggered with `gcloud builds submit`
   - Creates unnecessary duplication

**Decision**: Remove Cloud Build entirely, delete all Google Secret Manager secrets, keep only GitHub Secrets.

## Steps

### 1. Remove unused Cloud Build configuration
- Delete `cloudbuild.yaml` (150-line file that references Secret Manager)
- This eliminates the only code that actually uses Secret Manager

### 2. Update CLOUD_RUN_DEPLOYMENT.md to remove Secret Manager references

**Section "Prerequisites → 1. GCP Project Setup → 3. Enable required APIs":**
- Remove `secretmanager.googleapis.com` from the API list
- Keep only: `run.googleapis.com`, `storage.googleapis.com`, `firestore.googleapis.com`, `artifactregistry.googleapis.com`, `cloudbuild.googleapis.com`

**Section "3. Service Account Setup":**
- Remove the grant for `--role="roles/secretmanager.secretAccessor"`
- Keep only:
  - `roles/datastore.user` (for Firestore checkpoints)
  - `roles/storage.objectAdmin` (for GCS bucket)

**Section "4. Secrets Configuration":**
- Replace entire section with:
  ```markdown
  ### 4. GitHub Secrets Configuration
  
  All secrets are managed in GitHub and passed as environment variables during deployment.
  This is secure—Cloud Run encrypts environment variables at rest and in transit.
  
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
  ```

**Section "5. OAuth Configuration":**
- Change "Save Client ID and Client Secret to Secret Manager (done in step 4)" 
- To: "Save Client ID and Client Secret to GitHub Secrets (done in step 4)"

**Architecture diagram** (around lines 11-40):
- Remove the "│Secret Manager│" box and connections
- Update to show: `GitHub Secrets → GitHub Actions → Cloud Run env vars → App`

### 3. Verify GitHub Secrets are configured

Confirm these secrets exist in your GitHub repository (Settings → Secrets and variables → Actions):

**Required secrets:**
- `GOOGLE_API_KEY`
- `CHAINLIT_AUTH_SECRET`
- `OAUTH_GOOGLE_CLIENT_ID`
- `OAUTH_GOOGLE_CLIENT_SECRET`
- `GCP_PROJECT_ID`
- `GCS_BUCKET_NAME`
- `GCP_WORKLOAD_IDENTITY_PROVIDER`
- `GCP_SERVICE_ACCOUNT`

### 4. Delete Google Secret Manager resources

Clean up Secret Manager completely:

```bash
# List what exists
gcloud secrets list

# Delete all secrets (if they exist)
gcloud secrets delete GOOGLE_API_KEY --quiet
gcloud secrets delete CHAINLIT_AUTH_SECRET --quiet
gcloud secrets delete OAUTH_GOOGLE_CLIENT_ID --quiet
gcloud secrets delete OAUTH_GOOGLE_CLIENT_SECRET --quiet
gcloud secrets delete OAUTH_ALLOWED_DOMAINS --quiet

# Remove secretAccessor role from service account
PROJECT_ID=$(gcloud config get-value project)
SA_EMAIL="eagleagent-runner@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud projects remove-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor"

# Disable Secret Manager API (if nothing else uses it)
gcloud services disable secretmanager.googleapis.com
```

### 5. Update related documentation files

Check and update references in:
- `README.md` - Update architecture diagram if it shows Secret Manager
- `.github/prompts/plan-cloudRunDeployment.prompt.md` - Remove Secret Manager setup steps

## Verification

1. **Commit and deploy**: 
   ```bash
   git add cloudbuild.yaml CLOUD_RUN_DEPLOYMENT.md
   git commit -m "Remove unused Cloud Build and Secret Manager dependency"
   git push origin main
   ```

2. **Monitor GitHub Actions workflow** - verify it completes successfully

3. **Test deployed app**:
   - Visit Cloud Run URL
   - Verify OAuth login works (uses `OAUTH_GOOGLE_CLIENT_ID` / `SECRET`)
   - Test AI chat functionality (uses `GOOGLE_API_KEY`)
   - Check file uploads work (uses `GCP_BUCKET_NAME`)

4. **Verify no Secret Manager calls**:
   ```bash
   # Check Cloud Run logs for errors
   gcloud run services logs read eagleagent --region=australia-southeast1 --limit=50
   ```

## Decisions

**Why remove Google Secret Manager entirely?**
- ✅ **Secure**: Cloud Run env vars are encrypted and not exposed in logs
- ✅ **Simpler**: One less GCP service to configure and maintain
- ✅ **Standard practice**: Industry-standard pattern for GitHub → Cloud Run deployments
- ✅ **No duplication**: Secrets stored in one place only (GitHub)
- ✅ **Easier rotation**: Update secrets in GitHub UI, redeploy automatically
- ✅ **Already working**: Current deployment already uses this pattern

**Why GitHub Secrets over Secret Manager?**
- Integrated with your Git workflow and CI/CD
- Masked in GitHub Actions logs (displays `***`)
- Standard for GitHub-hosted projects
- Simpler for contributors to understand
- No additional GCP service quota or billing

**Single source of truth**: GitHub Secrets → GitHub Actions → Cloud Run environment variables → Application (`os.getenv()`)

**Security guarantee**: No secrets appear in plaintext in logs, build outputs, or Cloud Run console logs. Only authorized IAM users can view environment variables in the Cloud Run service configuration.
