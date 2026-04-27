# Skill Definition: Supplier Categorization

**Industrial Procurement & Supply Chain Taxonomy (v1.0)**

## 1. Objective

This skill defines the logic for categorizing industrial suppliers based on their commercial persona, pricing structures, and position within the supply chain. It is optimized for use in procurement houses identifying partners in the mining, construction, and heavy industry sectors.

## 2. Taxonomy of Supplier Roles

### Tier A: Primary Sources (The Makers)

| Role Name | Definition | Clues | LLM Rule | Public Pricing? |
|---|---|---|---|---|
| **OEM (Original Equipment Manufacturer)** | The brand owner (e.g., Caterpillar, Kincrome). Designs the parts. Usually only sells "Genuine" items. **Always classified as OEM even if they also sell direct-to-public or show pricing online.** | "Proprietary", "Genuine Parts", Single-brand focus. | Only sells their own brand. Often uses "Genuine" or "Original" terminology. Brand ownership overrides all other signals. | Varies |
| **Aftermarket Manufacturer** | Third-party maker of equivalent parts. No retail presence. Sells "to fit" other brands. | "ISO Certified", "Equivalent to...", "PMA Approved". | Makes parts for many brands. No "retail" shop. Often mentions ISO standards. | No (Quote only) |

### Tier B: Industrial Trade Partners (The B2B Core)

| Role Name | Definition | Clues | LLM Rule | Public Pricing? |
|---|---|---|---|---|
| **Trade Wholesaler** | High-volume stockists. No public storefront. Requires a credit application to view prices. | "Trade Account Required", "B2B Only", Login to view price. | Sells many brands. Requires a login to see prices. No physical shopfront for the public. | No (Login Req.) |
| **Authorized Dealer** | Third-party business with a direct contract to an OEM. Regional focus. | "Authorized Partner", "Service Center", OEM branding used. | A middleman with a direct contract to the OEM. Uses the OEM's branding heavily. | No (Quote only) |

### Tier C: General Commercial Sellers & Specialists

| Role Name | Definition | Clues | LLM Rule | Public Pricing? |
|---|---|---|---|---|
| **Retail / Trade Outlet** | Physical stores with a trade desk. Sells to anyone. High convenience, high price. | "Add to Cart", "Guest Checkout", Store locator. | Has a physical store (e.g., Grainger). Has an "Add to Cart" button for anyone. Also offers trade accounts. | Yes (Visible) |
| **Online Distributor** | Digital platforms (e.g. RS Components). Visible fixed pricing for everyone. | E-commerce interface, Clear list pricing, Credit card accepted. | Digital-first. Massive range. Fixed pricing is visible, but offers "Business Accounts." | Yes (Visible) |
| **Service Exchange (SX) Provider** | Specializes in refurbished/rebuilt heavy components. Exchange based. | "Core Charge", "Reman", "Exchange Basis". | Only sells refurbished/rebuilt heavy components. Mentions "Core Charges." | No (Quote only) |
| **Sourcing Broker** | Does not hold stock. Acts as an intermediary for a commission. | "Procurement Services", "Global Sourcing", No warehouse address. | Does not hold stock. Mentions "procurement services" or "finding parts." | No (Quote only) |

### Tier D: Retail Outlets (B2C / Full Price)

Sellers focusing on the general public or small tradesmen with zero to negligible procurement discounts.

| Role Name | Definition | Clues | LLM Rule | Public Pricing? |
|---|---|---|---|---|
| **B2C Retailer** | Consumer-facing retail. Full retail pricing with no trade discount pathway. | "Buy now", High-street presence, focus on home/hobby use. | Targets consumers, not trade buyers. No trade account option. Full retail price only. | Yes (Full Retail) |
| **Hardware / Big Box** | Major national hardware chains (e.g. Bunnings, Home Depot). Public guest checkout. Consumer list pricing. | National chain branding, store locator, guest checkout, consumer list pricing. | Large national retail chain. Sells to public at consumer list prices. May have a trade desk but pricing is still consumer-grade. | Yes (Consumer List) |

## 3. Categorization Decision Logic

*Follow this hierarchical logic when analyzing a supplier website or profile:*

### Step 1: Check Brand Ownership (OEM Override)

- If the supplier **owns the brand** and manufactures their own products, they are always an **OEM** — regardless of whether they also sell online, show public pricing, or operate retail outlets.
  - Key signal: the company name IS the brand, uses "Genuine" or "Original" terminology, single-brand focus.
  - Example: Kincrome, Caterpillar, Bosch. Even if they have an "Add to Cart" e-commerce site, the OEM classification takes priority.
- If the supplier is NOT a brand owner: Proceed to Step 2.

### Step 2: Check Pricing Visibility

- If prices are visible to any guest: Determine the seller type:
  - Consumer-only retailer with no trade account pathway → **B2C Retailer** (Tier D).
  - Major national hardware chain (e.g. Bunnings, Home Depot) with consumer list pricing → **Hardware / Big Box** (Tier D).
  - Physical store with trade desk and "Add to Cart" → **Retail / Trade Outlet** (Tier C).
  - Digital-first platform with massive range, visible list pricing, and business accounts → **Online Distributor** (Tier C).
- If "Login for Price" or "Request Quote" is mandatory: Proceed to Step 3.

### Step 3: Check Manufacturing Status

- If they claim to manufacture their own parts but don't mention a parent brand: **Aftermarket Manufacturer**.
  - Key signal: sells parts described as "Replacement for [Brand]", "Equivalent to...", or "To fit [Brand]".

### Step 4: Check Account Requirements & Branding

- If they require an ABN/Tax ID and 30-day credit application for all new customers: **Trade Wholesaler**.
  - Key signal: "Request a Trade Account", "ABN Required", sells many brands.
- If they emphasize OEM warranty and official partnership status: **Authorized Dealer**.
  - Key signal: sells only one brand (e.g. only Caterpillar parts) but is NOT the brand owner. Uses the OEM's branding heavily, mentions "Authorized Partner" or "Service Center".

### Step 5: Check Asset Model

- If they mention "Core Returns" or "sending back your old unit": **Service Exchange**.
  - Key signal: "Core Charge", "Reman", "Exchange Basis", refurbished/rebuilt heavy components.
- If they have no physical stock and offer "Finding services": **Sourcing Broker**.
  - Key signal: "Procurement Services", "Global Sourcing", no warehouse address.

## 4. Confidence Scoring

When using an LLM to categorize, ensure it provides a confidence score (1-5) based on the evidence available on the supplier's website.

- **Score 5:** Clear "Terms of Sale" confirming role (e.g., Wholesaler).
- **Score 3:** Assumed role based on "Request Quote" buttons only.
- **Score 1:** Generic website with conflicting clues.
