#!/usr/bin/env python
"""
Simplified test GUI to debug the display issue
"""
import tkinter as tk
import tkinter.ttk as ttk
from pathlib import Path
import sys

script_dir = Path(__file__).resolve().parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

import mail_list_fetcher as core

class TestMailGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Mail List Fetcher - Test')
        self.geometry('1000x700')
        print("✓ Window created")
        
        # Main container
        container = ttk.Frame(self)
        container.pack(fill='both', expand=True, padx=10, pady=10)
        print("✓ Container packed")
        
        # === TOP: Connection Settings ===
        conn_frame = ttk.LabelFrame(container, text='Connection & Account', padding=8)
        conn_frame.pack(fill='x', pady=(0, 10), padx=4)
        
        ttk.Label(conn_frame, text='Email:').pack(side='left', padx=5)
        ttk.Entry(conn_frame, width=30).pack(side='left', padx=5)
        ttk.Label(conn_frame, text='Password:').pack(side='left', padx=5)
        ttk.Entry(conn_frame, width=20, show='*').pack(side='left', padx=5)
        print("✓ Connection frame added")
        
        # === MIDDLE: Buttons ===
        btn_frame = ttk.Frame(container)
        btn_frame.pack(fill='x', pady=(0, 10), padx=4)
        ttk.Button(btn_frame, text='Start Fetch').pack(side='left', padx=5)
        ttk.Button(btn_frame, text='Stop').pack(side='left', padx=5)
        print("✓ Button frame added")
        
        # === STATUS TABLE ===
        status_frame = ttk.LabelFrame(container, text='Status & Fetch Progress', padding=8)
        status_frame.pack(fill='x', pady=(0, 10), padx=4)
        
        tree = ttk.Treeview(
            status_frame,
            columns=('Email', 'Connection', 'Progress'),
            height=5,
            show='headings'
        )
        tree.column('Email', width=200, anchor='w')
        tree.column('Connection', width=250, anchor='w')
        tree.column('Progress', width=250, anchor='w')
        tree.heading('Email', text='Email Address')
        tree.heading('Connection', text='Connection Status')
        tree.heading('Progress', text='Fetch Progress')
        
        # Add sample row
        tree.insert('', 'end', values=('test@example.com', 'Ready', 'Waiting...'))
        
        scrollbar = ttk.Scrollbar(status_frame, orient='vertical', command=tree.yview)
        tree.configure(yscroll=scrollbar.set)
        tree.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        print("✓ Status table added")
        
        # === LOGS ===
        log_frame = ttk.LabelFrame(container, text='Detailed Logs', padding=8)
        log_frame.pack(fill='both', expand=True, pady=(0, 10), padx=4)
        
        log_text = tk.Text(log_frame, wrap='word', height=10)
        log_text.pack(fill='both', expand=True)
        log_text.insert('end', 'Application started successfully!\n')
        log_text.insert('end', 'Ready to begin email fetching.\n')
        print("✓ Log text added")
        
        # === STATUS BAR ===
        status_bar = ttk.Frame(container)
        status_bar.pack(fill='x', padx=4)
        ttk.Label(status_bar, text='Ready').pack(side='left')
        print("✓ Status bar added")
        
        print("✓ GUI setup complete!")
        self.update_idletasks()
        print(f"✓ Window size: {self.winfo_width()}x{self.winfo_height()}")

if __name__ == '__main__':
    try:
        print("Creating app...")
        app = TestMailGUI()
        print("Starting mainloop...")
        app.mainloop()
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
