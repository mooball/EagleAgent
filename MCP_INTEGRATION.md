# MCP Server Integration

This document describes the MCP (Model Context Protocol) server integration added to EagleAgent.

## Overview

EagleAgent now supports connecting to MCP servers to extend its capabilities with external tools. The integration uses the official `langchain-mcp-adapters` library to automatically convert MCP tools into LangChain tools that work seamlessly with the existing LangGraph agent.

## Features

✅ **Multiple Transport Types**: STDIO, HTTP, and Server-Sent Events (SSE)  
✅ **Automatic Tool Discovery**: MCP tools automatically appear in the agent  
✅ **Environment Variable Interpolation**: Secure credential management via `${VAR_NAME}` syntax  
✅ **Graceful Degradation**: Agent works normally even if MCP servers are unavailable  
✅ **Configuration Validation**: Invalid configs are skipped with warnings  
✅ **OAuth Support**: Pass authentication tokens via headers (HTTP/SSE transports)  

## Architecture

### Components

1. **MCP Config Loader** ([includes/mcp_config.py](includes/mcp_config.py))
   - Loads YAML configuration from `config/mcp_servers.yaml`
   - Validates server configurations  
   - Interpolates environment variables
   - Returns config dict compatible with `MultiServerMCPClient`

2. **MCP Client Initialization** ([app.py](app.py))
   - Initializes `MultiServerMCPClient` at app startup
   - Global `mcp_client` variable (similar to `store`)
   - Graceful error handling if initialization fails

3. **Tool Integration** ([app.py](app.py))
   - `call_model()`: Fetches MCP tools + profile tools, binds to model
   - `call_tools()`: Executes both MCP and profile tools via ToolNode
   - Tools are fetched on each invocation (allows dynamic server updates)

### Data Flow

```
Startup:
  config/mcp_servers.yaml 
    → load_mcp_config() 
    → MultiServerMCPClient(config)
    → mcp_client (global)

Per Message:
  call_model():
    → mcp_client.get_tools()
    → merge with profile_tools
    → model.bind_tools(all_tools)
  
  call_tools():
    → mcp_client.get_tools()  
    → merge with profile_tools
    → ToolNode(all_tools).ainvoke(state)
```

## Configuration

### File Structure

**Location**: `config/mcp_servers.yaml`

**Format**:
```yaml
servers:
  server_name:
    transport: stdio | http | sse
    
    # STDIO transport (local process)
    command: npx
    args: ["-y", "@modelcontextprotocol/server-name"]
    env:  # Optional environment variables
      KEY: value
    
    # HTTP/SSE transport (remote server)
    url: https://api.example.com/mcp
    headers:  # Optional headers (e.g., auth)
      Authorization: "Bearer ${TOKEN}"
```

### Transport Types

1. **STDIO**: Spawns local process (e.g., Node.js MCP servers)
   - Requires: `command`, `args` (optional)
   - Example: `npx -y @modelcontextprotocol/server-filesystem /path`

2. **HTTP**: Connects to HTTP REST endpoint
   - Requires: `url`
   - Optional: `headers` for authentication

3. **SSE**: Connects to Server-Sent Events stream
   - Requires: `url`
   - Optional: `headers` for authentication

### Environment Variables

Use `${VAR_NAME}` syntax to reference environment variables (defined in `.env`):

```yaml
servers:
  github:
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_TOKEN: ${GITHUB_TOKEN}  # References .env variable
```

Missing variables log warnings but don't crash the app.

## Usage

### 1. Install MCP Server (Example: Filesystem)

```bash
# No installation needed - npx downloads on first run
# Or install globally:
npm install -g @modelcontextprotocol/server-filesystem
```

### 2. Configure Server

Edit `config/mcp_servers.yaml`:

```yaml
servers:
  filesystem:
    transport: stdio
    command: npx
    args:
      - "-y"
      - "@modelcontextprotocol/server-filesystem"
      - "/Users/tom/Documents"  # Allowed directory
```

### 3. Set Environment Variables (if needed)

Add to `.env`:
```bash
# For GitHub MCP server
GITHUB_TOKEN=ghp_your_token_here

# For HTTP MCP servers with auth
MCP_API_TOKEN=your_api_token
```

### 4. Start Agent

```bash
./run.sh
```

The MCP tools will automatically be available to the agent. Check logs for:
```
INFO:root:MCP client initialized with 1 server(s)
INFO:root:Loaded MCP server config: filesystem (stdio)
```

### 5. Use in Chat

The agent can now use MCP tools naturally:

**User**: "List the files in my Documents folder"  
**Agent**: *Uses filesystem MCP server's list_directory tool*

## Available MCP Servers

### Official Servers

- **@modelcontextprotocol/server-filesystem**: File system operations
- **@modelcontextprotocol/server-github**: GitHub API operations  
- **@modelcontextprotocol/server-postgres**: PostgreSQL database access
- **@modelcontextprotocol/server-slack**: Slack API operations
- **@modelcontextprotocol/server-puppeteer**: Web automation
- **@modelcontextprotocol/server-brave-search**: Web search
- **@modelcontextprotocol/server-google-maps**: Google Maps API

See full list: https://github.com/modelcontextprotocol/servers

### Custom Servers

You can connect to any MCP-compliant server (STDIO, HTTP, or SSE).

## OAuth Authentication

For MCP servers requiring OAuth (typically HTTP/SSE servers):

1. **Add credentials to .env**:
   ```bash
   MCP_CLIENT_ID=your_client_id
   MCP_CLIENT_SECRET=your_client_secret
   MCP_ACCESS_TOKEN=your_access_token
   ```

2. **Reference in config**:
   ```yaml
   servers:
     oauth_server:
       transport: http
       url: https://api.example.com/mcp
       headers:
         Authorization: "Bearer ${MCP_ACCESS_TOKEN}"
   ```

3. **Token Refresh**: Currently manual (add token to .env). Future: implement OAuth flow via Chainlit messages.

**Note**: STDIO servers don't support OAuth (local process, no network auth needed).

## Error Handling

### Startup Errors

If MCP client fails to initialize:
- Logs warning: `"Failed to initialize MCP client: <error>"`
- Agent continues WITHOUT MCP tools (profile tools still work)
- No impact on core functionality

### Runtime Errors

If MCP tool execution fails:
- Logs error: `"Failed to get MCP tools: <error>"`
- Agent continues with remaining tools
- User sees tool execution failure message

### Configuration Errors

Invalid server configs are skipped automatically:
- Missing required fields (transport, command/url)
- Invalid transport type
- Logged as warnings during startup

## Testing

Run the test suite:

```bash
# Simple integration test
uv run python test_mcp_simple.py

# Full test suite (requires Firestore emulator)
uv run pytest tests/test_mcp_integration.py -v
```

Test coverage:
- Empty configuration handling
- STDIO/HTTP/SSE config parsing
- Environment variable interpolation
- Validation and error cases
- Multiple server configs

## Troubleshooting

### "No MCP servers configured"

**Cause**: `config/mcp_servers.yaml` has empty `servers: {}` section  
**Fix**: Add server configurations (see examples above)

### "Failed to initialize MCP client"

**Cause**: YAML syntax error or invalid configuration  
**Fix**: Validate YAML with `uv run python -c "import yaml; yaml.safe_load(open('config/mcp_servers.yaml'))"`

### "Server 'X' skipped: Invalid transport"

**Cause**: Transport must be one of: `stdio`, `http`, `sse`  
**Fix**: Check spelling in config file

### "STDIO transport requires 'command' field"

**Cause**: STDIO server missing `command` field  
**Fix**: Add `command: npx` (or path to executable)

### "HTTP transport requires 'url' field"

**Cause**: HTTP/SSE server missing `url` field  
**Fix**: Add `url: https://...` to config

### npx downloads package every time

**Solution**: Install MCP server globally:
```bash
npm install -g @modelcontextprotocol/server-filesystem
```

Then update config to use direct command:
```yaml
command: /usr/local/bin/mcp-server-filesystem
args: ["/path"]
```

## Implementation Details

### Why langchain-mcp-adapters?

- **Official**: Maintained by LangChain team
- **Production Ready**: 2,100+ projects, actively maintained
- **Perfect Fit**: Designed for LangGraph StateGraph pattern
- **Auto-Conversion**: MCP tools → LangChain tools automatically
- **Multi-Transport**: STDIO, HTTP, SSE built-in
- **Battle-Tested**: Edge cases handled, well-documented

### Alternative Considered

Building a custom MCP client was considered but rejected because:
- Would take weeks vs hours with library
- Complex transport handling (STDIO process management, HTTP/SSE streams)
- Manual tool schema conversion (MCP JSON Schema → LangChain)
- Need to track MCP spec updates
- No community support

### Performance Notes

- Tool discovery: Cached by `MultiServerMCPClient` internally
- Connection: Long-lived (initialized at startup, not per-message)
- STDIO: Child processes spawned once, reused for all calls
- HTTP/SSE: Connection pooling handled by library

## Files Changed

- **app.py**: Added MCP client initialization and tool integration
- **includes/mcp_config.py**: New config loader utility (226 lines)
- **config/mcp_servers.yaml**: Runtime config (excluded from git)
- **config/mcp_servers.yaml.example**: Documentation and examples
- **.gitignore**: Added `config/mcp_servers.yaml` to exclude secrets
- **pyproject.toml**: Added `langchain-mcp-adapters==0.2.1` dependency
- **tests/test_mcp_integration.py**: Comprehensive test suite (260 lines)
- **test_mcp_simple.py**: Quick validation script

## Next Steps

1. **Add MCP Server Configs**: Edit `config/mcp_servers.yaml` with desired servers
2. **Set Credentials**: Add tokens/secrets to `.env` file  
3. **Test**: Run `uv run python test_mcp_simple.py`
4. **Deploy**: Start agent with `./run.sh`
5. **Monitor**: Check logs for successful MCP tool discovery

## Future Enhancements

- **Interactive OAuth**: Display auth URLs via Chainlit messages, handle callback
- **Server Health Checks**: Periodic connection validation, auto-reconnect
- **Tool Namespacing**: Prefix tools by server name (e.g., `github_create_issue`)
- **Dynamic Configuration**: Enable/disable servers without restart
- **Per-User Servers**: Allow users to connect their own MCP servers
- **Resource Protocol**: Support MCP resource URIs (not just tools)
- **Prompts/Sampling**: Support MCP prompts and sampling features

## References

- [MCP Specification](https://modelcontextprotocol.io)
- [langchain-mcp-adapters](https://github.com/langchain-ai/langchain-mcp-adapters)
- [Official MCP Servers](https://github.com/modelcontextprotocol/servers)
- [LangGraph Documentation](https://langchain-ai.github.io/langgraph/)
