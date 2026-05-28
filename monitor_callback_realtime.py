#!/usr/bin/env python3
"""
Real-time OAuth Callback Monitor

Monitors both callback debug log and temp files.
"""

import time
import json
from pathlib import Path
import sys

def monitor_oauth():
    """Monitor OAuth callbacks and debug log"""
    temp_dir = Path(Path.home()) / 'AppData' / 'Local' / 'Temp'
    log_file = temp_dir / 'oauth_callback_debug.log'
    
    print("=" * 70)
    print("OAuth Callback Monitor")
    print("=" * 70)
    print(f"\nMonitoring:")
    print(f"  - Log file: {log_file}")
    print(f"  - Temp dir: {temp_dir}")
    print("\nStart the OAuth flow and watch for updates...\n")
    
    seen_log_lines = 0
    seen_result_files = set()
    
    deadline = time.time() + 600  # 10 minute timeout
    
    while time.time() < deadline:
        # Check log file
        if log_file.exists():
            try:
                with log_file.open('r', encoding='utf-8') as f:
                    lines = f.readlines()
                
                new_lines = lines[seen_log_lines:]
                if new_lines:
                    print(f"\n[LOG] {time.strftime('%H:%M:%S')}")
                    for line in new_lines:
                        print(f"  {line.rstrip()}")
                    seen_log_lines = len(lines)
            except Exception as e:
                print(f"Error reading log: {e}")
        
        # Check for result files
        result_files = list(temp_dir.glob('oauth_result_*.json'))
        pkce_files = list(temp_dir.glob('pkce_oauth_result_*.json'))
        
        for f in result_files + pkce_files:
            if f.name not in seen_result_files:
                seen_result_files.add(f.name)
                try:
                    with f.open('r', encoding='utf-8') as fp:
                        data = json.load(fp)
                    
                    status = data.get('status', '?')
                    print(f"\n[FILE] {time.strftime('%H:%M:%S')} - {f.name}")
                    
                    if status == 'ok':
                        print(f"  ✅ SUCCESS - Token received")
                        if 'email' in data:
                            print(f"     Email: {data['email']}")
                    elif status == 'error':
                        print(f"  ❌ ERROR - {data.get('error', 'Unknown')}")
                    else:
                        print(f"  ⏳ Status: {status}")
                except json.JSONDecodeError:
                    print(f"\n[FILE] {time.strftime('%H:%M:%S')} - {f.name} (not yet readable)")
                except Exception as e:
                    print(f"\n[FILE] {f.name} - Error: {e}")
        
        time.sleep(1)
    
    print("\n" + "=" * 70)
    print("Timeout - Monitor ended")
    print("=" * 70)

if __name__ == '__main__':
    try:
        monitor_oauth()
    except KeyboardInterrupt:
        print("\nMonitor stopped")
        sys.exit(0)
