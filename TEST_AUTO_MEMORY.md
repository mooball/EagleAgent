# Quick Test Guide: Auto-Save Memory

## 🎯 Quick Test (2 minutes)

### Test 1: Save Information Naturally
1. **Go to**: http://localhost:8000
2. **Say**: "My name is Tom and I love Python programming"
3. **Expected**: Agent acknowledges and saves this information
4. **Verify**:
   ```bash
   uv run manage_user_profile.py get tom@mooball.net
   ```
   Should show your name and preference

### Test 2: Cross-Thread Memory
1. **Click**: "New Chat" (start a new conversation thread)
2. **Say**: "What's my name?"
3. **Expected**: Agent responds "Your name is Tom!" ✨
4. **Say**: "What do you know about me?"
5. **Expected**: Agent mentions your love of Python

### Test 3: Update Information
1. **Say**: "Actually, I also love AI and machine learning"
2. **Expected**: Agent saves additional preferences
3. **Verify**:
   ```bash
   uv run manage_user_profile.py get tom@mooball.net
   ```
   Should show both Python and AI/ML in preferences

## 🔍 Debug: See Tool Calls in Action

If you want to watch the agent call tools in real-time:

```bash
# In another terminal
./kill.sh
./run.sh  # Run in foreground to see logs
```

Then say: "My favorite color is blue"

Look for output like:
```
ToolCall(name='remember_user_info', args={'category': 'preferences', 'information': 'favorite color is blue'})
```

## ✅ What Should Happen

### ✅ Success Indicators:
- Agent acknowledges saving information ("I'll remember that", "Got it", etc.)
- New threads have your information loaded
- Verify command shows stored data
- Tool calls visible in logs (if running in foreground)

### ❌ If It's Not Working:
1. **Check app is running**: Visit http://localhost:8000
2. **Check you're logged in**: Should see your email in top-right
3. **Try more explicit phrasing**: "Remember that my name is Tom"
4. **Check terminal for errors**: Look for tool_call or error messages
5. **Test manual management**:
   ```bash
   # If auto-save isn't working, manual should still work
   uv run manage_user_profile.py set YOUR_EMAIL name "Tom"
   uv run manage_user_profile.py get YOUR_EMAIL
   ```

## 📝 Example Conversation

```
You: Hi! My name is Tom.
Agent: Nice to meet you, Tom! I'll remember that. 
      [Internally: calls remember_user_info("name", "Tom")]

You: I'm really into Python programming and AI.
Agent: Great! I've noted your interests in Python and AI.
       [Internally: calls remember_user_info("preferences", "Python programming")]
       [Internally: calls remember_user_info("preferences", "AI")]

--- START NEW CHAT ---

You: What do you know about me?
Agent: Your name is Tom, and you're interested in Python programming and AI.

You: What's my name?
Agent: Your name is Tom!
```

## 🛠 Manual Commands Reference

```bash
# View your profile
uv run manage_user_profile.py get YOUR_EMAIL

# Set data manually
uv run manage_user_profile.py set YOUR_EMAIL name "Tom"

# Delete specific field
uv run manage_user_profile.py delete YOUR_EMAIL name

# List all profiles
uv run manage_user_profile.py list
```

## 🎓 Information Categories

The agent automatically categorizes information:

- **name**: Your name
- **preferences**: Things you like/dislike
- **facts**: General facts about you (job, location, hobbies)
- **Custom fields**: Anything else (job, location, pet_name, etc.)

Examples:
- "My name is Tom" → `name`
- "I love Python" → `preferences`
- "I work at MooBall" → `facts`
- "I live in SF" → could be `location` or `facts`
