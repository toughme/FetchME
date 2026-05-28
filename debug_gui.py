#!/usr/bin/env python
import sys
import traceback
from pathlib import Path

script_dir = Path(__file__).resolve().parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

try:
    import mail_list_fetcher as core
    print("✓ mail_list_fetcher imported")
except Exception as e:
    print(f"✗ Failed to import mail_list_fetcher: {e}")
    traceback.print_exc()
    sys.exit(1)

try:
    import gui_mail_list_fetcher
    print("✓ gui_mail_list_fetcher imported")
except Exception as e:
    print(f"✗ Failed to import gui_mail_list_fetcher: {e}")
    traceback.print_exc()
    sys.exit(1)

try:
    print("Creating GUI app...")
    app = gui_mail_list_fetcher.MailListFetcherGUI()
    print("✓ GUI app created, starting mainloop...")
    app.mainloop()
except Exception as e:
    print(f"✗ Error during GUI execution: {e}")
    traceback.print_exc()
    sys.exit(1)
