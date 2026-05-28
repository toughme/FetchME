# OAuth Implementation - Setup Checklist

## Before You Start

- [ ] Python 3.12+ installed
- [ ] All dependencies installed: `pip install -r requirements.txt`
- [ ] Administrator access to computer (for protocol handler registration)
- [ ] Active internet connection for OAuth flow

## Step 1: Install Dependencies ✓

```bash
pip install -r requirements.txt
```

Required packages:
- `msal>=1.0.0` - MSAL library for OAuth
- `pywebview>=5.0` - Embedded browser for OAuth UI
- `exchangelib>=5.0.0` - Exchange support
- `requests>=2.0.0` - HTTP requests

**Verify installation:**
```bash
python -c "import msal; print(msal.__version__)"
python -c "import webview; print(webview.__version__)"
```

## Step 2: Azure AD Configuration

### Create/Register Your Application

1. Go to [Azure Portal](https://portal.azure.com)
2. Select **Azure Active Directory**
3. Go to **App registrations**
4. Click **New registration**
5. Name: `Mail List Fetcher`
6. Accounts: `Accounts in any organizational directory and personal Microsoft accounts`
7. Redirect URI (leave empty for now)
8. Click **Register**

### Get Client ID

1. Copy the **Application (client) ID**
2. Save it (you'll need it later)

### Add Redirect URI

1. Go to **Manage** → **Authentication**
2. Click **Add a platform**
3. Select **Mobile and desktop applications**
4. Check: `https://login.microsoftonline.com/common/oauth2/nativeclient`
5. Click **Configure** (or add custom)
6. Add custom URI: `com.emclient.MailClient://oauth`
7. Click **Save**

### Required API Permissions

1. Go to **Manage** → **API permissions**
2. Click **Add a permission**
3. Select **Microsoft Graph**
4. Click **Delegated permissions**
5. Search and add:
   - `Mail.Read` - Read mail
   - `Mail.ReadBasic` - Read basic mail properties
6. Click **Add permissions**
7. **Grant admin consent** (if applicable in your organization)

## Step 3: Protocol Handler Registration

### Option A: Automatic (Recommended)

1. Run the application:
   ```bash
   python gui_mail_list_fetcher.py
   ```
2. Look for message: `OAuth protocol handler registered successfully`
3. If you see it, you're done! Skip to Step 4

### Option B: Manual (If Automatic Failed)

Run as Administrator:

```powershell
# Run PowerShell as Administrator
$AppPath = "C:\Users\USER\Fetcher\gui_mail_list_fetcher.py"
$RegPath = "HKCU:\Software\Classes\com.emclient.MailClient"

New-Item -Path $RegPath -Force | Out-Null
Set-ItemProperty -Path $RegPath -Name "(Default)" -Value "URL:com.emclient.MailClient Protocol"
New-ItemProperty -Path $RegPath -Name "URL Protocol" -Value "" -Force | Out-Null

$CmdPath = "$RegPath\shell\open\command"
New-Item -Path $CmdPath -Force | Out-Null
Set-ItemProperty -Path $CmdPath -Name "(Default)" -Value "`"$AppPath`" --oauth-callback `"%1`""

Write-Host "Protocol handler registered successfully"
```

### Option C: Manual Registry Editor

1. Open Registry Editor (`Win+R` → `regedit`)
2. Navigate to: `HKEY_CLASSES_ROOT`
3. Create new key: `com.emclient.MailClient`
4. Right-click → **New** → **String Value**
5. Name: (Default), Value: `URL:com.emclient.MailClient Protocol`
6. Right-click → **New** → **String Value**
7. Name: `URL Protocol`, Value: (leave empty)
8. Right-click → **New** → **Key**
9. Name: `shell`
10. Under `shell` → **New** → **Key**
11. Name: `open`
12. Under `open` → **New** → **Key**
13. Name: `command`
14. Under `command` → Double-click (Default)
15. Value: `"C:\Users\USER\Fetcher\gui_mail_list_fetcher.py" --oauth-callback "%1"`

## Step 4: Update Configuration

### Update Your Client ID

1. Open `Setting.ini`
2. Update OAuth client ID:
   ```ini
   OauthClientId=YOUR_CLIENT_ID_HERE
   ```
3. Verify redirect URI:
   ```ini
   OauthRedirectUri=com.emclient.MailClient://oauth
   ```

## Step 5: Test OAuth Flow

1. Start the application:
   ```bash
   python gui_mail_list_fetcher.py
   ```

2. Enter your email: `sbaldwin@timbermart-south.com`

3. Leave password empty

4. Click **"OAuth IMAP (Microsoft)"** button

5. **Expected Flow:**
   - Browser opens for Microsoft sign-in
   - You sign in with your Office 365 account
   - Redirected to OAuth confirmation page
   - Browser redirect URL shows: `com.emclient.MailClient://oauth?code=...`
   - Windows launches app with callback
   - App extracts authorization code
   - App exchanges code for access token
   - App shows success message
   - Fetch starts automatically

## Step 6: Verify Success

After successful OAuth:

1. Check GUI shows: `OAuth: IMAP token active` (green)
2. Check `.oauth_tokens` directory:
   ```
   C:\Users\USER\Fetcher\.oauth_tokens\
   ```
3. Should contain file: `IMAP_sbaldwin@timbermart-south.com.json`
4. Token automatically used for email fetching

## Troubleshooting

### "OAuth helper exited with code 1"
- **Cause**: Missing dependencies
- **Fix**: `pip install msal pywebview --upgrade`

### "msal is not defined"
- **Cause**: MSAL not installed
- **Fix**: `pip install msal --upgrade`

### "webview module not available"
- **Cause**: pywebview not installed
- **Fix**: `pip install pywebview --upgrade`

### "Redirect URI mismatch" (AADSTS50011)
- **Cause**: Redirect URI not registered in Azure
- **Fix**: 
  1. Go to Azure portal
  2. Add `com.emclient.MailClient://oauth` to Authentication settings
  3. Wait 2-3 minutes for changes to propagate
  4. Retry OAuth

### "Protocol handler not registered"
- **Cause**: Admin privileges not available
- **Fix**: 
  1. Close application
  2. Run PowerShell as Administrator
  3. Run manual registration script (see Step 3, Option B)
  4. Restart application

### "No pending OAuth request found"
- **Cause**: App closed during OAuth flow
- **Fix**: Restart application and try OAuth again

### "State validation failed"
- **Cause**: PKCE verifier file deleted
- **Fix**: 
  1. Clear `%TEMP%\pkce_oauth_result_*.json` files
  2. Try OAuth again

## After Setup - Usage

### First Time
1. Click "OAuth IMAP (Microsoft)"
2. Complete sign-in
3. Token saved automatically
4. Fetch starts

### Subsequent Times
1. Click "OAuth IMAP (Microsoft)"
2. Token loaded from storage
3. Fetch starts immediately
4. Or click "Start Fetch" with OAuth token active

## Security Notes

- **Tokens**: Stored securely in `.oauth_tokens/` with user-only permissions
- **PKCE**: Prevents authorization code interception
- **State**: Prevents CSRF attacks
- **Never**: Share your client ID or tokens
- **Refresh**: Tokens automatically refreshed when expired

## Files Created/Modified

After setup, you'll have:

```
C:\Users\USER\Fetcher\
├── .oauth_tokens/                    (NEW: Token storage)
│   └── IMAP_*.json
├── gui_mail_list_fetcher.py         (MODIFIED: OAuth implementation)
├── mail_list_fetcher.py             (MODIFIED: OAuth config)
├── Setting.ini                      (MODIFIED: OAuth settings)
└── OAUTH_*.md                       (NEW: Setup documentation)
```

## Next Steps

1. ✓ Check off each step as you complete it
2. Test OAuth flow (Step 5)
3. Use application normally
4. Tokens handled automatically
5. Refer to documentation if issues arise

## Support Resources

- `OAUTH_PROTOCOL_SETUP.md` - Detailed protocol handler setup
- `OAUTH_IMPLEMENTATION_GUIDE.md` - Complete implementation details
- `OAUTH_FIX_SUMMARY.md` - Error handling improvements
- Application log output - Check GUI for detailed messages

## Quick Reference: Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run application
python gui_mail_list_fetcher.py

# Run with debug output
python -u gui_mail_list_fetcher.py 2>&1 | tee debug.log

# Test protocol handler
Start-Process "com.emclient.MailClient://oauth?code=test&state=test"
```

---

**Status**: Setup checklist complete when all boxes are checked ✓

For additional help, check the included documentation files.
