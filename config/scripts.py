"""
Script registry for server-side script execution.

Maps human-friendly names to actual commands, controlling which
scripts can be invoked from the chat UI. Only registered scripts
can be run — this prevents arbitrary command injection.

To add a new script, add an entry to SCRIPT_REGISTRY below.
"""

# Each entry maps a script key to its metadata:
#   command:       Base command as a list (no user-supplied args injected here)
#   description:   Shown to admins when listing available scripts
#   args_allowed:  Flags that may be appended (validated before use)
#   long_running:  Hint for the UI — if True, periodic progress updates are sent

SCRIPT_REGISTRY: dict[str, dict] = {
    "update_product_embeddings": {
        "command": ["uv", "run", "python", "-m", "scripts.update_product_embeddings"],
        "description": "Regenerate missing product vector embeddings (batches of 100, ~4s per batch)",
        "args_allowed": [],
        "long_running": True,
    },
    "update_supplier_embeddings": {
        "command": ["uv", "run", "python", "-m", "scripts.update_supplier_embeddings"],
        "description": "Regenerate missing supplier vector embeddings (batches of 100, ~4s per batch)",
        "args_allowed": [],
        "long_running": True,
    },
    "sync_netsuite_suppliers": {
        "command": ["uv", "run", "python", "-m", "scripts.sync_netsuite_suppliers"],
        "description": "Sync suppliers from NetSuite API (--since YYYY-MM-DD or Nd e.g. 7d; default: 30d)",
        "args_allowed": ["--since", "--dry-run"],
        "long_running": False,
    },
    "sync_netsuite_brands": {
        "command": ["uv", "run", "python", "-m", "scripts.sync_netsuite_brands"],
        "description": "Sync brands from NetSuite API (--since YYYY-MM-DD or Nd e.g. 7d; default: full sync)",
        "args_allowed": ["--since", "--dry-run"],
        "long_running": False,
    },
    "sync_netsuite_products": {
        "command": ["uv", "run", "python", "-m", "scripts.sync_netsuite_products"],
        "description": "Sync products from NetSuite API (--since YYYY-MM-DD or Nd e.g. 7d; default: 30d)",
        "args_allowed": ["--since", "--dry-run"],
        "long_running": True,
    },
    "import_purchase_history": {
        "command": ["uv", "run", "python", "-m", "scripts.import_purchase_history"],
        "description": "Import purchase history from CSV files",
        "args_allowed": [],
        "long_running": False,
    },
    "categorize_suppliers": {
        "command": ["uv", "run", "python", "-m", "scripts.categorize_suppliers_job"],
        "description": "Categorize suppliers using search-grounded Gemini (default: uncategorized only)",
        "args_allowed": ["--force", "--limit", "--model", "--delay", "--dry-run"],
        "long_running": True,
    },
}


def get_script(name: str) -> dict | None:
    """Look up a script by name. Returns None if not registered."""
    return SCRIPT_REGISTRY.get(name)


def list_scripts() -> dict[str, dict]:
    """Return the full script registry."""
    return SCRIPT_REGISTRY


def validate_args(name: str, args: list[str]) -> list[str]:
    """Validate that all requested args are in the script's allowed list.

    Returns the validated args list.
    Raises ValueError if any arg is not allowed.
    """
    script = get_script(name)
    if script is None:
        raise ValueError(f"Unknown script: {name}")

    allowed = set(script.get("args_allowed", []))
    for arg in args:
        # Allow both the flag and its value (e.g. --phase 2)
        # Only validate tokens that start with -- as flags
        if arg.startswith("--") and arg not in allowed:
            raise ValueError(
                f"Argument '{arg}' is not allowed for script '{name}'. "
                f"Allowed: {sorted(allowed)}"
            )

    return args
