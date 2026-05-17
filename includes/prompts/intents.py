"""
Intent definitions for procurement and research action buttons.

Each intent stores context that persists for the entire thread,
guiding the LLM's behaviour when the user clicks an action button.
"""

from pathlib import Path
from typing import Optional

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "config" / "prompts"

# =============================================================================
# PROCUREMENT INTENTS
# =============================================================================

INTENTS = {
    "find_product": {
        "label": "Product Lookup",
        "icon": "📦",
        "description": "Search the internal product catalog by part number, brand, or description",
        "follow_up": (
            "Sure — I can search our product database. Do you have a part number, "
            "brand name, supplier code, or a description of what you're looking for?"
        ),
        "context": (
            "The user wants to find a product in the internal product catalog. "
            "Use `search_products` with whatever identifiers they provide. If they "
            "give a vague description, use the semantic/vector search via the "
            "`description` parameter."
        ),
    },
    "find_supplier": {
        "label": "Supplier Lookup",
        "icon": "🔍",
        "description": "Search our supplier database by name, product, brand, or description",
        "follow_up": (
            "I can help you find a supplier. You can give me:\n"
            "- A **part number** (e.g. `6Y-0834`) — I'll find who supplies it\n"
            "- A **brand name** (e.g. `Caterpillar`) — I'll find authorised suppliers\n"
            "- A **supplier name** (e.g. `RAM Conveyors`) — I'll look them up\n"
            "- A **description** (e.g. `heavy duty conveyor belts`) — I'll search by relevance\n\n"
            "What are you looking for?"
        ),
        "context": (
            "The user wants to find a supplier. They may provide a part number, "
            "a brand name, a supplier name, a country, or a general description. "
            "Determine the type of input and use the appropriate search strategy:\n"
            "- **Part number**: Use `search_products` to identify the product and brand, "
            "then `part_purchase_history` to find proven suppliers. Fall back to "
            "`search_suppliers(brand=...)` if no purchase history exists.\n"
            "- **Brand name**: Use `search_brands` to verify the brand, then "
            "`search_suppliers(brand=...)` to find linked suppliers.\n"
            "- **Supplier name/country/description**: Use `search_suppliers` with the "
            "appropriate parameters (name, country, query).\n"
            "If the input is ambiguous (could be a part number, brand, or supplier name), "
            "ask the user to clarify before searching. "
            "Always present all returned suppliers in the results."
        ),
    },
    "check_purchase_history": {
        "label": "Purchase History",
        "icon": "📋",
        "description": "Look up past purchase orders, suppliers, and pricing from our records",
        "follow_up": (
            "I can look up purchase history. Are you looking for a specific "
            "part number, supplier, PO number, or a date range?"
        ),
        "context": (
            "The user wants to check past purchase history. Use "
            "`search_purchase_history` to find records matching their criteria. "
            "If they provide a specific part number, also use `part_purchase_history` "
            "to get a per-supplier summary. Dates use YYYY-MM-DD format."
        ),
    },
}

# =============================================================================
# RESEARCH INTENTS
# =============================================================================

RESEARCH_INTENTS = {
    "research_product_info": {
        "label": "Research a Product",
        "icon": "🔎",
        "description": "Search the web for detailed information about a product",
        "follow_up": (
            "I can research a product for you. Please provide the part number, "
            "product name, or a description and I'll search for detailed information."
        ),
        "context": (_PROMPTS_DIR / "product_research.md").read_text(),
    },
    "research_supply_chain": {
        "label": "Research a Supply Chain",
        "icon": "🌐",
        "description": "Search the web for supply chain and sourcing information for a product",
        "follow_up": (
            "I can research the supply chain for a product. Please provide the part "
            "number, product name, or description and I'll search for manufacturers, "
            "distributors, and sourcing options."
        ),
        "context": (_PROMPTS_DIR / "supply_chain_research.md").read_text(),
    },
}


def get_intent_context(intent_name: str) -> Optional[str]:
    """Return the LLM context string for a given intent, or None if unknown."""
    intent = INTENTS.get(intent_name) or RESEARCH_INTENTS.get(intent_name)
    return intent["context"] if intent else None
