## Plan: Setup Products Vector Database & Agent

To integrate a new vector-indexed Products and Suppliers database within the existing PostgreSQL infrastructure, we will swap the base Postgres image to support `pgvector`, build the necessary schemas using SQLAlchemy/Alembic, add scripts to embed and import the data via Gemini, and finally expose this data to the LangGraph application via a specialized Product Agent.

**Steps**

**Phase 0: Upgrade Local Docker PostgreSQL 16 → 17 + pgvector**

Railway is already on PG17 with pgvector available — no changes needed there. This phase only affects the local Docker Compose environment.

1. Stop the running container: `docker compose down`.
2. Delete the existing PG16 volume: `docker volume rm eagleagent_postgres_data` (data is expendable).
3. Update `docker-compose.yml` image from `postgres:16-alpine` to `pgvector/pgvector:0.8.0-pg17`.
4. Start fresh: `docker compose up -d postgres`.
5. Run `alembic upgrade head` to recreate the Chainlit/checkpoint schema on the new PG17 instance.

**Phase 1: Database Setup & Schema**
1. Update `docker-compose.yml` to use `pgvector/pgvector:0.8.0-pg17` (already done in Phase 0).
2. Add `pgvector` and `pandas` (for easier data processing) to `pyproject.toml` dependencies.
3. Create a unified `includes/db_models.py` file to declare the SQLAlchemy declarative base and define `Supplier` and `Product` models. The `Product` model will use `pgvector.sqlalchemy.Vector(768)` for the `embedding` column.
4. Generate a new Alembic migration that first executes `CREATE EXTENSION IF NOT EXISTS vector;` and then creates the `suppliers` and `products` tables.
*depends on Phase 0, steps 2 and 3*

**Phase 2: Data Processing & Import Scripts**
5. Create a standalone python script `scripts/import_data.py` to handle CSV merging and data cleansing.
6. Define an `IMPORT_DIR` inside `.env` configuration (defaulting to `./data/import/`).
7. Expect formatted CSV files like `products_import_01.csv` and `suppliers_import_A.csv`.
8. Have Pandas automatically clean trailing spaces, empty strings, and quoted fields.
9. Use an ORM-based upsert to securely handle records containing identical `netsuite_id` or `part_number` fields. Prevent overwriting existing valid properties with incoming blank/null fields. Handle CSV-internal duplicates smoothly.
10. Create a separate script `scripts/update_product_embeddings.py` that queries the database for records missing embeddings, connects to Google Generative AI (using an environment variable `EMBEDDINGS_MODEL`), batches the texts (e.g. "Description + Brand") with rate limiting, and saves the vector data back to the database.
*depends on 4*

**Phase 3: Tools and Specialized Agent**
8. Create `includes/tools/product_tools.py` with an async `@tool` named `search_products(part_number: str = None, brand: str = None, supplier_code: str = None, description: str = None, limit: int = 5)`.
   - If `part_number`, `brand`, or `supplier_code` is provided, use standard SQLAlchemy string matching (`ilike` or exact) to filter the table.
   - If `description` is provided, generate a Gemini embedding for the search description and apply pgvector proximity sorting (`embedding.cosine_distance()`).
   - Combine both methods seamlessly if multiple arguments are provided.
9. Implement `ProcurementAgent` in `includes/agents/procurement_agent.py` by extending `BaseSubAgent` from your existing agent architecture and equipping it with the `search_products` tool.
*depends on 8*

**Phase 4: LangGraph Integration**
10. Update `includes/agents/supervisor.py` to route user intents correctly to the `ProcurementAgent` instead of the general or browser agent when users search for parts, products, or suppliers.
11. Update `app.py` to add the `ProcurementAgent` node to the StateGraph workflow and connect conditional edges.
*depends on 9, 10*

**Proposed Database Schema**

* `products` table:
  - `id` (UUID, Primary Key)
  - `netsuite_id` (String, Unique) — NetSuite ID
  - `part_number` (String, Unique, Indexed) — Name / Item / Part Number
  - `supplier_code` (String, Nullable) — Supplier's preferred item code
  - `description` (Text)
  - `brand` (String)
  - `weight_kg` (Float)
  - `length_m` (Float)
  - `product_type` (String)
  - `embedding` (Vector(256)) — pgvector column for Gemini embeddings

* `suppliers` table (stub):
  - `id` (UUID, Primary Key)
  - `netsuite_id` (String, Unique) — NetSuite ID  
  - `name` (String)
  - *(additional fields to be defined)*

**Relevant files**
- `docker-compose.yml` — Update the PostgreSQL image to support vector operations.
- `pyproject.toml` — Add `pgvector` dependency.
- `app.py` — Add new `ProcurementAgent` state node and update edge routing.
- `includes/db_models.py` (new) — Define SQLAlchemy base, `Supplier`, and `Product` schemas.
- `scripts/import_products.py` (new) — CSV/JSON importer.
- `includes/tools/product_tools.py` (new) — LangChain tools wrapper for similarity matching.
- `includes/agents/procurement_agent.py` (new) — Dedicated agent class based on `BaseSubAgent`.
- `includes/agents/supervisor.py` — Add custom routing rules for `ProcurementAgent`.

**Decisions**
- Upgrade to PG17 now while the database has no production data worth preserving. This avoids a painful major-version migration later.
- Local dev uses `pgvector/pgvector:0.8.0-pg17` Docker image. Railway's managed PG already bundles pgvector — no custom image needed there.
- Embedding model: Gemini `gemini-embedding-2-preview` (256 dimensions). 
- The schema splits into Products and Suppliers. A join table will be added later for the relationships, rather than a direct Foreign Key. Agent tools will do `JOIN`s to return complete data cleanly.

**Questions/Considerations**
1. For generating the Gemini embedding on a product, I recommend concatenating `Description + Brand` into a single string. Does this match how you'd like your vector semantic search to index the item?
2. Currently, your existing tools check `user_id` permissions dynamically. Do these Products have any user access limitations, or should the agent tool treat it as a globally searchable catalog?
