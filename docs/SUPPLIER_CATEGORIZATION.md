# Supplier Categorization

EagleAgent uses Gemini LLM to categorize suppliers against a supply chain taxonomy. Categories help the ProcurementAgent filter suppliers by industry segment and power the dashboard supplier views.

## How It Works

1. **Taxonomy** — A structured category list lives in `config/prompts/supplier_categorization.md`. Categories are organized into tiers (e.g., "Bearings", "Hydraulics", "Electrical").

2. **Categorization** — `includes/supplier_categorization.py` builds a prompt containing the supplier's name, URL, location, and purchase history, then asks Gemini to select the best-fitting category and tier.

3. **Results** — Categories are stored on supplier records. The dashboard filters and the ProcurementAgent use them for supplier search and matching.

## Key Module

`includes/supplier_categorization.py`

| Function | Purpose |
|---|---|
| `load_taxonomy()` | Load and cache the taxonomy markdown file |
| `build_prompt(taxonomy, supplier)` | Build the Gemini categorization prompt for one supplier |
| `categorize_supplier(supplier, model)` | Categorize a single supplier (returns category + tier + confidence) |
| `categorize_suppliers(suppliers, model)` | Batch-categorize multiple suppliers |

## Running

```bash
# Batch categorize all uncategorized suppliers
uv run python -m scripts.categorize_suppliers_job

# Categorize a specific subset
uv run python -m scripts.categorize_suppliers --limit 50
```

## Configuration

- Valid categories and tiers are defined in `config/settings.py` via `get_valid_categories()` and `get_valid_tiers()`
- The taxonomy markdown file is at `config/prompts/supplier_categorization.md`
- Taxonomy is cached at module load — restart the app to pick up taxonomy changes
