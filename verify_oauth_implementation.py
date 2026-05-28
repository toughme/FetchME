#!/usr/bin/env python3
"""
OAuth Implementation Verification Script

This script verifies that the OAuth implementation is correctly configured
and all necessary components are in place.
"""

import sys
import json
from pathlib import Path
import importlib.util

def check_python_version():
    """Verify Python 3.12+ is being used"""
    if sys.version_info < (3, 12):
        print(f"❌ Python 3.12+ required. Current: {sys.version_info.major}.{sys.version_info.minor}")
        return False
    print(f"✅ Python version: {sys.version_info.major}.{sys.version_info.minor}")
    return True

def check_required_libraries():
    """Verify required libraries are installed"""
    libs = ['msal', 'requests']
    all_ok = True
    
    for lib in libs:
        try:
            importlib.import_module(lib)
            print(f"✅ {lib} installed")
        except ImportError:
            print(f"❌ {lib} NOT installed")
            all_ok = False
    
    return all_ok

def check_oauth_settings():
    """Verify OAuth settings are configured"""
    settings_file = Path('Setting.ini')
    
    if not settings_file.exists():
        print(f"❌ Setting.ini not found")
        return False
    
    print(f"✅ Setting.ini exists")
    
    # Read settings
    settings = {}
    try:
        with open(settings_file) as f:
            current_section = None
            for line in f:
                line = line.strip()
                if line.startswith('[') and line.endswith(']'):
                    current_section = line[1:-1]
                    settings[current_section] = {}
                elif '=' in line and current_section:
                    key, value = line.split('=', 1)
                    settings[current_section][key.strip()] = value.strip()
    except Exception as e:
        print(f"❌ Failed to read Setting.ini: {e}")
        return False
    
    # Check required settings
    required = {
        'OauthClientId': 'OAuth Client ID',
        'OauthAuthority': 'OAuth Authority',
        'OauthRedirectUri': 'OAuth Redirect URI',
    }
    
    all_ok = True
    for key, desc in required.items():
        if key in settings.get('MailListFetcher', {}):
            value = settings['MailListFetcher'][key]
            expected_scheme = 'com.emclient.MailClient://oauth'
            if key == 'OauthRedirectUri' and value != expected_scheme:
                print(f"⚠️  {desc}: {value} (expected: {expected_scheme})")
            else:
                print(f"✅ {desc}: {value}")
        else:
            print(f"❌ {desc}: NOT SET")
            all_ok = False
    
    return all_ok

def check_gui_implementation():
    """Verify GUI implementation has required functions"""
    gui_file = Path('gui_mail_list_fetcher.py')
    
    if not gui_file.exists():
        print(f"❌ gui_mail_list_fetcher.py not found")
        return False
    
    print(f"✅ gui_mail_list_fetcher.py exists")
    
    # Check for required functions
    with open(gui_file) as f:
        content = f.read()
    
    functions = [
        '_handle_oauth_callback',
        '_run_oauth_helper_process',
        '_oauth_helper_webview_flow',
        '_oauth_helper_browser_flow',
        '_complete_oauth_login',
        'save_oauth_token',
        'load_oauth_token',
    ]
    
    all_ok = True
    for func in functions:
        if f'def {func}' in content:
            print(f"✅ Function {func} implemented")
        else:
            print(f"❌ Function {func} NOT found")
            all_ok = False
    
    return all_ok

def check_token_exchange_implementation():
    """Verify token exchange is implemented in callback handler"""
    gui_file = Path('gui_mail_list_fetcher.py')
    
    with open(gui_file) as f:
        content = f.read()
    
    # Check for token exchange code
    required_code = [
        'acquire_token_by_auth_code_flow',
        'local_app = msal.PublicClientApplication',
        'token_result = local_app.acquire_token_by_auth_code_flow',
    ]
    
    all_ok = True
    for code in required_code:
        if code in content:
            print(f"✅ Token exchange code found: {code[:50]}...")
        else:
            print(f"❌ Token exchange code NOT found: {code}")
            all_ok = False
    
    return all_ok

def check_oauth_credential_storage():
    """Verify client_id and authority stored in PKCE file"""
    gui_file = Path('gui_mail_list_fetcher.py')
    
    with open(gui_file) as f:
        content = f.read()
    
    required = [
        "'client_id': args.client_id",
        "'authority': args.authority",
    ]
    
    all_ok = True
    for code in required:
        if code in content:
            print(f"✅ Credentials stored in PKCE file: {code}")
        else:
            print(f"❌ Credentials NOT stored: {code}")
            all_ok = False
    
    return all_ok

def check_syntax():
    """Check Python syntax"""
    gui_file = Path('gui_mail_list_fetcher.py')
    
    try:
        import py_compile
        py_compile.compile(str(gui_file), doraise=True)
        print(f"✅ GUI Python syntax is valid")
        return True
    except py_compile.PyCompileError as e:
        print(f"❌ GUI Python syntax error: {e}")
        return False

def main():
    """Run all verification checks"""
    print("=" * 60)
    print("OAuth Implementation Verification")
    print("=" * 60)
    
    checks = [
        ("Python Version", check_python_version),
        ("Required Libraries", check_required_libraries),
        ("OAuth Settings", check_oauth_settings),
        ("GUI Implementation", check_gui_implementation),
        ("Token Exchange Implementation", check_token_exchange_implementation),
        ("OAuth Credential Storage", check_oauth_credential_storage),
        ("Python Syntax", check_syntax),
    ]
    
    results = []
    for name, check in checks:
        print(f"\n[{name}]")
        try:
            result = check()
            results.append((name, result))
        except Exception as e:
            print(f"❌ Check failed with exception: {e}")
            results.append((name, False))
    
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {name}")
    
    all_pass = all(result for _, result in results)
    print("=" * 60)
    
    if all_pass:
        print("✅ ALL CHECKS PASSED - OAuth implementation is ready!")
        return 0
    else:
        print("❌ SOME CHECKS FAILED - Please review the errors above")
        return 1

if __name__ == '__main__':
    sys.exit(main())
