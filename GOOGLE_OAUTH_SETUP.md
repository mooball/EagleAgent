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
     - For local development: `http://localhost:8000/auth/oauth/google/callback`
     - For production: `https://yourdomain.com/auth/oauth/google/callback`
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

## Optional: Restrict to Specific Domain

To only allow users from a specific Google Workspace domain, edit the `oauth_callback` function in `app.py`:

```python
@cl.oauth_callback
def oauth_callback(
    provider_id: str,
    token: str,
    raw_user_data: Dict[str, str],
    default_user: cl.User,
) -> Optional[cl.User]:
    if provider_id == "google":
        # Only allow users from yourdomain.com
        if raw_user_data.get("hd") == "yourdomain.com":
            return default_user
        return None  # Reject users from other domains
    
    return None
```

## Troubleshooting

### "Redirect URI mismatch" error
- Make sure the redirect URI in Google Console exactly matches your Chainlit URL
- For local development: `http://localhost:8000/auth/oauth/google/callback`
- Don't forget the `/auth/oauth/google/callback` path

### "Access blocked: This app's request is invalid"
- Check that you've configured the OAuth consent screen
- Make sure required scopes are added (email, profile, openid)
- Add your email as a test user if the app is in testing mode

### Users can't see chat history
- Chat history requires both authentication AND a data layer
- See `README.md` for data layer setup (coming soon)

## Next Steps

Once authentication is working, you can:
1. Set up a data layer (PostgreSQL or custom Firestore) for conversation persistence
2. Implement `@cl.on_chat_resume` to restore conversation state
3. Deploy to production with proper CHAINLIT_URL configuration
