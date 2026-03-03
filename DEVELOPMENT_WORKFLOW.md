# Development Workflow Guide

Quick reference for the recommended development workflow for EagleAgent.

## Daily Development Cycle

```bash
# Morning - Start local development server
./run.sh

# Code all day with hot reload...
# Test in browser at http://localhost:8000
# Edit files → Save → Auto-refresh in browser

# Evening - Stop the server
./kill.sh
```

## Before Committing Code

```bash
# Run test suite
./run_tests.sh

# Optionally: Quick Docker validation
docker build -t eagleagent:local . && \
docker run --rm -d -p 8080:8080 --env-file .env.docker eagleagent:local

# If you ran Docker test, clean up
docker rm -f $(docker ps -aq --filter ancestor=eagleagent:local)
```

## Before Deploying to Cloud Run

```bash
# 1. Build Docker image
docker build -t eagleagent:local .

# 2. Run full Docker test
docker run -d --name eagleagent \
  -v /tmp/eagleagent-data:/data \
  -p 8080:8080 \
  --env-file .env.docker \
  eagleagent:local

# 3. Thorough testing
# - Open http://localhost:8080
# - Test OAuth flow
# - Send messages
# - Upload files
# - Check logs
docker logs -f eagleagent

# 4. If all tests pass, cleanup and deploy
docker rm -f eagleagent

# 5. Deploy to Cloud Run via GitHub Actions
git add .
git commit -m "Your commit message"
git push origin main  # Triggers GitHub Actions → Cloud Run

# OR deploy manually with gcloud
gcloud run deploy eagleagent \
  --source . \
  --region australia-southeast1 \
  --execution-environment gen2 \
  --memory 2Gi \
  --cpu 2 \
  --add-volume name=gcs-data,type=cloud-storage,bucket=eagleagent-data \
  --add-volume-mount volume=gcs-data,mount-path=/data
```

## Quick Commands Reference

### Local Development
```bash
# Start with hot reload
./run.sh

# Stop server
./kill.sh

# Run tests
./run_tests.sh

# View Firestore emulator logs
tail -f /tmp/firestore-emulator.log
```

### Docker Testing
```bash
# Build image
docker build -t eagleagent:local .

# Run detached
docker run -d --name eagleagent \
  -v /tmp/eagleagent-data:/data \
  -p 8080:8080 \
  --env-file .env.docker \
  eagleagent:local

# View logs
docker logs -f eagleagent

# Stop and remove
docker rm -f eagleagent

# Clean all
docker rm -f eagleagent && \
docker rmi eagleagent:local && \
rm -rf /tmp/eagleagent-data/
```

### Deployment
```bash
# Check Cloud Run status
gcloud run services describe eagleagent \
  --region=australia-southeast1

# Get Cloud Run URL
gcloud run services describe eagleagent \
  --region=australia-southeast1 \
  --format='value(status.url)'

# View Cloud Run logs
gcloud run services logs tail eagleagent \
  --region=australia-southeast1
```

## Environment Files Cheat Sheet

| File | Purpose | Used By |
|------|---------|---------|
| `.env` | Local development | `./run.sh` |
| `.env.docker` | Docker local testing | `docker run --env-file .env.docker` |
| `.env.cloudrun.example` | Cloud Run template | Reference only |
| `service-account-key.json` | Local GCP auth | Local dev only |

## Port Reference

| Environment | Port | URL |
|-------------|------|-----|
| Local dev | 8000 | http://localhost:8000 |
| Docker local | 8080 | http://localhost:8080 |
| Cloud Run | 8080 | https://eagleagent-*.run.app |

## When to Use Each Environment

- **Daily coding**: Local (`./run.sh`) - fastest iteration
- **Pre-commit**: Optional Docker test
- **Pre-deployment**: Required Docker validation
- **Production**: Cloud Run

## Troubleshooting

### Local server won't start
```bash
# Check if port is already in use
lsof -i :8000
kill <PID>

# Check Firestore emulator
ps aux | grep firestore | grep -v grep
```

### Docker build fails
```bash
# Clean Docker cache
docker builder prune -a
docker build --no-cache -t eagleagent:local .
```

### Can't connect to Firestore emulator from Docker
```bash
# On Linux, update .env.docker:
FIRESTORE_EMULATOR_HOST=172.17.0.1:8686

# On Mac/Windows, use:
FIRESTORE_EMULATOR_HOST=host.docker.internal:8686
```

## Full Documentation

- [DOCKER_LOCAL_TESTING.md](DOCKER_LOCAL_TESTING.md) - Complete Docker testing guide
- [CLOUD_RUN_DEPLOYMENT.md](CLOUD_RUN_DEPLOYMENT.md) - Cloud Run deployment guide
- [TESTING.md](TESTING.md) - Testing guide and best practices
