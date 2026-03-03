# Plan: Google Workspace MCP Integration (Single OAuth User)

**Recommended Approach**: Use **taylorwilsdon/google-workspace-mcp** with one-time OAuth authentication for a dedicated agent Google Workspace account. After initial browser-based login, credentials are persisted and auto-refresh indefinitely—no service accounts, no domain-wide delegation, no forking required.

This leverages the server's built-in `MCP_SINGLE_USER_MODE` designed exactly for your scenario: one agent account, human performs OAuth once, then fully automated thereafter. The server handles token refresh automatically using stored refresh tokens.

## Steps

### 1. Create Google Cloud OAuth Credentials

- Go to [Google Cloud Console](https://console.cloud.google.com) → APIs & Services → Credentials
- Enable APIs: Gmail API, Google Drive API, Google Calendar API, Google Docs API
- Create OAuth 2.0 Client ID (Application type: Desktop app)
- Download client secrets JSON or copy Client ID and Client Secret
- Add to `.env`: `GOOGLE_WORKSPACE_OAUTH_CLIENT_ID` and `GOOGLE_WORKSPACE_OAUTH_CLIENT_SECRET`
- Update `.env.example` with these new variables as documentation

### 2. Install google-workspace-mcp Server

- Install using uv (already your package manager): `uv pip install google-workspace-mcp` OR use npx to run it directly
- Alternatively, clone the repo if you want to run from source: `git clone https://github.com/taylorwilsdon/google-workspace-mcp.git`
- Server will be invoked via STDIO transport by EagleAgent's MCP client

### 3. Configure MCP Server

- Add configuration to `config/mcp_servers.yaml`:
  ```yaml
  google_workspace:
    transport: stdio
    command: uv
    args:
      - run
      - google-workspace-mcp
    env:
      GOOGLE_OAUTH_CLIENT_ID: "${GOOGLE_WORKSPACE_OAUTH_CLIENT_ID}"
      GOOGLE_OAUTH_CLIENT_SECRET: "${GOOGLE_WORKSPACE_OAUTH_CLIENT_SECRET}"
      MCP_SINGLE_USER_MODE: "1"
      WORKSPACE_MCP_TOOL_TIER: "core"
      WORKSPACE_MCP_CREDENTIALS_DIR: "${HOME}/.google_workspace_mcp/credentials"
  ```
- Update `config/mcp_servers.yaml.example` with commented example
- Update `.gitignore` to exclude credentials directory: `.google_workspace_mcp/`

### 4. Perform One-Time OAuth Authentication

- Start EagleAgent: `./run.sh`
- On first MCP tool invocation, the server will output an OAuth URL to the terminal
- Open the URL in a browser, log in as your dedicated agent account (e.g., `agent@yourdomain.com`)
- Grant all requested permissions (Gmail, Drive, Calendar, Docs)
- Complete OAuth flow - credentials stored to `~/.google_workspace_mcp/credentials/agent@yourdomain.com.json`
- Subsequent starts automatically use stored refresh token (no browser needed)

### 5. Update System Prompt for Google Workspace Capabilities

- In `includes/prompts.py`, add to `AGENT_CONFIG['capabilities']` list:
  - "Access Google Workspace (Gmail, Drive, Calendar, Docs) for the agent account"
- Add to `TOOL_INSTRUCTIONS` dictionary with guidance on when to use Gmail/Calendar/Drive tools
- Document that the agent has access to a specific workspace account, not user accounts

### 6. Create Documentation

- Create `GOOGLE_WORKSPACE_MCP_SETUP.md` in root documenting:
  - OAuth credentials setup in Google Cloud Console
  - Initial authentication workflow
  - Token refresh behavior (automatic, no expiry)
  - Available tools by tier (core/extended/complete)
  - Troubleshooting (token revocation, re-authentication)

## Verification

- **Configuration Test**: Run existing `tests/test_mcp_integration.py` - should load google_workspace server config
- **Manual Authentication**: Start app, trigger OAuth flow, verify credentials saved to `~/.google_workspace_mcp/credentials/`
- **Tool Availability**: After auth, ask agent "what emails do I have?" - should invoke Gmail search tool
- **Persistence**: Restart app without OAuth flow - tools should work immediately (refresh token used)
- **Token Refresh**: Wait 1 hour, use tools again - should automatically refresh access token

## Decisions

- **Chose OAuth over Service Accounts**: One-time browser login is simpler than domain-wide delegation setup; perfect for single agent account use case
- **Chose STDIO transport**: Simpler than HTTP (no separate server process), works with existing `MultiServerMCPClient` in `includes/mcp_config.py`
- **Chose `core` tier initially**: Limits scope permissions to essential tools (can expand to `extended` or `complete` later if needed)
- **No forking required**: Server's existing OAuth implementation perfectly matches requirements - stored refresh tokens auto-renew indefinitely
