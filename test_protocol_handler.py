#!/usr/bin/env python3
"""
Test OAuth Protocol Handler

This script tests if the protocol handler is properly registered and can be called.
"""

import sys
import subprocess
from pathlib import Path

def test_protocol_handler():
    """Test if protocol handler can be called"""
    print("=" * 70)
    print("Testing OAuth Protocol Handler")
    print("=" * 70)
    
    # Test 1: Check registry
    print("\n[TEST 1] Checking Windows Registry...")
    try:
        result = subprocess.run(
            ['powershell', '-Command', 
             'Get-ItemProperty "HKCU:\\Software\\Classes\\com.emclient.MailClient" -ErrorAction SilentlyContinue | Format-List'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.stdout:
            print("✅ Protocol handler found in registry:")
            print(result.stdout[:200])
        else:
            print("⚠️  Protocol handler NOT found in registry")
            print("   Run gui_mail_list_fetcher.py once to register it")
    except Exception as e:
        print(f"❌ Error checking registry: {e}")
    
    # Test 2: Check if callback handler works
    print("\n[TEST 2] Testing callback handler directly...")
    try:
        # Create a test callback URI
        test_uri = 'com.emclient.MailClient://oauth?code=test_code_123&state=test_state_456'
        print(f"Test URI: {test_uri}")
        
        # Call the callback handler
        result = subprocess.run(
            [sys.executable, 'gui_mail_list_fetcher.py', '--oauth-callback', test_uri],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(Path(__file__).parent)
        )
        
        print("\nCallback handler output:")
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print("STDERR:", result.stderr)
        print(f"Exit code: {result.returncode}")
        
    except subprocess.TimeoutExpired:
        print("⚠️  Callback handler timed out (may be waiting for result file)")
    except Exception as e:
        print(f"❌ Error testing callback: {e}")
    
    # Test 3: Check temp directory for OAuth files
    print("\n[TEST 3] Checking temp directory for OAuth files...")
    temp_dir = Path(Path.home()) / 'AppData' / 'Local' / 'Temp'
    oauth_files = list(temp_dir.glob('oauth_result_*.json')) + list(temp_dir.glob('pkce_oauth_result_*.json'))
    print(f"Found {len(oauth_files)} OAuth-related files")
    if oauth_files:
        for f in sorted(oauth_files)[-3:]:
            print(f"  - {f.name} ({f.stat().st_size} bytes)")
    
    print("\n" + "=" * 70)
    print("Test complete")
    print("=" * 70)

if __name__ == '__main__':
    test_protocol_handler()
