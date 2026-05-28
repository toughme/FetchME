# OAuth Custom Protocol Handler Setup Guide

## Overview
This application uses the custom URI scheme `com.emclient.MailClient://oauth` for OAuth2 callback handling. This guide explains how to set it up.

## Step 1: Automatic Windows Protocol Handler Registration

When you run the GUI application for the first time, it will attempt to automatically register the Windows protocol handler:

```
HKEY_CLASSES_ROOT\com.emclient.MailClient
  Default: "URL:com.emclient.MailClient Protocol"
  URL Protocol: ""
  
HKEY_CLASSES_ROOT\com.emclient.MailClient\shell\open\command
  Default: "C:\Path\To\gui_mail_list_fetcher.py" --oauth-callback "%1"
```

**Note:** This requires administrator privileges. If the registration fails, you can manually register it (see Step 2).

## Step 2: Manual Windows Protocol Handler Registration (If Needed)

If automatic registration fails, manually add the registry entries:

### Via Registry Editor:

1. Open Registry Editor (`regedit.exe`)
2. Navigate to `HKEY_CLASSES_ROOT`
3. Create new key: `com.emclient.MailClient`
4. Set Default value: `URL:com.emclient.MailClient Protocol`
5. Create String value: `URL Protocol` = (empty)
6. Create key path: `com.emclient.MailClient\shell\open\command`
7. Set Default value to your app path:
   ```
   "C:\Path\To\gui_mail_list_fetcher.py" --oauth-callback "%1"
   ```
   Or with pythonw.exe for hidden console:
   ```
   "C:\Path\To\pythonw.exe" "C:\Path\To\gui_mail_list_fetcher.py" --oauth-callback "%1"
   ```

### Via PowerShell (Run as Administrator):

```powershell
$AppPath = "C:\Users\USER\Fetcher\gui_mail_list_fetcher.py"

# Create protocol handler registry entries
New-Item -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.emclient" -Force | Out-Null

$RegPath = "HKCU:\Software\Classes\com.emclient.MailClient"
New-Item -Path $RegPath -Force | Out-Null
Set-ItemProperty -Path $RegPath -Name "(Default)" -Value "URL:com.emclient.MailClient Protocol"
New-ItemProperty -Path $RegPath -Name "URL Protocol" -Value "" -Force | Out-Null

$CmdPath = "$RegPath\shell\open\command"
New-Item -Path $CmdPath -Force | Out-Null
Set-ItemProperty -Path $CmdPath -Name "(Default)" -Value "`"$AppPath`" --oauth-callback `"%1`""
```

## Step 3: Azure AD Application Configuration

You must configure your Azure AD application to accept the custom scheme as a redirect URI:

1. Go to [Azure Portal](https://portal.azure.com)
2. Navigate to **Azure Active Directory** → **App registrations**
3. Search for and select your application: `e9a7fea1-1cc0-4cd9-a31b-9137ca5deedd`
4. Go to **Manage** → **Authentication**
5. Under **Redirect URIs**, click **Add URI**
6. Add this redirect URI:
   ```
   com.emclient.MailClient://oauth
   ```
7. Click **Save**

Your redirect URIs should now include:
- `com.emclient.MailClient://oauth`
- `http://localhost:9000/oauth/callback` (for fallback if needed)

## Step 4: PKCE Configuration

The application automatically uses PKCE (Proof Key for Code Exchange) for enhanced security:

- **Code Verifier**: Random 128-byte value, URL-safe base64 encoded
- **Code Challenge**: SHA256 hash of verifier, URL-safe base64 encoded
- **Challenge Method**: S256 (SHA256)

This provides protection against authorization code interception.

## Step 5: State Validation

The application generates a CSRF protection state token for each OAuth request:

- Unique `state` parameter generated per request
- Validated on callback
- Stored temporarily in `%TEMP%\pkce_oauth_result_*.json`
- Cleaned up after use

## Step 6: Token Storage

OAuth tokens are stored securely:

- **Location**: `<app_dir>/.oauth_tokens/`
- **Permissions**: User-only read/write (700)
- **Format**: JSON with metadata
- **File naming**: `{PROVIDER}_{EMAIL}.json`

Example: `IMAP_user@example.com.json`

## OAuth Flow

### User Clicks "OAuth IMAP (Microsoft)"

1. App generates PKCE codes and state token
2. PKCE verifier stored temporarily
3. Browser opens with OAuth authorization URL
4. User signs in with Microsoft account
5. Microsoft redirects to: `com.emclient.MailClient://oauth?code=...&state=...`

### Windows Handles Callback

1. Windows recognizes protocol scheme
2. Launches app with: `--oauth-callback "com.emclient.MailClient://oauth?code=...&state=..."`
3. App parses callback URI
4. App validates state token (CSRF protection)
5. App exchanges code for access token using stored PKCE verifier
6. App saves token securely
7. Fetching begins automatically

## Troubleshooting

### "Protocol handler not registered"
- Run app as administrator for automatic registration
- Or manually register via Registry Editor or PowerShell (see Step 2)

### "State validation failed"
- Indicates possible CSRF attack or request timeout
- Try OAuth login again
- Check that app files haven't been moved

### "No authorization code in callback"
- User may have cancelled login
- Check Microsoft OAuth error in browser
- Try again

### "Code exchange failed"
- Token endpoint unreachable (check network)
- Client ID/Secret misconfigured
- Verify Azure AD app settings

### "Redirect URI mismatch" error from Azure
- Verify redirect URI added to Azure portal
- Check exact spelling: `com.emclient.MailClient://oauth`
- Wait a few minutes for Azure changes to propagate

## Security Considerations

1. **PKCE**: Protects against authorization code interception attacks
2. **State**: Prevents CSRF attacks on OAuth flow
3. **Token Storage**: Tokens stored with user-only file permissions
4. **No Hardcoding**: Client credentials not embedded in code
5. **Temporary Files**: PKCE files cleaned up after use
6. **HTTPS Only**: All Azure endpoints use TLS

## Advanced: Custom Application Registration

If you want to use your own Azure AD application:

1. Create app in Azure AD
2. Add redirect URI: `com.emclient.MailClient://oauth`
3. Get Client ID
4. Update `Setting.ini`:
   ```ini
   OauthClientId=YOUR_CLIENT_ID
   OauthAuthority=https://login.microsoftonline.com/common
   OauthRedirectUri=com.emclient.MailClient://oauth
   ```
5. Restart application

## Testing

To test the protocol handler registration:

1. Open PowerShell:
   ```powershell
   Start-Process "com.emclient.MailClient://oauth?code=test&state=test"
   ```

2. App should launch with `--oauth-callback` argument

3. Check logs for callback handling messages
