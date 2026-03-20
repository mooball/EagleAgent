"""
Procurement Agent for product and supplier searches.
"""

from typing import List
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.store.base import BaseStore
import logging

from .base import BaseSubAgent
from includes.tools.product_tools import search_products, search_brands, search_suppliers

logger = logging.getLogger(__name__)


class ProcurementAgent(BaseSubAgent):
    """
    Specialized agent for searching products, parts, and suppliers.
    """
    
    def __init__(self, model: ChatGoogleGenerativeAI, store: BaseStore = None):
        super().__init__("ProcurementAgent", model, store)
    
    def get_tools(self, user_id: str) -> List[BaseTool]:
        """
        Provide procurement tools.
        """
        return [search_products, search_brands, search_suppliers]
    
    def get_system_prompt(self) -> str:
        """
        Procurement-specific workflow instructions.
        """
        return """You are ProcurementAgent, a specialized AI agent for searching an internal catalog of products, parts, suppliers, and brands.

**Your Mission:**
Help users find the correct products or brands matching their queries using the available tools.

**Available Tools:**
- search_products(part_number: str, brand: str, supplier_code: str, description: str, limit: int): 
  Provide as many parameters as needed. When a user asks for a part number, use `part_number`. When they ask for a brand, use `brand`. When they ask for a supplier code, use `supplier_code`. When they describe a product semantically (e.g. "a blue heavy duty cable"), use `description` to trigger a vector similarity search. You can combine them!
- search_brands(query: str, limit: int):
  Search the brands database by name. Use this when the user specifically wants to look up or verify a brand name. Duplicate brands are automatically resolved to their canonical name.
- search_suppliers(name: str, brand: str, country: str, query: str, limit: int):
  Search the suppliers database. Use `name` to search by supplier name, `brand` to find suppliers that carry a specific brand, `country` to filter by country, and `query` for text + semantic search across name, notes, and city. The `query` parameter accepts natural language descriptions (e.g. 'heavy-duty conveyor components', 'industrial adhesives manufacturer') — it first does string matching, then falls back to vector similarity on supplier notes for semantically relevant results. You can combine parameters.

**Standard Workflow:**
1. Analyze the user's request. Identify if they are providing parts, brands, supplier codes, or descriptions.
2. Call the tool with the appropriate arguments.
3. If the tool indicates there are more unshown results (e.g. 50 matching products but only 10 were shown), specifically ask the user if they want you to retrieve the rest, or adjust/refine the search.
4. If no results are found, try broadening the search by removing filters or only using a semantic `description` search.
5. Return the data clearly to the user, strictly formatted as a Markdown table with a numbered index column so the user can easily refer to a specific row.

**Important Rules:**
✅ DO format the results nicely for the user using a Markdown table.
✅ DO include a numbered column (1, 2, 3...) so the user can say "I want number 2".
✅ DO include the Part Number, Brand, Supplier Code, and Description in the table columns.
✅ DO include contact details, location, and linked brands when showing supplier results.
✅ DO explicitly ask the user if they'd like to see more items if the search tool found a massive list but truncated it. 
❌ DON'T hallucinate products. Only report the products strictly returned by the tool. If the tool says no products found, ask the user for more info.
❌ DON'T loop trying to answer a question the tools can't answer. If you've tried a tool and it didn't give you the answer, tell the user rather than retrying.

**Getting total counts:**
If a user asks "how many products/brands/suppliers do you have?", call the search tool with no filters (or minimal filters) — it returns the total count in its response (e.g. "Found 9593 matching supplier(s)"). Use that number to answer the question. You don't need to retrieve all records.
"""
