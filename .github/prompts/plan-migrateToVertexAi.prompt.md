# Plan: Migrate from AI Studio to Vertex AI

## TL;DR
Switch from Google AI Studio (API key) to Vertex AI (service account + GCP project) for better reliability and rate limits. The `langchain-google-genai` v4.x already supports Vertex AI natively — no package swap needed, just configuration changes.

## Key Discovery
The official LangChain migration guide (langchain-google/discussions/1422) confirms that `ChatGoogleGenerativeAI` and `GoogleGenerativeAIEmbeddings` in `langchain-google-genai` v4.x **already support Vertex AI** via:
- `GOOGLE_GENAI_USE_VERTEXAI=true` env var
- `GOOGLE_CLOUD_PROJECT` env var
- `GOOGLE_APPLICATION_CREDENTIALS` pointing to a service account JSON
- No code imports need to change

## Steps

### Phase 1: GCP Setup (manual, user action)
1. Enable the **Vertex AI API** in the GCP project console
2. Create a service account with the **"Vertex AI User"** role
3. Download the JSON key → `service-account-key.json` (already in .gitignore)

### Phase 2: Code Changes (minimal)
4. Update `create_model()` in `app.py` — remove the explicit `google_api_key=os.getenv("GOOGLE_API_KEY")` parameter (Vertex AI uses service account credentials, auto-detected from env vars)
5. Update `GoogleGenerativeAIEmbeddings` in `includes/tools/product_tools.py` — remove explicit api_key if present
6. Update `GoogleGenerativeAIEmbeddings` in `scripts/update_product_embeddings.py` and `scripts/update_supplier_embeddings.py`
7. Update `ChatGoogleGenerativeAI` in `scripts/deduplicate_brands.py`
8. Update `scripts/smoke_test_models.py`

### Phase 3: Configuration
9. Update `.env`:
   ```
   # Remove: GOOGLE_API_KEY=...
   # Add:
   GOOGLE_GENAI_USE_VERTEXAI=true
   GOOGLE_CLOUD_PROJECT=<your-project-id>
   GOOGLE_CLOUD_LOCATION=us-central1
   GOOGLE_APPLICATION_CREDENTIALS=service-account-key.json
   ```
10. Update `.env.docker.example` with the new env vars

### Phase 4: Verification
11. Run smoke test script to verify models respond
12. Run full test suite
13. Manual test all three chat profiles

## Authentication Flow
The `google-genai` SDK auto-detects `GOOGLE_APPLICATION_CREDENTIALS` → loads service account JSON → authenticates to Vertex AI. No code-level auth needed.

## What Stays the Same
- All imports (`langchain_google_genai`)
- All model names (`gemini-2.5-flash`, etc.)
- All LangChain tool binding
- All native Google Search grounding

## What Changes
- Auth method: API key → service account
- API endpoint: generativelanguage.googleapis.com → us-central1-aiplatform.googleapis.com
- Rate limits/quotas: much higher on Vertex AI

## Relevant Files
- `app.py` — `create_model()` function (~line 297)
- `includes/tools/product_tools.py` — `GoogleGenerativeAIEmbeddings` (~line 39)
- `scripts/update_product_embeddings.py` — embeddings model
- `scripts/update_supplier_embeddings.py` — embeddings model
- `scripts/deduplicate_brands.py` — `ChatGoogleGenerativeAI` direct usage
- `scripts/smoke_test_models.py` — `GoogleGenerativeAIEmbeddings`
- `.env` — API key and model config
- `.env.docker.example` — Docker deployment template
