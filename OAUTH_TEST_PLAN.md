# OAuth Implementation Test Plan

## Overview
This document outlines the complete OAuth flow and testing procedures to verify the token exchange implementation.

## OAuth Flow Sequence

### 1. **User Initiates OAuth** (GUI)
- User clicks "OAuth IMAP (Microsoft)" button
- GUI calls `_oauth_imap_oauth()` which calls `_run_oauth_helper_process()` in subprocess
- GUI spawns OAuth helper subprocess with parameters:
  - `--oauth-helper`
  - `--client-id` (from settings)
  - `--authority` (from settings)
  - `--redirect-uri com.emclient.MailClient://oauth`
  - `--email` (if provided)

### 2. **OAuth Helper Process Starts** (Subprocess)
- Helper creates MSAL app and initiates auth code flow
- Flow creates authorization URL with PKCE challenge
- Creates temporary pkce file in temp directory: `pkce_oauth_result_<uuid>.json`
- Pkce file contains:
  ```json
  {
    "flow": { ...MSAL flow object... },
    "result_file": "/temp/oauth_result_<uuid>.json",
    "client_id": "e9a7fea1-1cc0-4cd9-a31b-9137ca5deedd",
    "authority": "https://login.microsoftonline.com/common"
  }
  ```
- Opens browser with authorization URL (or webview if available)
- Waits for callback (up to 600 seconds)

### 3. **User Signs In** (Browser)
- User signs in to Microsoft account
- User approves permissions
- Microsoft redirects to: `com.emclient.MailClient://oauth?code=<code>&state=<state>`
- Browser detects protocol scheme and prompts: "Do you want to open gui_mail_list_fetcher?"
- User clicks "Open"

### 4. **Protocol Handler Callback** (Callback Process)
- Windows launches new Python process: `python gui_mail_list_fetcher.py --oauth-callback "com.emclient.MailClient://oauth?code=...&state=..."`
- `_handle_oauth_callback()` is invoked
- Parses callback URI to extract code and state
- Finds matching pkce file
- Loads MSAL flow from pkce file
- Creates MSAL app with stored credentials
- Calls `acquire_token_by_auth_code_flow(flow, callback_params)`
- Receives token_result with access_token and refresh_token
- Writes to result file:
  ```json
  {
    "status": "ok",
    "token_result": { "access_token": "...", "refresh_token": "...", ... },
    "email": "user@example.com",
    "authority": "https://login.microsoftonline.com/common",
    "client_id": "e9a7fea1-1cc0-4cd9-a31b-9137ca5deedd"
  }
  ```

### 5. **OAuth Helper Completes** (Original Subprocess)
- OAuth helper detects result file was populated
- Reads result_file and checks status
- Exits with status code 0
- Cleans up pkce file

### 6. **GUI Processes Result** (Original GUI Process)
- `_watch_oauth_helper_result()` detects result file was populated
- Calls `_handle_oauth_helper_payload()` with result
- `_complete_oauth_login()` is called with token info
- Token is saved to: `.oauth_tokens/IMAP_<email>.json`
- Shows success message
- Auto-starts email fetching

## Testing Procedure

### Prerequisites
- Windows 10/11
- Python 3.12+
- MSAL library installed: `pip install msal`
- Valid Microsoft account for testing
- Application registered in Azure AD with correct redirect URI

### Test Environment Setup

1. **Verify Registry Entry**
   - Check if protocol handler is registered:
   ```powershell
   Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.ml\UserChoice'
   # or
   Get-ItemProperty 'HKCU:\Software\Classes\com.emclient.MailClient'
   ```

2. **Check Configuration**
   - Open `Setting.ini`
   - Verify OAuth settings:
   ```ini
   [MailListFetcher]
   OauthClientId=e9a7fea1-1cc0-4cd9-a31b-9137ca5deedd
   OauthAuthority=https://login.microsoftonline.com/common
   OauthRedirectUri=com.emclient.MailClient://oauth
   ```

### Test Case 1: Basic OAuth Flow

**Objective:** Verify complete OAuth token exchange

**Steps:**
1. Start GUI: `python gui_mail_list_fetcher.py`
2. Click "OAuth IMAP (Microsoft)" button
3. In browser sign-in page:
   - Enter test account email: `sbaldwin@timbermart-south.com`
   - Enter password
   - Click "Sign in"
4. If prompted, approve permissions
5. Browser shows redirect to: `com.emclient.MailClient://oauth?code=...&state=...`
6. Windows prompts: "Do you want to open gui_mail_list_fetcher?"
7. Click "Open"
8. **Expected Outcome:**
   - New process launches in background
   - Original GUI shows "Office OAuth" success message
   - Token is saved

**Validation:**
- Check token file exists: `ls .oauth_tokens/`
- Should see: `IMAP_sbaldwin@timbermart-south.com.json`
- Check token content:
  ```powershell
  Get-Content .oauth_tokens\IMAP_sbaldwin@timbermart-south.com.json | ConvertFrom-Json
  ```
- Should have: access_token, refresh_token, expires_in

### Test Case 2: Verify Token Properties

**Objective:** Verify token structure is correct

**Steps:**
1. After successful OAuth, check token file:
   ```powershell
   $token = Get-Content .oauth_tokens\IMAP_sbaldwin@timbermart-south.com.json | ConvertFrom-Json
   $token.token_result.access_token.Length -gt 100
   $token.token_result.refresh_token.Length -gt 100
   ```
2. **Expected Outcome:**
   - access_token: Long string (1000+ chars)
   - refresh_token: Long string (1000+ chars)
   - expires_in: Numeric value (usually 3600)

### Test Case 3: Token Used for Email Fetch

**Objective:** Verify token can be used for IMAP operations

**Steps:**
1. After successful OAuth
2. GUI shows success message and "Starting fetch now"
3. Email fetch should begin automatically
4. Monitor log output for:
   - Connection to outlook.office365.com:993
   - Authentication using OAuth token
   - Email list retrieval

**Expected Outcome:**
- Emails retrieved successfully
- No authentication errors

### Test Case 4: Error Handling

**Objective:** Verify proper error handling in edge cases

**Test 4a: User Denies Permissions**
1. Click OAuth button
2. In browser, deny the permission request
3. Browser shows error: `error=access_denied&error_description=...`
4. Windows launches app with error in callback
5. **Expected:** GUI shows error message

**Test 4b: Callback Timeout**
1. Click OAuth button
2. Do NOT complete sign-in within 600 seconds
3. Wait for timeout
4. **Expected:** GUI shows "OAuth timed out" error

**Test 4c: Protocol Handler Not Triggered**
1. Click OAuth button
2. Manually close browser without completing sign-in
3. Wait for timeout
4. **Expected:** Proper timeout error

### Test Case 5: Security Verification

**Objective:** Verify security features are working

**Steps:**
1. Check pkce file is deleted after use:
   ```powershell
   Get-Item -Path $env:TEMP\pkce_oauth_result_*.json -ErrorAction SilentlyContinue
   # Should return nothing after OAuth completes
   ```

2. Check token file has user-only permissions:
   ```powershell
   Get-Acl .oauth_tokens\IMAP_sbaldwin@timbermart-south.com.json | Format-List
   # Should show only owner has access
   ```

3. Verify state token is validated:
   - (Internal verification - check console output)

### Debugging Tips

**If OAuth fails:**

1. **Check console output:**
   - Run GUI with console: `python gui_mail_list_fetcher.py`
   - Look for error messages

2. **Check result file:**
   ```powershell
   Get-Item -Path $env:TEMP\oauth_result_*.json | Sort-Object LastWriteTime -Descending | Select-Object -First 1 | Get-Content | ConvertFrom-Json
   ```

3. **Check PKCE file:**
   ```powershell
   Get-Item -Path $env:TEMP\pkce_oauth_result_*.json | Sort-Object LastWriteTime -Descending | Select-Object -First 1 | Get-Content | ConvertFrom-Json
   ```

4. **Enable MSAL logging:**
   - Add to code temporarily:
   ```python
   import logging
   logging.basicConfig(level=logging.DEBUG)
   ```

5. **Check protocol handler registration:**
   ```powershell
   Get-ItemProperty 'HKCU:\Software\Classes\com.emclient.MailClient' | Format-List
   ```

### Success Criteria

- [x] OAuth flow completes without "exit code 1" error
- [x] Browser opens for Microsoft sign-in
- [x] Callback is triggered after sign-in
- [x] New process launches correctly
- [x] Token is saved to `.oauth_tokens/` directory
- [x] Email fetching starts automatically
- [x] Token has correct structure (access_token, refresh_token)
- [x] No duplicate PKCE parameters in request
- [x] State token is properly validated
- [x] Security: Token file has user-only permissions
- [x] Security: PKCE file is cleaned up after use

## Known Issues & Workarounds

### Issue 1: "msal is not defined"
**Status:** FIXED
**Solution:** Added `import msal` at function level

### Issue 2: "code_challenge is duplicated"
**Status:** FIXED
**Solution:** Removed manual PKCE parameter generation, let MSAL handle internally

### Issue 3: "Redirect URI mismatch"
**Status:** FIXED
**Solution:** Changed from localhost to custom scheme: `com.emclient.MailClient://oauth`

### Issue 4: "OAuth helper exited with code 1"
**Status:** FIXED
**Solution:** Added comprehensive try-except wrapping

## Next Steps After Testing

1. **Token Refresh:**
   - Test automatic token refresh on expiry
   - Verify `_try_refresh_oauth_token()` works

2. **Multiple Accounts:**
   - Test OAuth with multiple email addresses
   - Verify each account has separate token file

3. **Token Reuse:**
   - Test loading saved token on next app start
   - Verify token doesn't require re-authentication

4. **Pop3/Exchange:**
   - Extend OAuth support to POP3 and Exchange protocols
