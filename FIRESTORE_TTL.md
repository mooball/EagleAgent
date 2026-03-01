# Firestore TTL Policy Setup

This project uses Firestore with TTL (Time To Live) policies to automatically delete old checkpoint data.

## How Firestore TTL Works

Firestore TTL policies work by monitoring a **timestamp field** in your documents. When you create a TTL policy, you:
1. Point it to a collection (or collection-group)
2. Specify which timestamp field to monitor (e.g., `expire_at`)
3. Firestore automatically deletes documents when the current time exceeds the timestamp value

**The TTL policy does NOT have a duration setting** — instead, it reads the timestamp field in each document to determine when to delete it.

## Timestamp Strategy

Each conversation creates a two-level structure:
```
/checkpoints/{thread_id}_{ns}/          ← partition document (session container)
  /checkpoints/{checkpoint_id}          ← checkpoint documents (conversation state)
```

**Both levels get `created_at` and `expire_at` timestamps** to ensure complete cleanup:
- `created_at`: Server timestamp marking when the document was created (for auditing)
- `expire_at`: Calculated timestamp (created_at + TTL duration, e.g., 7 days) that tells Firestore when to delete the document

**Why both timestamps?**
- The partition document gets both timestamps (so TTL deletes the session container)
- Each checkpoint document gets both timestamps (so TTL deletes individual checkpoints)
- This prevents orphaned session containers after checkpoint data expires

## How the Code Sets Timestamps

The `TimestampedFirestoreSaver` class (in `timestamped_firestore_saver.py`) automatically adds both timestamps when saving checkpoints:

```python
timestamp_data = {
    "created_at": SERVER_TIMESTAMP,  # Firestore server time (for auditing)
    "expire_at": datetime.now(timezone.utc) + timedelta(days=ttl_days)  # Deletion time
}
```

By default, `ttl_days=7`, meaning documents will be deleted 7 days after creation. You can customize this:

```python
# Use default 7 days
checkpointer = TimestampedFirestoreSaver(
    project_id="mooballai", 
    checkpoints_collection="checkpoints"
)

# Or specify custom TTL (e.g., 14 days)
checkpointer = TimestampedFirestoreSaver(
    project_id="mooballai", 
    checkpoints_collection="checkpoints",
    ttl_days=14
)
```

## Setting up TTL in Firestore

Because both the partition documents and checkpoint documents are in collections named "checkpoints", you can set **one TTL policy** that applies to both levels using a **collection-group** query.

The TTL policy monitors the `expire_at` timestamp field in each document. When the current time exceeds the `expire_at` value, Firestore automatically deletes that document.

### Option 1: Using Firebase Console

1. Go to [Firebase Console](https://console.firebase.google.com/)
2. Select your project: `mooballai`
3. Navigate to **Firestore Database**
4. Go to **Settings** → **Field TTL**
5. Click **Create Field TTL**
6. Configure:
   - Collection group ID: `checkpoints` (applies to ALL collections named "checkpoints")
   - Field path: `expire_at`

### Option 2: Using gcloud CLI

```bash
# Set TTL to monitor the expire_at field
# This applies to the collection-group (all collections named "checkpoints")
gcloud firestore fields ttls update expire_at \
  --collection-group=checkpoints \
  --enable-ttl \
  --project=mooballai
```

**Note:** Using `--collection-group=checkpoints` automatically applies the TTL to:
- Top-level partition documents: `/checkpoints/{thread_id}_{ns}`
- Nested checkpoint documents: `/checkpoints/{thread_id}_{ns}/checkpoints/{checkpoint_id}`

**Important:** The TTL policy itself doesn't define "how long" documents live — that's determined by the `expire_at` value you set on each document (default: 7 days from creation).

## Verify TTL is Working

After setting up TTL, you can verify it's working by:

1. Creating some test conversations
2. Checking the Firestore console after the TTL period
3. Old documents should be automatically deleted

## Notes

- TTL deletion happens in the background and may take up to 72 hours after the `expire_at` timestamp
- The `created_at` field is set using Firestore SERVER_TIMESTAMP for accurate server-side timing
- The `expire_at` field is calculated as: current time + TTL duration (default: 7 days)
- You can customize the TTL duration when initializing `TimestampedFirestoreSaver(ttl_days=14)`
- Deleting a collection's TTL policy doesn't restore deleted data


