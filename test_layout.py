#!/usr/bin/env python
import tkinter as tk
import tkinter.ttk as ttk
from pathlib import Path

class TestGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Test Layout')
        self.geometry('1024x760')
        print("Window created")
        
        container = ttk.Frame(self)
        container.pack(fill='both', expand=True, padx=10, pady=10)
        print("Container packed")
        
        # Test frame 1
        frame1 = ttk.LabelFrame(container, text='Frame 1 - Connection')
        frame1.pack(fill='x', pady=(0, 10))
        ttk.Label(frame1, text='Email:').pack()
        ttk.Entry(frame1, width=40).pack()
        print("Frame 1 created and packed")
        
        # Test frame 2 - Notebook
        notebook = ttk.Notebook(container)
        notebook.pack(fill='both', expand=True, pady=(0, 10))
        tab1 = ttk.Frame(notebook)
        notebook.add(tab1, text='Tab 1')
        ttk.Label(tab1, text='Tab content').pack()
        print("Notebook created and packed")
        
        # Test frame 3 - Status table
        frame3 = ttk.LabelFrame(container, text='Status Table')
        frame3.pack(fill='x', pady=(0, 10))
        
        tree = ttk.Treeview(frame3, columns=('Col1', 'Col2'), height=3, show='headings')
        tree.column('Col1', width=150)
        tree.column('Col2', width=150)
        tree.heading('Col1', text='Column 1')
        tree.heading('Col2', text='Column 2')
        tree.pack(fill='x', expand=True, padx=4, pady=4)
        print("Status table created and packed")
        
        # Test frame 4 - Logs
        frame4 = ttk.LabelFrame(container, text='Logs')
        frame4.pack(fill='both', expand=True, pady=(0, 10))
        log_text = tk.Text(frame4, wrap='word', height=8)
        log_text.pack(fill='both', expand=True, padx=4, pady=4)
        log_text.insert('end', 'Test log content\n')
        print("Logs frame created and packed")
        
        # Test frame 5 - Status
        frame5 = ttk.Frame(container)
        frame5.pack(fill='x')
        ttk.Label(frame5, text='Ready').pack()
        print("Status frame created and packed")
        
        print("GUI setup complete!")

if __name__ == '__main__':
    app = TestGUI()
    app.mainloop()
