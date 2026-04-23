# Skill Definition: Supplier Categorization

**Industrial Procurement & Supply Chain Taxonomy (v1.0)**

## 1. Objective

This skill defines the logic for categorizing industrial suppliers based on their commercial persona, pricing structures, and position within the supply chain. It is optimized for use in procurement houses identifying partners in the mining, construction, and heavy industry sectors.

## 2. Taxonomy of Supplier Roles

### Tier A: Primary Sources (The Makers)

| Role Name | Definition | Clues | LLM Rule | Public Pricing? |
|---|---|---|---|---|
| **OEM (Original Equipment Manufacturer)** | The brand owner (e.g., Caterpillar). Designs the parts. Usually only sells "Genuine" items. | "Proprietary", "Genuine Parts", Single-brand focus. | Only sells their own brand. Often uses "Genuine" or "Original" terminology. | No (Quote only) |
| **Aftermarket Manufacturer** | Third-party maker of equivalent parts. No retail presence. Sells "to fit" other brands. | "ISO Certified", "Equivalent to...", "PMA Approved". | Makes parts for many brands. No "retail" shop. Often mentions ISO standards. | No (Quote only) |

### Tier B: Industrial Trade Partners (The B2B Core)

| Role Name | Definition | Clues | LLM Rule | Public Pricing? |
|---|---|---|---|---|
| **Trade Wholesaler** | High-volume stockists. No public storefront. Requires a credit application to view prices. | "Trade Account Required", "B2B Only", Login to view price. | Sells many brands. Requires a login to see prices. No physical shopfront for the public. | No (Login Req.) |
| **Authorized Dealer** | Third-party business with a direct contract to an OEM. Regional focus. | "Authorized Partner", "Service Center", OEM branding used. | A middleman with a direct contract to the OEM. Uses the OEM's branding heavily. | No (Quote only) |

### Tier C: General Commercial Sellers (Public Access)

| Role Name | Definition | Clues | LLM Rule | Public Pricing? |
|---|---|---|---|---|
| **Retail / Trade Outlet** | Physical stores with a trade desk. Sells to anyone. High convenience, high price. | "Add to Cart", "Guest Checkout", Store locator. | Has a physical store (e.g., Bunnings/Grainger). Has an "Add to Cart" button for anyone. | Yes (Visible) |
| **Online Distributor** | Digital platforms (e.g. RS Components). Visible fixed pricing for everyone. | E-commerce interface, Clear list pricing, Credit card accepted. | Digital-first. Massive range. Fixed pricing is visible, but offers "Business Accounts." | Yes (Visible) |

### Tier D: Specialist Commercial Models

| Role Name | Definition | Clues | LLM Rule | Public Pricing? |
|---|---|---|---|---|
| **Service Exchange (SX) Provider** | Specializes in refurbished/rebuilt heavy components. Exchange based. | "Core Charge", "Reman", "Exchange Basis". | Only sells refurbished/rebuilt heavy components. Mentions "Core Charges." | No (Quote only) |
| **Sourcing Broker** | Does not hold stock. Acts as an intermediary for a commission. | "Procurement Services", "Global Sourcing", No warehouse address. | Does not hold stock. Mentions "procurement services" or "finding parts." | No (Quote only) |

## 3. Categorization Decision Logic

*Follow this hierarchical logic when analyzing a supplier website or profile:*

### Step 1: Check Pricing Visibility

- If prices are visible to any guest: Categorize as **Retail / Trade Outlet** or **Online Distributor**.
  - Physical store with "Add to Cart" and store locator → **Retail / Trade Outlet**.
  - Digital-first platform with massive range and visible list pricing → **Online Distributor**.
- If "Login for Price" or "Request Quote" is mandatory: Proceed to Step 2.

### Step 2: Check Manufacturing Status

- If they claim to manufacture their own parts but don't mention a parent brand: **Aftermarket Manufacturer**.
  - Key signal: sells parts described as "Replacement for [Brand]", "Equivalent to...", or "To fit [Brand]".
- If they manufacture and own a major global brand: **OEM**.
  - Key signal: only sells their own brand, uses "Genuine" or "Original" terminology throughout.

### Step 3: Check Account Requirements & Branding

- If they require an ABN/Tax ID and 30-day credit application for all new customers: **Trade Wholesaler**.
  - Key signal: "Request a Trade Account", "ABN Required", sells many brands.
- If they emphasize OEM warranty and official partnership status: **Authorized Dealer**.
  - Key signal: sells only one brand (e.g. only Caterpillar parts) but is NOT the brand owner. Uses the OEM's branding heavily, mentions "Authorized Partner" or "Service Center".

### Step 4: Check Asset Model

- If they mention "Core Returns" or "sending back your old unit": **Service Exchange**.
  - Key signal: "Core Charge", "Reman", "Exchange Basis", refurbished/rebuilt heavy components.
- If they have no physical stock and offer "Finding services": **Sourcing Broker**.
  - Key signal: "Procurement Services", "Global Sourcing", no warehouse address.

## 4. Confidence Scoring

When using an LLM to categorize, ensure it provides a confidence score (1-5) based on the evidence available on the supplier's website.

- **Score 5:** Clear "Terms of Sale" confirming role (e.g., Wholesaler).
- **Score 3:** Assumed role based on "Request Quote" buttons only.
- **Score 1:** Generic website with conflicting clues.
