"""
Intent definitions for procurement and research action buttons.

Each intent stores context that persists for the entire thread,
guiding the LLM's behaviour when the user clicks an action button.
"""

from typing import Optional

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
    "new_rfq": {
        "label": "New RFQ",
        "icon": "📋",
        "description": "Create a new Request for Quote",
        "follow_up": (
            "I'll help you set up this RFQ. Please provide the **Customer Name** "
            "along with a **list of products**. Ideally include product names, brands, "
            "and part numbers.\n\n"
            "You can type the details, or upload an **image** (PNG, JPG), **PDF**, **text file** (TXT, CSV), or **spreadsheet** (XLS, XLSX) of the parts list."
        ),
        "context": (
            "The user is setting up an RFQ (Request for Quote). "
            "IMPORTANT: Check the [Dashboard Context] first. If an RFQ ID is "
            "already shown (e.g. the user is viewing an RFQ detail page), you "
            "MUST update that existing RFQ — do NOT create a new one. Use "
            "`manage_rfq(action='update', rfq_id=..., data={customer: ...})` to "
            "set the customer, and `manage_rfq(action='add_items', rfq_id=..., "
            "data={items: [...]})` to add line items in bulk. "
            "Only use `manage_rfq(action='create', data={...})` if there is NO "
            "RFQ in the dashboard context. "
            "Gather the customer name and a parts list. The parts list can come "
            "from text, a screenshot, or an attachment — extract each line item. "
            "IMPORTANT: For EVERY item, you MUST populate ALL available fields: "
            "input_description, input_code (the original part/code from the request), "
            "part_number (same as input_code if given), brand, quantity, and uom. "
            "Do NOT omit part numbers or brands — if they appear in the source data, "
            "they MUST be included in the item data. "
            "After populating the RFQ, STOP and present the RFQ summary for the "
            "user to review. Ask them to confirm the customer details and line "
            "items are correct before proceeding. Do NOT search for products or "
            "suppliers until the user explicitly confirms the RFQ or asks you to."
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
        "context": (
            "The user wants to research a specific product. Follow this process "
            "carefully:\n\n"
            "## Step 1: Identify the Product\n"
            "Before presenting any information, you MUST positively identify the "
            "product. Search the web to:\n"
            "- Confirm the part number exists, or find the corrected syntax if the "
            "user made a typo.\n"
            "- Confirm the brand/manufacturer for the part.\n\n"
            "**Never guess.** If you cannot identify the product with certainty, "
            "ask the user for more information — name, brand, description, or "
            "application — to help narrow it down.\n\n"
            "## Step 2: Present Product Information\n"
            "Once the product is positively identified, present your findings using "
            "this exact structure with markdown headings:\n\n"
            "### Part Number\n"
            "The confirmed part number (e.g. 1G8878).\n\n"
            "### Product Name\n"
            "The full product name (e.g. Spin-On Hydraulic and Transmission Oil "
            "Filter).\n\n"
            "### Brand\n"
            "The manufacturer or brand (e.g. Caterpillar (CAT)). Include the "
            "industry-standard cross-reference if one exists (e.g. HF6553 from "
            "Fleetguard / Cummins Filtration).\n\n"
            "### Product Description\n"
            "A concise description of what the product is, its purpose, and any "
            "notable characteristics such as performance ratings, classifications, "
            "or design features.\n\n"
            "### Technical Specifications\n"
            "Key measurements and technical details as a bullet list. Include "
            "whichever specifications are relevant to the product — this could be "
            "dimensions, weight, volume, power ratings, voltage, pressure ratings, "
            "flow capacity, micron ratings, thread sizes, material composition, or "
            "any other measurable attribute. Focus on what matters for the specific "
            "product type. Present all measurements in metric units (mm, kg, litres, "
            "kW, bar, etc.).\n\n"
            "### Primary Applications\n"
            "List the machinery, equipment, or systems this product is used in. "
            "Group by manufacturer with specific model numbers. For heavy machinery "
            "spare parts, cover:\n"
            "- **Caterpillar Equipment** — Wheel loaders, articulated dump trucks, "
            "off-highway trucks, telehandlers, excavators, skid steers, etc. with "
            "specific series/model numbers.\n"
            "- **Other Brands (via Cross-Reference)** — e.g. Bobcat skid steer "
            "loaders, John Deere tractors and combines, Case/New Holland "
            "agricultural and construction equipment.\n\n"
            "### Equivalent Parts\n"
            "List aftermarket alternatives and direct cross-reference part numbers "
            "from other manufacturers as a bullet list. Include manufacturer name "
            "and part number for each (e.g. Donaldson: P164378, Baldwin: BT8851-MPG, "
            "WIX: 51494, John Deere: RE47313, Bobcat: 6668819).\n\n"
            "---\n"
            "Cite sources for all information. Use this exact heading structure for "
            "every product research response to ensure consistency."
        ),
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
        "context": (
            "The user wants to research the supply chain for a specific product. "
            "Follow this process carefully:\n\n"
            "## Step 1: Identify the Product\n"
            "Before researching the supply chain, you MUST positively identify the "
            "product. Search the web to confirm the part number exists (or find the "
            "corrected syntax) and confirm the brand/manufacturer.\n\n"
            "**Never guess.** If you cannot identify the product with certainty, "
            "ask the user for more information — name, brand, description, or "
            "application — to help narrow it down.\n\n"
            "## Step 2: Map the Supply Chain by Tier\n"
            "Once the product is positively identified, search the web to find "
            "suppliers across the four supply chain tiers below. Aim to find "
            "around 5 suppliers per tier, but prioritise quality over quantity — "
            "do not pad a tier with poor matches. A tier or sub-category may be "
            "empty if no credible suppliers exist.\n\n"
            "### Tier A — Manufacturers (The Makers)\n"
            "- **OEM (Original Equipment Manufacturer)** — The brand owner who "
            "designs and manufactures the product. Sells only \"Genuine\" parts. "
            "Identify the parent company and manufacturing locations.\n"
            "- **Aftermarket Manufacturer** — Third-party makers of compatible or "
            "equivalent parts (\"to fit\" other brands). Include quality "
            "comparisons where available (e.g. ISO certification, PMA approval).\n\n"
            "### Tier B — Industrial Trade Partners (B2B)\n"
            "- **Trade Wholesaler** — High-volume stockists that carry many brands. "
            "Typically require a trade account or login to view pricing.\n"
            "- **Authorized Dealer** — Third-party businesses with a direct OEM "
            "contract. Regional focus, uses the OEM's branding heavily.\n\n"
            "### Tier C — General Commercial Sellers (Public Access)\n"
            "- **Retail / Trade Outlet** — Physical stores with a trade desk that "
            "sell to anyone (e.g. Bunnings, Grainger). Visible \"Add to Cart\" pricing.\n"
            "- **Online Distributor** — Digital-first platforms (e.g. RS Components, "
            "PartSouq) with visible fixed pricing and broad range.\n\n"
            "### Tier D — Specialist Commercial (if any exist)\n"
            "- **Service Exchange (SX) Provider** — Specialises in refurbished/"
            "rebuilt heavy components on an exchange basis (\"Core Charge\", \"Reman\").\n"
            "- **Sourcing Broker** — Does not hold stock. Acts as an intermediary "
            "offering procurement or global sourcing services.\n\n"
            "For each supplier found, state its **name, category, tier, and URL**.\n\n"
            "## Step 3: Sourcing Analysis\n"
            "1. **Geographic Sourcing** — Key sourcing regions (e.g. China, USA, "
            "Europe) and typical lead times.\n"
            "2. **Pricing Landscape** — Price ranges across OEM, aftermarket, and "
            "different suppliers to identify cost-effective sourcing options.\n"
            "3. **Supply Risks** — Any known supply chain risks, shortages, or "
            "disruptions affecting this product or category.\n\n"
            "Cite sources for all information."
        ),
    },
}


def get_intent_context(intent_name: str) -> Optional[str]:
    """Return the LLM context string for a given intent, or None if unknown."""
    intent = INTENTS.get(intent_name) or RESEARCH_INTENTS.get(intent_name)
    return intent["context"] if intent else None
