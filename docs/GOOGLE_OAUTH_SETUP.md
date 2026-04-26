# Google OAuth Setup Guide

This guide walks you through setting up Google OAuth authentication for EagleAgent.

## Step 1: Generate Chainlit Auth Secret

Run this command to generate a secure authentication secret:

```bash
uv run chainlit create-secret
```

Copy the generated secret and add it to your `.env` file as `CHAINLIT_AUTH_SECRET`.

## Step 2: Create Google OAuth Credentials

1. Go to the [Google Cloud Console](https://console.developers.google.com/apis/credentials)

2. Select your project (or create a new one if needed)

3. Click **"Create Credentials"** → **"OAuth 2.0 Client ID"**

4. If prompted, configure the OAuth consent screen:
   - **User Type**: External (for testing) or Internal (if using Google Workspace)
   - **App name**: EagleAgent
   - **User support email**: Your email
   - **Developer contact**: Your email
   - **Scopes**: Add the following scopes:
     - `.../auth/userinfo.email`
     - `.../auth/userinfo.profile`
     - `openid`
   - Click **Save and Continue**

5. Create OAuth 2.0 Client ID:
   - **Application type**: Web application
   - **Name**: EagleAgent (or any name you prefer)
   - **Authorized redirect URIs**: 
     - For local development: `http://localhost:8000/auth/google/callback`
     - For production: `https://yourdomain.com/auth/google/callback`
   - Click **Create**

6. Copy the **Client ID** and **Client Secret** that are displayed

## Step 3: Update Environment Variables

Add these variables to your `.env` file:

```bash
# Chainlit Authentication Secret (from Step 1)
CHAINLIT_AUTH_SECRET=your_generated_secret_here

# Google OAuth Credentials (from Step 2)
OAUTH_GOOGLE_CLIENT_ID=your_client_id_here.apps.googleusercontent.com
OAUTH_GOOGLE_CLIENT_SECRET=your_client_secret_here

# OAuth Domain Restriction (Optional)
# Comma-separated list of allowed Google Workspace domains
# Only users from these domains will be able to authenticate
# Leave empty or comment out to allow all Google accounts (including personal Gmail)
OAUTH_ALLOWED_DOMAINS=mooball.com,eagle-exports.com

# Only needed if running behind a reverse proxy (production)
# CHAINLIT_URL=https://yourdomain.com
```

## Step 4: Test Authentication

1. Start the application:
   ```bash
   ./run.sh
   ```

2. Open your browser to `http://localhost:8000`

3. You should see a "Sign in with Google" button

4. Click it and authenticate with your Google account

5. After successful authentication, you'll be redirected back to the chat interface

## Optional: Restrict to Specific Domain(s)

**Domain restriction is now configured via environment variable!**

To restrict authentication to specific Google Workspace domains, set `OAUTH_ALLOWED_DOMAINS` in your `.env` file:

```bash
# Single domain
OAUTH_ALLOWED_DOMAINS=yourcompany.com

# Multiple domains (comma-separated)
OAUTH_ALLOWED_DOMAINS=mooball.com,eagle-exports.com,partner.com
```

**How it works:**
- Only users with email addresses from the specified domains can authenticate
- Personal Gmail accounts (@gmail.com) will be **rejected** when domain restriction is enabled
- The `hd` (hosted domain) field from Google OAuth is checked against the allowed list
- Leave `OAUTH_ALLOWED_DOMAINS` empty or unset to allow all Google accounts

**Example scenarios:**
- `OAUTH_ALLOWED_DOMAINS=mooball.com` - Only @mooball.com users allowed
- `OAUTH_ALLOWED_DOMAINS=` (empty) - All Google users allowed (including Gmail)
- Commented out or not set - All Google users allowed

## Railway Production Setup

When deploying to Railway, you need to update the OAuth redirect URIs to include your Railway service URL.

### Initial Setup

1. During initial deployment, the Railway URL is dynamically generated
2. Add a temporary redirect URI for local development first
3. Deploy to Railway and get the service URL
4. Update Google Console with the Railway redirect URI

### Getting Your Railway URL

After deploying to Railway:

1. Go to your Railway project dashboard
2. Click on the EagleAgent service
3. Find the service URL under "Settings" → "Networking" → "Public Networking"
4. Example: `https://eagleagent-production.up.railway.app`

### Update OAuth Redirect URIs

1. Go to [Google Cloud Console Credentials](https://console.developers.google.com/apis/credentials)
2. Click on your OAuth 2.0 Client ID
3. Under **Authorized redirect URIs**, add:
   ```
   https://your-railway-url.up.railway.app/auth/google/callback
   ```
   Replace with your actual Railway URL

4. **Important**: 
   - Railway URLs are **permanent** and don't change between deployments
   - Each Railway region/service has a unique URL
   - Wildcard URIs (e.g., `https://*.run.app/...`) are **not supported** by Google OAuth
   - You must add the exact full URL

### Custom Domain (Optional)

If you map a custom domain to Railway:

1. Go to Railway → Service → Settings → Networking → Custom Domain
2. Add your domain (e.g., `app.yourdomain.com`)
3. Configure DNS as directed by Railway

Add the custom domain redirect URI:
```
https://app.yourdomain.com/auth/google/callback
```

### Environment Variables for Railway

Ensure these are set in Railway:

```bash
CHAINLIT_URL=https://your-railway-url.up.railway.app
OAUTH_GOOGLE_CLIENT_ID=your_client_id
OAUTH_GOOGLE_CLIENT_SECRET=your_client_secret
OAUTH_ALLOWED_DOMAINS=yourdomain.com
```

See the [Development Workflow](DEVELOPMENT_WORKFLOW.md) for deployment instructions.

## Troubleshooting

### "Redirect URI mismatch" error
- Make sure the redirect URI in Google Console exactly matches your app URL
- For local development: `http://localhost:8000/auth/google/callback`
- Don't forget the `/auth/google/callback` path

### "Access blocked: This app's request is invalid"
- Check that you've configured the OAuth consent screen
- Make sure required scopes are added (email, profile, openid)
- Add your email as a test user if the app is in testing mode

### Authentication rejected / can't log in
- **Check domain restriction**: If `OAUTH_ALLOWED_DOMAINS` is set, ensure your Google account's domain is in the list
- Personal Gmail accounts won't have a hosted domain (`hd` field) and will be rejected when domain restriction is enabled
- Check application logs for "Authentication rejected" message showing which domain was attempted
- Verify `.env` file is loaded correctly: `grep OAUTH_ALLOWED_DOMAINS .env`

### Users can't see chat history
- Chat history requires both authentication AND a data layer
- The PostgreSQL data layer is configured automatically via `app.py`

## Next Steps

Once authentication is working, you can:
1. Set up a data layer (PostgreSQL or custom PostgreSQL) for conversation persistence
2. Implement `@cl.on_chat_resume` to restore conversation state
3. Deploy to production with proper CHAINLIT_URL configuration
