# Local Docker Testing Guide

This guide covers testing EagleAgent locally using Docker before deploying to Cloud Run. This allows you to validate the containerized application, debug issues, and verify the deployment configuration in a local environment.

**💡 Quick tip**: For daily development workflow commands, see [DEVELOPMENT_WORKFLOW.md](DEVELOPMENT_WORKFLOW.md)

## Why Test Locally with Docker?

- ✅ **Validate container build** before pushing to Cloud Run
- ✅ **Test volume mounts** to ensure database persistence works
- ✅ **Debug startup issues** in a controlled environment
- ✅ **Verify environment variables** and configuration
- ✅ **Check resource usage** and performance
- ✅ **Iterate faster** without cloud deployment delays

## Prerequisites

### 1. Install Docker Desktop

**macOS**:
```bash
brew install --cask docker
```

Or download from [Docker Desktop for Mac](https://www.docker.com/products/docker-desktop/)

**Windows/Linux**: Follow [official Docker installation guide](https://docs.docker.com/get-docker/)

### 2. Start Firestore Emulator (Required)

The application requires Firestore for LangGraph checkpoints. Start the local emulator:

```bash
# Install Firestore emulator (one-time)
gcloud components install cloud-firestore-emulator

# Start emulator (runs in background)
export FIRESTORE_EMULATOR_HOST=localhost:8686
gcloud emulators firestore start --host-port=localhost:8686 > /tmp/firestore-emulator.log 2>&1 &

# Verify it's running
ps aux | grep firestore | grep -v grep
```

**Keep this running** while testing Docker containers.

### 3. Create Docker Environment File

Create `.env.docker` for local Docker testing (already gitignored):

**Option 1: Use the template**
```bash
# Copy the example file
cp .env.docker.example .env.docker

# Edit with your actual values
# Fill in: GOOGLE_API_KEY, CHAINLIT_AUTH_SECRET, OAUTH credentials
nano .env.docker  # or use your preferred editor
```

**Option 2: Generate from existing .env**
```bash
# Extract secrets from your .env file
grep -E '^(GOOGLE_API_KEY|CHAINLIT_AUTH_SECRET|OAUTH_GOOGLE_CLIENT_ID|OAUTH_GOOGLE_CLIENT_SECRET|OAUTH_ALLOWED_DOMAINS)=' .env > .env.docker

# Add Docker-specific configuration
cat >> .env.docker << 'EOF'

# GCP Project (required for Firestore)
GOOGLE_PROJECT_ID=test-project

# Deployment
CHAINLIT_URL=http://localhost:8080
PORT=8080

# Database (using Docker volume)
DATABASE_URL=sqlite+aiosqlite:////data/database/chainlit_datalayer.db

# Storage  
GCP_BUCKET_NAME=eagleagent
TEMP_FILES_FOLDER=/tmp/files

# Firestore Emulator (access host's emulator from container)
# Note: host.docker.internal works on Mac/Windows
# On Linux, use: 172.17.0.1:8686
FIRESTORE_EMULATOR_HOST=host.docker.internal:8686
EOF
```

**Important**: `.env.docker` should NOT contain `GOOGLE_APPLICATION_CREDENTIALS` - the container uses Application Default Credentials.

## Building the Docker Image

### Standard Build

```bash
# Build the image
docker build -t eagleagent:local .
```

This typically takes 15-30 seconds after the first build (thanks to layer caching).

### Force Rebuild (when dependencies change)

```bash
# Clear cache and rebuild
docker build --no-cache -t eagleagent:local .
```

### Check Image Size

```bash
docker images eagleagent:local --format "{{.Repository}}:{{.Tag}} - {{.Size}}"
# Expected: ~1.27GB (includes Python 3.12, Node.js 20, dependencies)
```

## Running the Container

### Quick Start (Detached Mode)

```bash
# Create persistent volume directory
mkdir -p /tmp/eagleagent-data

# Run container in background
docker run -d \
  --name eagleagent \
  -v /tmp/eagleagent-data:/data \
  -p 8080:8080 \
  --env-file .env.docker \
  eagleagent:local

# View logs
docker logs -f eagleagent
```

### Interactive Mode (for debugging)

```bash
# Run with logs visible
docker run --rm \
  -v /tmp/eagleagent-data:/data \
  -p 8080:8080 \
  --env-file .env.docker \
  eagleagent:local
```

Press `Ctrl+C` to stop.

### Development Mode (with live code updates)

```bash
# Mount source code as volume for live updates
docker run --rm \
  -v /tmp/eagleagent-data:/data \
  -v $(pwd)/app.py:/app/app.py \
  -v $(pwd)/includes:/app/includes \
  -p 8080:8080 \
  --env-file .env.docker \
  eagleagent:local
```

**Note**: You'll need to restart the container after code changes since Chainlit doesn't auto-reload in the container.

## Accessing the Application

### Web Interface

Open your browser to:
```
http://localhost:8080
```

### Health Check

```bash
# Check if service is responding
curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/
# Expected: 200
```

### Test OAuth Login

1. Navigate to `http://localhost:8080`
2. Click "Sign in with Google"
3. Complete OAuth flow
4. Verify you're redirected back to the chat interface

**Important**: Your Google OAuth credentials must have `http://localhost:8080/auth/oauth/google/callback` as an authorized redirect URI.

## Viewing Logs

### Follow Logs (live)

```bash
docker logs -f eagleagent
```

### View Recent Logs

```bash
# Last 50 lines
docker logs --tail 50 eagleagent

# Last 100 lines with timestamps
docker logs --tail 100 -t eagleagent
```

### Search Logs

```bash
# Find errors
docker logs eagleagent 2>&1 | grep -i error

# Find specific message
docker logs eagleagent 2>&1 | grep "Starting Chainlit"
```

## Verifying Functionality

### 1. Check Startup Sequence

Expected log output:
```
🚀 Starting EagleAgent on Cloud Run...
✅ All required environment variables are set
✅ Created /tmp/files directory
✅ Ensured /data directory exists
⏳ Waiting for GCSFuse mount at /data...
✅ GCSFuse mount at /data is ready and writable
✅ Created /data/database directory for SQLite
🌐 Starting Chainlit on port 8080
Your app is available at http://0.0.0.0:8080
```

### 2. Verify Volume Mount

```bash
# Check database directory was created
ls -lh /tmp/eagleagent-data/database/

# After using the app, verify database file exists
ls -lh /tmp/eagleagent-data/database/*.db
```

### 3. Test Persistence

```bash
# Send some messages in the chat
# Then restart the container
docker restart eagleagent

# Chat history should persist in the sidebar
```

### 4. Check Resource Usage

```bash
# View container stats
docker stats eagleagent

# Expected:
# - CPU: 0-10% idle, 50-100% during LLM calls
# - Memory: 200-500MB typical
```

### 5. Inspect Container

```bash
# Connect to running container
docker exec -it eagleagent bash

# Inside container:
ls -la /app/              # Application code
ls -la /data/             # Persistent volume
ls -la /tmp/files/        # Ephemeral uploads
env | grep -E 'GOOGLE|CHAINLIT|OAUTH'  # Environment variables
python --version          # Python 3.12.x
node --version            # Node v20.20.x
uv --version              # uv package manager
exit
```

## Troubleshooting

### Issue: Container exits immediately

**Check logs**:
```bash
docker logs eagleagent
```

**Common causes**:
- Missing required environment variables
- Invalid `.env.docker` file syntax
- Port 8080 already in use

**Solution**:
```bash
# Check for conflicting processes
lsof -i :8080

# Kill conflicting process
kill <PID>

# Or use different port
docker run -p 8081:8080 ...
```

### Issue: "File service-account-key.json was not found"

**Cause**: `.env.docker` contains `GOOGLE_APPLICATION_CREDENTIALS=./service-account-key.json`

**Solution**: Remove this line from `.env.docker`:
```bash
# Remove GOOGLE_APPLICATION_CREDENTIALS line
grep -v GOOGLE_APPLICATION_CREDENTIALS .env.docker > .env.docker.tmp
mv .env.docker.tmp .env.docker
```

### Issue: "Cannot connect to Firestore"

**Check emulator**:
```bash
ps aux | grep firestore | grep -v grep
```

**Restart emulator**:
```bash
pkill -f firestore
export FIRESTORE_EMULATOR_HOST=localhost:8686
gcloud emulators firestore start --host-port=localhost:8686 > /tmp/firestore-emulator.log 2>&1 &
```

**Linux users**: Change `FIRESTORE_EMULATOR_HOST` in `.env.docker`:
```bash
# Use Docker bridge IP on Linux
FIRESTORE_EMULATOR_HOST=172.17.0.1:8686
```

### Issue: Volume mount not working

**Verify mount**:
```bash
docker inspect eagleagent | grep -A 10 Mounts
```

**Expected output**:
```json
"Mounts": [
    {
        "Type": "bind",
        "Source": "/tmp/eagleagent-data",
        "Destination": "/data",
        "Mode": "",
        "RW": true
    }
]
```

### Issue: OAuth redirect URI mismatch

**Cause**: Google Console doesn't have `http://localhost:8080/auth/oauth/google/callback`

**Solution**: Add redirect URI in [Google Cloud Console](https://console.cloud.google.com/apis/credentials)

### Issue: "Permission denied" errors

**Check file permissions**:
```bash
ls -la /tmp/eagleagent-data/
```

**Fix permissions**:
```bash
# Container runs as UID 1000 (appuser)
sudo chown -R 1000:1000 /tmp/eagleagent-data/
```

Or use your user's UID:
```bash
chown -R $(id -u):$(id -g) /tmp/eagleagent-data/
```

## Stopping and Cleanup

### Stop Container

```bash
# Graceful stop
docker stop eagleagent

# Force stop
docker kill eagleagent
```

### Remove Container

```bash
# Remove stopped container
docker rm eagleagent

# Stop and remove in one command
docker rm -f eagleagent
```

### Remove Image

```bash
# Remove local image
docker rmi eagleagent:local

# Remove all unused images
docker image prune -a
```

### Clean Volume Data

```bash
# Remove persistent data
rm -rf /tmp/eagleagent-data/

# Or just clean database
rm -rf /tmp/eagleagent-data/database/*.db
```

### Stop Firestore Emulator

```bash
pkill -f firestore
```

## Advanced Usage

### Using Custom Volume Locations

```bash
# Use a specific directory for persistence
mkdir -p ~/eagleagent-data

docker run -d \
  --name eagleagent \
  -v ~/eagleagent-data:/data \
  -p 8080:8080 \
  --env-file .env.docker \
  eagleagent:local
```

### Override Environment Variables

```bash
# Override specific variables
docker run -d \
  --name eagleagent \
  -v /tmp/eagleagent-data:/data \
  -p 8080:8080 \
  --env-file .env.docker \
  -e PORT=9000 \
  -e GOOGLE_PROJECT_ID=my-project \
  eagleagent:local
```

### Multi-Container Setup with Docker Compose

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  eagleagent:
    build: .
    image: eagleagent:local
    container_name: eagleagent
    ports:
      - "8080:8080"
    volumes:
      - ./data:/data
    env_file:
      - .env.docker
    restart: unless-stopped
    
  # Optional: Run Firestore emulator in container
  firestore-emulator:
    image: gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators
    ports:
      - "8686:8686"
    command: gcloud emulators firestore start --host-port=0.0.0.0:8686
```

Run with:
```bash
docker-compose up
```

### Performance Profiling

```bash
# Monitor resource usage
docker stats eagleagent --no-stream

# Export stats to file
docker stats eagleagent --no-stream > stats.txt

# Check memory details
docker exec eagleagent ps aux --sort=-%mem | head -10
```

### Export Container Logs

```bash
# Export all logs
docker logs eagleagent > eagleagent.log 2>&1

# Export with timestamps
docker logs -t eagleagent > eagleagent-timestamped.log 2>&1

# Export only errors
docker logs eagleagent 2>&1 | grep -i error > errors.log
```

## Comparison: Local vs Cloud Run

| Feature | Local Docker | Cloud Run |
|---------|-------------|-----------|
| **Port** | 8080 (configurable) | 8080 (automatic) |
| **Volume** | Local bind mount | GCSFuse cloud storage |
| **Database** | `/tmp/eagleagent-data` | GCS bucket `/data` |
| **Firestore** | Emulator | Cloud Firestore |
| **Credentials** | Emulator/none needed | Service Account ADC |
| **Scaling** | Single container | 0-10 instances |
| **Cost** | Free (local resources) | ~$26/month |
| **Restart Policy** | Manual | Automatic on crash |
| **Logs** | `docker logs` | Cloud Logging |
| **Public Access** | localhost only | HTTPS URL |
| **OAuth Redirect** | `localhost:8080` | `*.run.app` |

## Next Steps

Once you've successfully tested locally:

1. **Push to Cloud Run**: Follow [CLOUD_RUN_DEPLOYMENT.md](CLOUD_RUN_DEPLOYMENT.md)
2. **Set up CI/CD**: Configure GitHub Actions for automatic deployments
3. **Configure production OAuth**: Update OAuth redirect URIs with Cloud Run URL
4. **Monitor production**: Enable Cloud Logging and Cloud Trace

## Quick Reference Commands

```bash
# Build
docker build -t eagleagent:local .

# Run (detached)
docker run -d --name eagleagent -v /tmp/eagleagent-data:/data -p 8080:8080 --env-file .env.docker eagleagent:local

# Logs
docker logs -f eagleagent

# Stop
docker stop eagleagent

# Remove
docker rm eagleagent

# Clean all
docker rm -f eagleagent && docker rmi eagleagent:local && rm -rf /tmp/eagleagent-data/
```

## Resources

- [Docker Documentation](https://docs.docker.com/)
- [Docker Best Practices](https://docs.docker.com/develop/dev-best-practices/)
- [Cloud Run Local Development](https://cloud.google.com/run/docs/testing/local)
- [Dockerfile Reference](https://docs.docker.com/engine/reference/builder/)
