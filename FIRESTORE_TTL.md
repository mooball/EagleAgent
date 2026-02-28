# Firestore TTL Policy Setup

This project uses Firestore with TTL (Time To Live) policies to automatically delete old checkpoint data.

## Timestamp Strategy

Each conversation creates a two-level structure:
```
/checkpoints/{thread_id}_{ns}/          ← partition document (session container)
  /checkpoints/{checkpoint_id}          ← checkpoint documents (conversation state)
```

**Both levels get a `created_at` timestamp** to ensure complete cleanup:
- The partition document gets timestamped (so TTL deletes the session container)
- Each checkpoint document gets timestamped (so TTL deletes individual checkpoints)

This prevents orphaned session containers after checkpoint data expires.

## Setting up TTL in Firestore

Because both the partition documents and checkpoint documents are in collections named "checkpoints", you can set **one TTL policy** that applies to both levels using a **collection-group** query.

### Option 1: Using Firebase Console

1. Go to [Firebase Console](https://console.firebase.google.com/)
2. Select your project: `mooballai`
3. Navigate to **Firestore Database**
4. Go to **Settings** → **Field TTL**
5. Click **Create Field TTL**
6. Configure:
   - Collection group ID: `checkpoints` (applies to ALL collections named "checkpoints")
   - Field path: `created_at`
   - Expiration time: e.g., `7 days`, `30 days`, etc.

### Option 2: Using gcloud CLI

```bash
# Set TTL to delete all documents with created_at older than 7 days
# This applies to the collection-group (all collections named "checkpoints")
gcloud firestore fields ttls update created_at \
  --collection-group=checkpoints \
  --enable-ttl \
  --project=mooballai
```

**Note:** Using `--collection-group=checkpoints` automatically applies the TTL to:
- Top-level partition documents: `/checkpoints/{thread_id}_{ns}`
- Nested checkpoint documents: `/checkpoints/{thread_id}_{ns}/checkpoints/{checkpoint_id}`

## Verify TTL is Working

After setting up TTL, you can verify it's working by:

1. Creating some test conversations
2. Checking the Firestore console after the TTL period
3. Old documents should be automatically deleted

## Notes

- TTL deletion happens in the background and may take up to 72 hours
- The `created_at` field is automatically set by Firestore server timestamp
- Deleting a collection's TTL policy doesn't restore deleted data
