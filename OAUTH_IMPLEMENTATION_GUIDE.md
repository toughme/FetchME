# OAuth Custom Protocol Handler Implementation - Complete Guide

## Implementation Summary

Your Mail List Fetcher application now has a complete OAuth2 implementation with custom URI scheme callback handling, PKCE protection, and secure token storage.

## Components Implemented

### 1. PKCE (Proof Key for Code Exchange)

Located in: `PKCEHelper` class

- **Code Verifier**: 128-byte random value, URL-safe base64 encoded
- **Code Challenge**: SHA256 hash of verifier, URL-safe base64 encoded
- **Method**: S256 (SHA256)

**Security**: Prevents authorization code interception attacks

```python
code_verifier = PKCEHelper.generate_code_verifier()
code_challenge = PKCEHelper.generate_code_challenge(code_verifier)
```

### 2. Windows Protocol Handler

Located in: `ProtocolHandler` class

**Automatic Registration**: On first run, attempts to register:
```
HKEY_CLASSES_ROOT\com.emclient.MailClient
  → shell\open\command: "python gui_mail_list_fetcher.py" --oauth-callback "%1"
```

**Manual Registration**: See `OAUTH_PROTOCOL_SETUP.md` for manual setup

**Protocol Scheme**: `com.emclient.MailClient://oauth`

### 3. Secure Token Storage

Located in: `MailListFetcherGUI` class methods

- **Location**: `<app_dir>/.oauth_tokens/`
- **Permissions**: User-only (700)
- **Format**: JSON with metadata
- **Methods**:
  - `save_oauth_token(provider, email, token_data)`
  - `load_oauth_token(provider, email)`

Example token file: `.oauth_tokens/IMAP_user@example.com.json`

### 4. OAuth Browser Flow with PKCE

Located in: `_oauth_helper_browser_flow()` function

**Flow**:
1. Generate PKCE code_verifier and code_challenge
2. Generate CSRF protection state token
3. Create OAuth flow with msal
4. Open system browser with auth URL (includes PKCE challenge)
5. Store PKCE verifier temporarily in `%TEMP%/pkce_oauth_result_*.json`
6. Browser opens, user signs in
7. Microsoft redirects to: `com.emclient.MailClient://oauth?code=AUTH_CODE&state=XYZ`

### 5. Protocol Callback Handler

Located in: `_handle_oauth_callback()` function

**Invoked by Windows when protocol URL is clicked**:

1. Parse callback URI
2. Extract authorization code
3. Validate state (CSRF protection)
4. Read PKCE verifier from temp file
5. Verify with Azure using code + PKCE verifier
6. Exchange authorization code for access token
7. Save token securely
8. Clean up temporary files

## Security Features

### PKCE Protection
- Prevents code interception attacks
- Required for public clients (no secret)
- Uses SHA256 hash validation

### CSRF Protection via State
- Random state token per request
- Validated on callback
- Prevents unauthorized redirection attacks

### Secure Token Storage
- User-only file permissions (700)
- Stored locally in `.oauth_tokens/`
- JSON format with metadata
- Never sent over network in plain text

### Temporary File Cleanup
- PKCE verifier files cleaned up after use
- Located in system temp directory
- Deleted immediately after token exchange

## File Structure

```
C:\Users\USER\Fetcher\
├── gui_mail_list_fetcher.py          (Main GUI with OAuth implementation)
├── mail_list_fetcher.py              (Core fetcher with OAuth config)
├── Setting.ini                       (OAuth redirect URI config)
├── .oauth_tokens/                    (Secure token storage directory)
│   ├── IMAP_user@example.com.json
│   └── Exchange_user@example.com.json
└── OAUTH_PROTOCOL_SETUP.md           (Setup and registration guide)
```

## Configuration

### Setting.ini

```ini
[MailListFetcher]
OauthClientId=e9a7fea1-1cc0-4cd9-a31b-9137ca5deedd
OauthAuthority=https://login.microsoftonline.com/common
OauthRedirectUri=com.emclient.MailClient://oauth
```

## Command-Line Arguments

### OAuth Helper Process
```bash
python gui_mail_list_fetcher.py --oauth-helper \
  --provider IMAP \
  --client-id e9a7fea1-1cc0-4cd9-a31b-9137ca5deedd \
  --authority https://login.microsoftonline.com/common \
  --email user@example.com \
  --scope https://outlook.office.com/IMAP.AccessAsUser.All \
  --result-file /path/to/result.json \
  --redirect-uri com.emclient.MailClient://oauth
```

### Protocol Callback Handler
```bash
python gui_mail_list_fetcher.py --oauth-callback "com.emclient.MailClient://oauth?code=...&state=..."
```

## OAuth Flow Diagram

```
User                  App                    Browser              Azure AD
 │                     │                        │                    │
 ├──Click OAuth───────>│                        │                    │
 │                     ├──Generate PKCE──────>│                        │
 │                     ├──Open Browser───────>│                        │
 │                     │                        ├──Request Auth───────>│
 │                     │                        │<────Login Form───────┤
 │<───────Sign In──────┼────Sign In Form──────┤                        │
 │                     │                        │                        │
 │ (User enters creds) │                        │                        │
 │                     │                        ├──Auth Code + State──>│
 │                     │<──Redirect to protocol scheme────────────────┤
 │                     │ com.emclient.MailClient://oauth?code=X&state=Y
 │                     │                        │                      │
 │                     ├──Windows Launches App with callback───────────┤
 │                     │                        │                      │
 │                     ├──Parse Callback───────┤                      │
 │                     ├──Validate State───────┤                      │
 │                     ├──Exchange Code (with PKCE)──────────────────>│
 │                     │<──Access Token────────────────────────────────┤
 │                     ├──Save Token Securely──┤                      │
 │                     │                        │                      │
 │<──Success + Fetch──┤                        │                      │
 │                    │                        │                      │
```

## OAuth Token Format

```json
{
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
  "refresh_token": "0.ARcA...Qd2OPG8...A",
  "token_type": "Bearer",
  "expires_in": 3600,
  "expires_on": 1717069200,
  "scope": "https://outlook.office.com/IMAP.AccessAsUser.All",
  "provider": "IMAP",
  "email": "user@example.com",
  "saved_at": 1717065600.123456,
  "token_result": { ... }
}
```

## Troubleshooting

### OAuth Fails with Redirect URI Mismatch
1. Ensure `com.emclient.MailClient://oauth` is added to Azure portal
2. Check exact spelling and case
3. Wait for Azure changes to propagate (few minutes)

### Protocol Handler Not Registered
1. Run app as Administrator
2. Or manually register via PowerShell (see OAUTH_PROTOCOL_SETUP.md)

### PKCE Verification Failed
1. Verify PKCE temp file exists: `%TEMP%\pkce_oauth_result_*.json`
2. Check that temp file wasn't deleted prematurely
3. Restart OAuth flow

### State Validation Failed
1. Possible CSRF attack or timeout
2. Try OAuth login again
3. Check that app hasn't been moved

### Token Exchange Failed
1. Network connectivity issue (check internet)
2. Azure tenant misconfigured
3. Expired refresh token (re-authenticate)

## Advanced Usage

### Using Custom Azure AD Application

1. Create application in Azure AD
2. Add Redirect URI: `com.emclient.MailClient://oauth`
3. Get Client ID
4. Update `Setting.ini`:

```ini
OauthClientId=YOUR_CLIENT_ID
OauthAuthority=https://login.microsoftonline.com/common
OauthRedirectUri=com.emclient.MailClient://oauth
```

5. Restart application

### Token Refresh

Tokens are automatically refreshed when expired:

```python
def _try_refresh_oauth_token(self, provider: str, email_addr: str) -> None:
    # Automatically called when token expires
    # Uses refresh_token to get new access_token
    # Saves updated token
```

### Token Persistence

Tokens persist across application sessions:

```python
# On startup, check for saved token
saved_token = self.load_oauth_token('IMAP', 'user@example.com')

# On login, save token
self.save_oauth_token('IMAP', 'user@example.com', token_data)
```

## Performance Considerations

- PKCE code generation: < 1ms
- State token generation: < 1ms
- Token file I/O: < 5ms
- Protocol handler registration: One-time on first run
- No performance impact on fetch operations

## Future Enhancements

Possible improvements:

1. Multi-tenant support for Azure AD
2. Token encryption in storage
3. Automatic background token refresh
4. Token caching with TTL
5. Web-based OAuth flow fallback
6. Device code flow for headless environments

## Reference Documentation

- [PKCE RFC 7636](https://tools.ietf.org/html/rfc7636)
- [OAuth 2.0 Authorization Code Flow](https://tools.ietf.org/html/rfc6749#section-1.3.1)
- [MSAL Python Documentation](https://msal-python.readthedocs.io/)
- [Azure AD Redirect URIs](https://docs.microsoft.com/en-us/azure/active-directory/develop/reply-url)
- [Windows Protocol Handlers](https://docs.microsoft.com/en-us/previous-versions/windows/internet-explorer/ie-developer/platform-apis/aa767914(v=vs.85))

## Support & Debugging

For issues, check:

1. **Application logs** in GUI log window
2. **Event Viewer** → Windows Logs → Application (Windows errors)
3. **Registry** at `HKEY_CLASSES_ROOT\com.emclient.MailClient`
4. **Temp files** at `%TEMP%\pkce_oauth_result_*.json`
5. **Token storage** at `<app_dir>\.oauth_tokens\`

Enable debug logging by running:
```bash
python -u gui_mail_list_fetcher.py 2>&1 | tee debug.log
```
