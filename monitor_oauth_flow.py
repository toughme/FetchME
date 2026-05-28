#!/usr/bin/env python3
"""
OAuth Flow Monitor

Watches temporary files during OAuth flow to track what's happening.
Run this in another terminal while performing OAuth login.
"""

import json
import time
from pathlib import Path
import subprocess
import sys

def monitor_oauth_flow():
    """Monitor OAuth flow by watching temporary files"""
    temp_dir = Path(tempfile.gettempdir())
    print(f"Monitoring: {temp_dir}")
    print("=" * 70)
    print("Watching for OAuth files...")
    print("=" * 70)
    
    seen_files = set()
    
    # Monitor for 5 minutes
    deadline = time.time() + 300
    
    while time.time() < deadline:
        # Check for oauth result files
        oauth_files = list(temp_dir.glob('oauth_result_*.json'))
        pkce_files = list(temp_dir.glob('pkce_oauth_result_*.json'))
        
        all_files = oauth_files + pkce_files
        
        for file_path in all_files:
            if file_path.name not in seen_files:
                seen_files.add(file_path.name)
                print(f"\n[NEW FILE] {file_path.name}")
                print(f"Time: {time.strftime('%H:%M:%S')}")
                
                try:
                    # Wait a moment for file to be fully written
                    time.sleep(0.5)
                    
                    if file_path.stat().st_size > 0:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            try:
                                data = json.load(f)
                                print(f"Content: {json.dumps(data, indent=2)[:500]}...")
                                
                                # Check status
                                status = data.get('status')
                                if status == 'ok':
                                    print("✅ OAuth SUCCESS - Token received!")
                                    if 'token_result' in data:
                                        tr = data['token_result']
                                        print(f"   - Access Token: {str(tr.get('access_token', ''))[:50]}...")
                                        print(f"   - Has Refresh Token: {'refresh_token' in tr}")
                                        print(f"   - Expires In: {tr.get('expires_in')} seconds")
                                elif status == 'error':
                                    print(f"❌ OAuth ERROR: {data.get('error')}")
                                elif status == 'callback_received':
                                    print("⏳ Callback received (waiting for token exchange)")
                                else:
                                    print(f"⏳ Status: {status}")
                            except json.JSONDecodeError as e:
                                print(f"⚠️  File not fully written yet or invalid JSON: {e}")
                    else:
                        print("⏳ File created but empty (being written)")
                        
                except Exception as e:
                    print(f"Error reading file: {e}")
        
        time.sleep(1)
    
    print("\n" + "=" * 70)
    print("Monitor timeout - OAuth may not have completed")
    print("=" * 70)

if __name__ == '__main__':
    import tempfile
    monitor_oauth_flow()
