import argparse
import base64
import hashlib
import http.server
import io
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import tkinter as tk
import urllib.parse
import webbrowser
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

script_dir = Path(__file__).resolve().parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

import mail_list_fetcher as core


@dataclass
class OAuthSessionState:
    provider: str
    email: str
    access_token: str
    authority: str
    client_id: str
    token_result: dict | None = None
    refresh_token: str | None = None
    token_expiry: float | None = None


class PKCEHelper:
    @staticmethod
    def generate_code_verifier(length: int = 128) -> str:
        """Generate PKCE code verifier."""
        return base64.urlsafe_b64encode(secrets.token_bytes(length // 2)).decode('utf-8').rstrip('=')

    @staticmethod
    def generate_code_challenge(code_verifier: str) -> str:
        """Generate PKCE code challenge from verifier."""
        code_sha = hashlib.sha256(code_verifier.encode('utf-8')).digest()
        return base64.urlsafe_b64encode(code_sha).decode('utf-8').rstrip('=')


class ProtocolHandler:
    PROTOCOL_SCHEME = 'com.emclient.MailClient'
    REGISTRY_PATH = f'HKEY_CLASSES_ROOT\\{PROTOCOL_SCHEME}'

    @staticmethod
    def register_protocol_handler(app_exe_path: str | None = None) -> bool:
        """Register Windows protocol handler for custom URI scheme."""
        try:
            import winreg
        except ImportError:
            return False

        try:
            python_exe = sys.executable
            pythonw_exe = python_exe.replace('python.exe', 'pythonw.exe')
            if Path(pythonw_exe).exists():
                runner_exe = pythonw_exe
            else:
                runner_exe = python_exe

            script_path = str(Path(__file__).resolve())

            with winreg.CreateKey(winreg.HKEY_CLASSES_ROOT, ProtocolHandler.PROTOCOL_SCHEME) as key:
                winreg.SetValueEx(key, '', 0, winreg.REG_SZ, f'URL:{ProtocolHandler.PROTOCOL_SCHEME} Protocol')
                winreg.SetValueEx(key, 'URL Protocol', 0, winreg.REG_SZ, '')

            cmd_path = f'{ProtocolHandler.PROTOCOL_SCHEME}\\shell\\open\\command'
            with winreg.CreateKey(winreg.HKEY_CLASSES_ROOT, cmd_path) as key:
                cmd = f'"{runner_exe}" "{script_path}" --oauth-callback "%1"'
                winreg.SetValueEx(key, '', 0, winreg.REG_SZ, cmd)

            return True
        except Exception as e:
            print(f'Failed to register protocol handler: {e}')
            return False

    @staticmethod
    def parse_callback_uri(uri: str) -> dict[str, str]:
        """Parse OAuth callback URI."""
        parsed = urllib.parse.urlparse(uri)
        params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        return params


class TextRedirector(io.TextIOBase):
    def __init__(self, write_callback):
        self.write_callback = write_callback

    def write(self, s: str) -> int:
        if not s:
            return 0
        self.write_callback(s)
        return len(s)

    def flush(self) -> None:
        pass


class StatusAwareTextRedirector(io.TextIOBase):
    def __init__(self, write_callback, status_callback=None):
        self.write_callback = write_callback
        self.status_callback = status_callback
        self.buffer = ''

    def write(self, s: str) -> int:
        if not s:
            return 0
        self.buffer += s
        
        # Try to extract status information from the message
        if self.status_callback:
            self._parse_and_report_status(s)
        
        self.write_callback(s)
        return len(s)

    def _parse_and_report_status(self, message: str) -> None:
        """Parse log messages and extract status updates."""
        msg = message.lower()
        
        # Extract email if present
        email = ''
        if '@' in message:
            import re
            match = re.search(r'[\w\.-]+@[\w\.-]+', message)
            if match:
                email = match.group(0)
        
        # Connection status detection
        if 'connecting' in msg and 'imap' in msg:
            self.status_callback(email, 'Logging in with IMAP...', '')
        elif 'connecting' in msg and 'pop3' in msg:
            self.status_callback(email, 'Logging in with POP3...', '')
        elif 'pop3 login succeeded' in msg or 'imap login succeeded' in msg or 'xoauth2 authentication succeeded' in msg:
            self.status_callback(email, 'Logged in', '')
        elif 'authentication failed' in msg or 'login failed' in msg:
            self.status_callback(email, 'Logging failed', '')
        
        # Fetch progress detection
        elif 'found' in msg and 'folders' in msg:
            import re
            match = re.search(r'found\s+(\d+)\s+folders', msg)
            if match:
                self.status_callback(email, '', f"Searching {match.group(1)} folders...")
        elif 'fetching folder' in msg:
            import re
            match = re.search(r'fetching folder\s+(\S+)', msg)
            if match:
                folder_name = match.group(1)
                self.status_callback(email, '', f"Searching {folder_name} folder...")
        elif 'messages matching criteria' in msg:
            import re
            match = re.search(r'(\d+)\s+messages matching criteria', msg)
            if match:
                count = match.group(1)
                self.status_callback(email, '', f"Found {count} emails - starting fetch...")
        elif 'completed' in msg.lower() and 'save' not in msg.lower():
            self.status_callback(email, '', 'Completed')

    def flush(self) -> None:
        pass


class MailListFetcherGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title('Mail List Fetcher v2.5')
        self.geometry('1024x760')
        self.minsize(980, 720)
        self.protocol('WM_DELETE_WINDOW', self.on_close)
        self._configure_style()

        self.script_dir = Path(__file__).resolve().parent
        self.config_path = self.script_dir / 'Config.ini'
        self.server_path = self.script_dir / 'Server_List.ini'
        self.settings_path = self.script_dir / 'Setting.ini'
        self.fetcher: core.BaseFetcher | None = None
        self.stdout_backup = sys.stdout
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.main_thread_id = threading.get_ident()
        self.status_updates_queue: queue.Queue[dict] = queue.Queue()
        self.status_tree_items: dict[str, str] = {}

        self.provider_var = tk.StringVar(value='IMAP')
        self.email_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.server_var = tk.StringVar()
        self.port_var = tk.StringVar(value='993')
        self.ssl_var = tk.BooleanVar(value=True)
        self.output_var = tk.StringVar(value=str(self.script_dir / 'output'))
        self.login_file = self.script_dir / 'logins.txt'
        self.login_listbox: tk.Listbox | None = None
        self.login_context_menu: tk.Menu | None = None
        self.login_entries: list[tuple[str, str, str]] = []
        self.oauth_client_id = 'e9a7fea1-1cc0-4cd9-a31b-9137ca5deedd'
        self.oauth_authority = 'https://login.microsoftonline.com/common'
        self.oauth_session: OAuthSessionState | None = None
        self._oauth_in_progress = False
        self._oauth_helper_proc: subprocess.Popen | None = None
        self._oauth_result_file: Path | None = None

        self.keyword_var = tk.StringVar()
        self.date_from_var = tk.StringVar()
        self.date_to_var = tk.StringVar()
        self.search_subject_var = tk.BooleanVar(value=True)
        self.search_body_var = tk.BooleanVar(value=True)

        self.save_content_var = tk.BooleanVar(value=True)
        self.save_attachments_var = tk.BooleanVar(value=True)
        self.save_results_var = tk.BooleanVar(value=True)
        self.save_logs_var = tk.BooleanVar(value=True)
        self.separated_file_var = tk.BooleanVar(value=True)
        self.correct_account_var = tk.BooleanVar(value=False)
        self.thread_count_var = tk.StringVar(value='5')

        self.folder_whitelist_text: tk.Text | None = None
        self.folder_blacklist_text: tk.Text | None = None
        self.attachment_whitelist_text: tk.Text | None = None
        self.attachment_blacklist_text: tk.Text | None = None
        self.log_text: tk.Text | None = None
        self.status_tree: ttk.Treeview | None = None

        self._build_gui()
        try:
            self.load_configuration()
        except Exception as e:
            self.append_log(f'Error loading configuration: {e}\n')
            import traceback
            traceback.print_exc()
        self._register_oauth_protocol_handler()
        self.after(75, self._drain_log_queue)
        self.after(75, self._process_status_updates)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('TFrame', background='#f5f7fb')
        style.configure('TLabelframe', background='#f5f7fb', padding=8)
        style.configure('TLabelframe.Label', background='#f5f7fb', foreground='#1f2937')
        style.configure('TLabel', background='#f5f7fb', foreground='#1f2937')
        style.configure('TButton', padding=(10, 5))
        style.configure('TNotebook', background='#f5f7fb')
        style.configure('TNotebook.Tab', padding=(12, 6))
        self.configure(background='#f5f7fb')

    def _register_oauth_protocol_handler(self) -> None:
        """Register Windows protocol handler for OAuth callback."""
        try:
            script_path = Path(__file__).resolve()
            success = ProtocolHandler.register_protocol_handler(str(script_path))
            if success:
                self.append_log('OAuth protocol handler registered successfully.\n')
            else:
                self.append_log('Note: OAuth protocol handler registration requires admin privileges.\n')
        except Exception as e:
            print(f'Protocol handler registration error: {e}')  # Use print to avoid log_text issues
            self.append_log(f'Protocol handler registration skipped: {e}\n')

    def _get_secure_token_dir(self) -> Path:
        """Get secure token storage directory."""
        token_dir = self.script_dir / '.oauth_tokens'
        token_dir.mkdir(exist_ok=True, mode=0o700)
        return token_dir

    def save_oauth_token(self, provider: str, email: str, token_data: dict) -> bool:
        """Save OAuth token securely."""
        try:
            token_dir = self._get_secure_token_dir()
            token_file = token_dir / f'{provider}_{email}.json'
            
            # Include metadata
            token_data['provider'] = provider
            token_data['email'] = email
            token_data['saved_at'] = time.time()
            
            with token_file.open('w', encoding='utf-8') as fp:
                json.dump(token_data, fp)
            
            # Restrict file permissions
            try:
                import stat
                token_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except Exception:
                pass
            
            return True
        except Exception as e:
            self.append_log(f'Failed to save OAuth token: {e}\n')
            return False

    def load_oauth_token(self, provider: str, email: str) -> dict | None:
        """Load OAuth token securely."""
        try:
            token_dir = self._get_secure_token_dir()
            token_file = token_dir / f'{provider}_{email}.json'
            
            if not token_file.exists():
                return None
            
            with token_file.open('r', encoding='utf-8') as fp:
                return json.load(fp)
        except Exception as e:
            self.append_log(f'Failed to load OAuth token: {e}\n')
            return None

    def _build_gui(self) -> None:
        container = ttk.Frame(self)
        container.pack(fill='both', expand=True, padx=10, pady=10)

        connection_frame = ttk.LabelFrame(container, text='Connection & Account')
        connection_frame.pack(fill='x', pady=(0, 10))

        provider_frame = ttk.Frame(connection_frame)
        provider_frame.grid(row=0, column=0, columnspan=4, sticky='w', pady=(4, 8))
        ttk.Label(provider_frame, text='Provider:').pack(side='left', padx=(0, 8))
        for provider in ['IMAP', 'POP3', 'Exchange']:
            ttk.Radiobutton(provider_frame, text=provider, variable=self.provider_var, value=provider, command=self._update_port_label).pack(side='left', padx=6)

        ttk.Label(connection_frame, text='Email:').grid(row=1, column=0, sticky='w', padx=4, pady=2)
        ttk.Entry(connection_frame, textvariable=self.email_var, width=32).grid(row=1, column=1, sticky='w', padx=4, pady=2)
        ttk.Label(connection_frame, text='Password:').grid(row=1, column=2, sticky='w', padx=4, pady=2)
        ttk.Entry(connection_frame, textvariable=self.password_var, show='*', width=32).grid(row=1, column=3, sticky='w', padx=4, pady=2)

        ttk.Label(connection_frame, text='Server:').grid(row=2, column=0, sticky='w', padx=4, pady=2)
        ttk.Entry(connection_frame, textvariable=self.server_var, width=32).grid(row=2, column=1, sticky='w', padx=4, pady=2)
        ttk.Label(connection_frame, text='Port:').grid(row=2, column=2, sticky='w', padx=4, pady=2)
        ttk.Entry(connection_frame, textvariable=self.port_var, width=10).grid(row=2, column=3, sticky='w', padx=4, pady=2)

        ttk.Checkbutton(connection_frame, text='Use SSL/TLS', variable=self.ssl_var).grid(row=3, column=0, sticky='w', padx=4, pady=2)
        ttk.Button(connection_frame, text='Auto Detect Server', command=self.auto_detect_server).grid(row=3, column=1, sticky='w', padx=4, pady=2)
        ttk.Button(connection_frame, text='Open Config Folder', command=self.open_config_folder).grid(row=3, column=2, sticky='w', padx=4, pady=2)

        oauth_frame = ttk.Frame(connection_frame)
        oauth_frame.grid(row=4, column=0, columnspan=4, sticky='w', pady=(4, 2))
        ttk.Label(oauth_frame, text='OAuth:').pack(side='left', padx=(0, 4))
        ttk.Button(oauth_frame, text='OAuth IMAP (Microsoft)', command=self.office_oauth_imap).pack(side='left', padx=4)
        ttk.Button(oauth_frame, text='OAuth Exchange (Microsoft)', command=self.office_oauth_exchange).pack(side='left', padx=4)
        self.oauth_status_label = ttk.Label(oauth_frame, text='No OAuth token', foreground='gray')
        self.oauth_status_label.pack(side='left', padx=(12, 0))

        action_frame = ttk.Frame(container)
        action_frame.pack(fill='x', pady=(0, 10))
        self.start_button = ttk.Button(action_frame, text='Start Fetch', command=self.start_fetch)
        self.start_button.pack(side='left', padx=(0, 6))
        self.stop_button = ttk.Button(action_frame, text='Stop', command=self.stop_fetch, state='disabled')
        self.stop_button.pack(side='left', padx=(0, 6))
        ttk.Button(action_frame, text='Save Current Settings', command=self.save_current_settings).pack(side='left', padx=(0, 6))
        ttk.Button(action_frame, text='Reload Config Files', command=self.load_configuration).pack(side='left', padx=(0, 6))

        notebook = ttk.Notebook(container)
        notebook.pack(fill='both', expand=True, pady=(0, 10))

        self._build_login_tab(notebook)
        self._build_search_tab(notebook)
        self._build_filters_tab(notebook)
        self._build_options_tab(notebook)

        # Status table with fixed height
        log_frame = ttk.LabelFrame(container, text='Status & Fetch Progress')
        log_frame.pack(fill='x', pady=(0, 10))
        
        tree_frame = ttk.Frame(log_frame)
        tree_frame.pack(fill='x', padx=4, pady=4)
        
        self.status_tree = ttk.Treeview(
            tree_frame,
            columns=('Email', 'Connection Status', 'Fetch Progress'),
            height=5,
            show='headings'
        )
        self.status_tree.column('Email', width=150, anchor='w')
        self.status_tree.column('Connection Status', width=200, anchor='w')
        self.status_tree.column('Fetch Progress', width=200, anchor='w')
        
        self.status_tree.heading('Email', text='Email Address')
        self.status_tree.heading('Connection Status', text='Connection Status')
        self.status_tree.heading('Fetch Progress', text='Fetch Progress')
        
        scrollbar = ttk.Scrollbar(tree_frame, orient='vertical', command=self.status_tree.yview)
        self.status_tree.configure(yscroll=scrollbar.set)
        self.status_tree.pack(side='left', fill='x', expand=True)
        scrollbar.pack(side='right', fill='y')
        
        # Detailed logs
        log_detail_frame = ttk.LabelFrame(container, text='Detailed Logs')
        log_detail_frame.pack(fill='both', expand=True, pady=(0, 10))
        self.log_text = tk.Text(log_detail_frame, wrap='word', state='disabled', height=8)
        self.log_text.pack(fill='both', expand=True, padx=4, pady=4)

        status_frame = ttk.Frame(container)
        status_frame.pack(fill='x', pady=(0, 0))
        self.status_label = ttk.Label(status_frame, text='Ready')
        self.status_label.pack(side='left', padx=4)

    def _build_login_tab(self, notebook: ttk.Notebook) -> None:
        login_tab = ttk.Frame(notebook)
        notebook.add(login_tab, text='Login List')

        login_frame = ttk.Frame(login_tab)
        login_frame.pack(fill='both', expand=True, padx=8, pady=8)

        left_frame = ttk.Frame(login_frame)
        left_frame.pack(side='left', fill='both', expand=True, padx=(0, 8))
        right_frame = ttk.Frame(login_frame)
        right_frame.pack(side='right', fill='y')

        ttk.Label(left_frame, text='Login Entries (.txt) - format: email|password|domain(optional)').pack(anchor='w')
        self.login_listbox = tk.Listbox(left_frame, activestyle='dotbox', height=18)
        self.login_listbox.pack(fill='both', expand=True, pady=4)
        self.login_listbox.bind('<Double-Button-1>', self.on_login_double_click)
        self.login_listbox.bind('<Button-3>', self.on_login_right_click)

        button_frame = ttk.Frame(right_frame)
        button_frame.pack(fill='x', pady=4)
        ttk.Button(button_frame, text='Load Logins...', command=self.choose_login_file).pack(fill='x', padx=4, pady=2)
        ttk.Button(button_frame, text='Clear List', command=self.clear_login_list).pack(fill='x', padx=4, pady=2)
        ttk.Button(button_frame, text='Add Current Login', command=self.add_current_login).pack(fill='x', padx=4, pady=2)
        ttk.Button(button_frame, text='Save Login File', command=self.save_login_file).pack(fill='x', padx=4, pady=2)

        self.login_context_menu = tk.Menu(self, tearoff=0)
        self.login_context_menu.add_command(label='Copy Email Address', command=self.copy_email_address)
        self.login_context_menu.add_command(label='Copy Password', command=self.copy_password)
        self.login_context_menu.add_command(label='Copy Domain', command=self.copy_domain)
        self.login_context_menu.add_separator()
        self.login_context_menu.add_command(label='Use Login', command=self.use_selected_login)
        self.login_context_menu.add_command(label='Clear List', command=self.clear_login_list)
        self.login_context_menu.add_separator()
        self.login_context_menu.add_command(label='Login with OAuth IMAP', command=self.office_oauth_imap)
        self.login_context_menu.add_command(label='Office OAuth Login (Exchange)', command=self.office_oauth_exchange)

    def _build_search_tab(self, notebook: ttk.Notebook) -> None:
        search_tab = ttk.Frame(notebook)
        notebook.add(search_tab, text='Search & Query')

        ttk.Label(search_tab, text='Keyword:').grid(row=0, column=0, sticky='w', padx=4, pady=6)
        ttk.Entry(search_tab, textvariable=self.keyword_var, width=60).grid(row=0, column=1, columnspan=3, sticky='w', padx=4, pady=6)

        ttk.Label(search_tab, text='Date From:').grid(row=1, column=0, sticky='w', padx=4, pady=6)
        ttk.Entry(search_tab, textvariable=self.date_from_var, width=16).grid(row=1, column=1, sticky='w', padx=4, pady=6)
        ttk.Label(search_tab, text='Date To:').grid(row=1, column=2, sticky='w', padx=4, pady=6)
        ttk.Entry(search_tab, textvariable=self.date_to_var, width=16).grid(row=1, column=3, sticky='w', padx=4, pady=6)

        ttk.Checkbutton(search_tab, text='Search Subject', variable=self.search_subject_var).grid(row=2, column=0, sticky='w', padx=4, pady=6)
        ttk.Checkbutton(search_tab, text='Search Body', variable=self.search_body_var).grid(row=2, column=1, sticky='w', padx=4, pady=6)
        ttk.Label(search_tab, text='Date format: YYYY-MM-DD').grid(row=2, column=2, columnspan=2, sticky='w', padx=4, pady=6)

        for i in range(4):
            search_tab.columnconfigure(i, weight=1)

    def _build_filters_tab(self, notebook: ttk.Notebook) -> None:
        filters_tab = ttk.Frame(notebook)
        notebook.add(filters_tab, text='Folder & Attachment Filters')

        left_frame = ttk.Frame(filters_tab)
        left_frame.grid(row=0, column=0, sticky='nsew', padx=4, pady=4)
        right_frame = ttk.Frame(filters_tab)
        right_frame.grid(row=0, column=1, sticky='nsew', padx=4, pady=4)

        ttk.Label(left_frame, text='Folder White List').pack(anchor='w')
        self.folder_whitelist_text = tk.Text(left_frame, width=40, height=10)
        self.folder_whitelist_text.pack(fill='both', expand=True, pady=4)

        ttk.Label(left_frame, text='Folder Black List').pack(anchor='w')
        self.folder_blacklist_text = tk.Text(left_frame, width=40, height=10)
        self.folder_blacklist_text.pack(fill='both', expand=True, pady=4)

        ttk.Label(right_frame, text='Attachment Extension White List').pack(anchor='w')
        self.attachment_whitelist_text = tk.Text(right_frame, width=40, height=10)
        self.attachment_whitelist_text.pack(fill='both', expand=True, pady=4)

        ttk.Label(right_frame, text='Attachment Extension Black List').pack(anchor='w')
        self.attachment_blacklist_text = tk.Text(right_frame, width=40, height=10)
        self.attachment_blacklist_text.pack(fill='both', expand=True, pady=4)

        button_frame = ttk.Frame(filters_tab)
        button_frame.grid(row=1, column=0, columnspan=2, pady=6)
        ttk.Button(button_frame, text='Save Filter Lists', command=self.save_filter_config).pack(side='left', padx=4)
        ttk.Button(button_frame, text='Reload Filter Lists', command=self.load_configuration).pack(side='left', padx=4)

        filters_tab.columnconfigure(0, weight=1)
        filters_tab.columnconfigure(1, weight=1)

    def _build_options_tab(self, notebook: ttk.Notebook) -> None:
        options_tab = ttk.Frame(notebook)
        notebook.add(options_tab, text='Options & Output')

        options_frame = ttk.LabelFrame(options_tab, text='Fetch Options')
        options_frame.pack(fill='x', padx=8, pady=8)

        ttk.Checkbutton(options_frame, text='Save Email Content', variable=self.save_content_var).grid(row=0, column=0, sticky='w', padx=8, pady=4)
        ttk.Checkbutton(options_frame, text='Save Attachments', variable=self.save_attachments_var).grid(row=0, column=1, sticky='w', padx=8, pady=4)
        ttk.Checkbutton(options_frame, text='Save Result CSV', variable=self.save_results_var).grid(row=1, column=0, sticky='w', padx=8, pady=4)
        ttk.Checkbutton(options_frame, text='Save Log Files', variable=self.save_logs_var).grid(row=1, column=1, sticky='w', padx=8, pady=4)
        ttk.Checkbutton(options_frame, text='Save Correct Account', variable=self.correct_account_var).grid(row=2, column=0, sticky='w', padx=8, pady=4)
        ttk.Checkbutton(options_frame, text='Save To Separated File', variable=self.separated_file_var).grid(row=2, column=1, sticky='w', padx=8, pady=4)

        ttk.Label(options_frame, text='Thread Count:').grid(row=3, column=0, sticky='w', padx=8, pady=4)
        ttk.Entry(options_frame, textvariable=self.thread_count_var, width=8).grid(row=3, column=1, sticky='w', padx=8, pady=4)

        timeout_frame = ttk.Frame(options_frame)
        timeout_frame.grid(row=4, column=0, columnspan=2, sticky='w', padx=8, pady=4)
        ttk.Label(timeout_frame, text='Connection Timeout (seconds):').pack(side='left')
        self.timeout_var = tk.StringVar(value='30')
        ttk.Entry(timeout_frame, textvariable=self.timeout_var, width=8).pack(side='left', padx=(4, 0))

        output_frame = ttk.LabelFrame(options_tab, text='Output Settings')
        output_frame.pack(fill='x', padx=8, pady=(0, 8))
        ttk.Label(output_frame, text='Output Folder:').grid(row=0, column=0, sticky='w', padx=8, pady=6)
        ttk.Entry(output_frame, textvariable=self.output_var, width=60).grid(row=0, column=1, sticky='w', padx=8, pady=6)
        ttk.Button(output_frame, text='Browse', command=self.choose_output_folder).grid(row=0, column=2, sticky='w', padx=8, pady=6)

    def _update_port_label(self) -> None:
        provider = self.provider_var.get()
        if provider == 'IMAP':
            self.port_var.set('993' if self.ssl_var.get() else '143')
        elif provider == 'POP3':
            self.port_var.set('995' if self.ssl_var.get() else '110')
        elif provider == 'Exchange':
            self.port_var.set('443')

    def choose_output_folder(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.output_var.get() or str(self.script_dir))
        if folder:
            self.output_var.set(folder)

    def auto_detect_server(self) -> None:
        email = self.email_var.get().strip()
        if '@' not in email:
            messagebox.showwarning('Auto Detect', 'Please enter a valid email address for autodiscovery.')
            return
        domain = email.split('@', 1)[1]
        provider = self.provider_var.get()
        server, port, encryption, url = core.ServerResolver.choose_server(domain, provider, core.IniLoader.load_server_rules(self.server_path))
        if server:
            self.server_var.set(server)
        if port:
            self.port_var.set(str(port))
        if encryption and encryption.upper() == 'SSL':
            self.ssl_var.set(True)
        self.append_log(f'Autodetected server: {server}:{port} (encryption={encryption})\n')

    def open_config_folder(self) -> None:
        try:
            import subprocess
            subprocess.Popen(['explorer', str(self.script_dir)])
        except Exception:
            messagebox.showinfo('Open Folder', f'Config folder: {self.script_dir}')

    def load_configuration(self) -> None:
        try:
            config = core.IniLoader.load_config(self.config_path)
            settings = core.IniLoader.load_settings(self.settings_path)
            self.keyword_var.set(settings.keyword)
            self.date_from_var.set(settings.date_from.isoformat() if settings.date_from else '')
            self.date_to_var.set(settings.date_to.isoformat() if settings.date_to else '')
            self.search_subject_var.set(settings.search_subject)
            self.search_body_var.set(settings.search_body)
            self.save_content_var.set(settings.save_content)
            self.save_attachments_var.set(settings.save_attachments)
            self.save_results_var.set(settings.save_email_result)
            self.save_logs_var.set(settings.save_log)
            self.separated_file_var.set(settings.save_separated_file)
            self.correct_account_var.set(settings.save_correct_account)
            self.thread_count_var.set(str(settings.thread_count))
            self.oauth_client_id = settings.oauth_client_id
            self.oauth_authority = settings.oauth_authority
            self.timeout_var.set(str(settings.connection_timeout))

            def fill_text(widget: tk.Text, values: list[str]) -> None:
                if widget is None:
                    return
                widget.delete('1.0', 'end')
                widget.insert('end', '\n'.join(values))

            if self.folder_whitelist_text:
                fill_text(self.folder_whitelist_text, config.folder_whitelist)
            if self.folder_blacklist_text:
                fill_text(self.folder_blacklist_text, config.folder_blacklist)
            if self.attachment_whitelist_text:
                fill_text(self.attachment_whitelist_text, config.attachment_whitelist)
            if self.attachment_blacklist_text:
                fill_text(self.attachment_blacklist_text, config.attachment_blacklist)

            self.append_log('Loaded configuration files.\n')
            self.load_login_file()
        except Exception as e:
            print(f'Error in load_configuration: {e}')
            import traceback
            traceback.print_exc()
            self.append_log(f'Error loading configuration: {e}\n')

    def save_current_settings(self) -> None:
        settings = core.IniLoader.load_settings(self.settings_path)
        settings.keyword = self.keyword_var.get().strip()
        settings.date_from = core.parse_date_option(self.date_from_var.get().strip()) if self.date_from_var.get().strip() else None
        settings.date_to = core.parse_date_option(self.date_to_var.get().strip()) if self.date_to_var.get().strip() else None
        settings.search_subject = self.search_subject_var.get()
        settings.search_body = self.search_body_var.get()
        settings.save_content = self.save_content_var.get()
        settings.save_attachments = self.save_attachments_var.get()
        settings.save_email_result = self.save_results_var.get()
        settings.save_log = self.save_logs_var.get()
        settings.save_separated_file = self.separated_file_var.get()
        settings.save_correct_account = self.correct_account_var.get()
        settings.thread_count = int(self.thread_count_var.get() or '1')
        settings.oauth_client_id = self.oauth_client_id
        settings.oauth_authority = self.oauth_authority
        settings.connection_timeout = int(self.timeout_var.get() or '30')

        parser = core.configparser.ConfigParser()
        parser['MailListFetcher'] = {
            'ThreadCount': str(settings.thread_count),
            'ChkSaveContent': '1' if settings.save_content else '0',
            'ChkSaveAttachment': '1' if settings.save_attachments else '0',
            'ChkSaveCorrectAccount': '1' if settings.save_correct_account else '0',
            'ChkToSeparatedFile': '1' if settings.save_separated_file else '0',
            'ChkSaveEmailResult': '1' if settings.save_email_result else '0',
            'Keyword': settings.keyword,
            'DateFrom': settings.date_from.isoformat() if settings.date_from else '',
            'DateTo': settings.date_to.isoformat() if settings.date_to else '',
            'ChkSaveLogFiles': '1' if settings.save_log else '0',
            'ChkSearchFromSubject': '1' if settings.search_subject else '0',
            'ChkSearchFromBody': '1' if settings.search_body else '0',
            'OauthClientId': settings.oauth_client_id,
            'OauthAuthority': settings.oauth_authority,
            'OauthRedirectUri': settings.oauth_redirect_uri,
            'ConnectionTimeout': str(settings.connection_timeout),
        }
        with self.settings_path.open('w', encoding='utf-8', newline='') as fp:
            parser.write(fp)
        self.append_log('Saved settings to Setting.ini\n')

    def save_filter_config(self) -> None:
        def read_list(widget: tk.Text) -> list[str]:
            return [line.strip() for line in widget.get('1.0', 'end').splitlines() if line.strip()]

        folder_wl = read_list(self.folder_whitelist_text)
        folder_bl = read_list(self.folder_blacklist_text)
        attachment_wl = [item.lower() for item in read_list(self.attachment_whitelist_text)]
        attachment_bl = [item.lower() for item in read_list(self.attachment_blacklist_text)]

        with self.config_path.open('w', encoding='utf-8', newline='') as fp:
            fp.write('# Folder name white list, if white list is not empty, fetcher only gets emails in the listed folders\n')
            fp.write('[FoldNameWhiteList]\n')
            for item in folder_wl:
                fp.write(f'{item}\n')
            fp.write('\n# Folder name black list, if white list is empty, fetcher skips these folders\n')
            fp.write('[FoldNameBlackList]\n')
            for item in folder_bl:
                fp.write(f'{item}\n')
            fp.write('\n# Attachment file extension white list\n')
            fp.write('[AttachmentExtensionWhiteList]\n')
            for item in attachment_wl:
                fp.write(f'{item}\n')
            fp.write('\n# Attachment file extension black list\n')
            fp.write('[AttachmentExtensionBlackList]\n')
            for item in attachment_bl:
                fp.write(f'{item}\n')

        self.append_log('Saved filter configuration to Config.ini\n')

    def choose_login_file(self) -> None:
        file_path = filedialog.askopenfilename(
            initialdir=self.script_dir,
            title='Select Login File',
            filetypes=[('Text Files', '*.txt'), ('All Files', '*.*')]
        )
        if not file_path:
            return
        self.login_file = Path(file_path)
        self.load_login_file()

    def load_login_file(self) -> None:
        self.login_entries = []
        try:
            if not self.login_file.exists():
                print(f'Login file not found: {self.login_file}')
                return
            with self.login_file.open('r', encoding='utf-8', errors='ignore') as fp:
                for raw in fp:
                    line = raw.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = [part.strip() for part in line.split('|')]
                    if len(parts) == 1:
                        email, password, domain = parts[0], '', ''
                    elif len(parts) == 2:
                        email, password, domain = parts[0], parts[1], ''
                    else:
                        email, password, domain = parts[0], parts[1], parts[2]
                    self.login_entries.append((email, password, domain))
            self._refresh_login_listbox()
            self.append_log(f'Loaded {len(self.login_entries)} login entries from {self.login_file}\n')
        except Exception as e:
            print(f'Error loading login file: {e}')
            self.append_log(f'Error loading login file: {e}\n')

    def save_login_file(self) -> None:
        if not self.login_entries:
            messagebox.showinfo('Save Login File', 'No login entries to save.')
            return
        with self.login_file.open('w', encoding='utf-8', newline='') as fp:
            for email, password, domain in self.login_entries:
                parts = [email, password]
                if domain:
                    parts.append(domain)
                fp.write('|'.join(parts) + '\n')
        self.append_log(f'Saved {len(self.login_entries)} login entries to {self.login_file}\n')

    def clear_login_list(self) -> None:
        self.login_entries = []
        self._refresh_login_listbox()
        self.append_log('Cleared login list\n')

    def add_current_login(self) -> None:
        email = self.email_var.get().strip()
        password = self.password_var.get().strip()
        if not email:
            messagebox.showwarning('Add Login', 'Email is required to add a login entry.')
            return
        domain = email.split('@', 1)[-1] if '@' in email else ''
        self.login_entries.append((email, password, domain))
        self._refresh_login_listbox()
        self.append_log(f'Added login entry: {email}\n')

    def _refresh_login_listbox(self) -> None:
        if self.login_listbox is None:
            return
        self.login_listbox.delete(0, 'end')
        for email, password, domain in self.login_entries:
            display = email
            if domain:
                display += f' ({domain})'
            self.login_listbox.insert('end', display)

    def _selected_login_index(self) -> int:
        if self.login_listbox is None:
            return -1
        selection = self.login_listbox.curselection()
        return selection[0] if selection else -1

    def _get_selected_login(self) -> tuple[str, str, str] | None:
        idx = self._selected_login_index()
        if idx < 0 or idx >= len(self.login_entries):
            return None
        return self.login_entries[idx]

    def _set_login_fields(self, email: str, password: str, domain: str = '') -> None:
        self.email_var.set(email)
        self.password_var.set(password)
        if domain:
            self.server_var.set(f'imap.{domain}')
            self.port_var.set('993')
            self.ssl_var.set(True)

    def use_selected_login(self) -> None:
        tmpl = self._get_selected_login()
        if tmpl is None:
            messagebox.showwarning('Use Login', 'No login entry selected.')
            return
        email, password, domain = tmpl
        self._set_login_fields(email, password, domain)
        self.append_log(f'Selected login: {email}\n')

    def copy_email_address(self) -> None:
        tmpl = self._get_selected_login()
        if tmpl is None:
            return
        self.clipboard_clear()
        self.clipboard_append(tmpl[0])
        self.append_log(f'Copied email address: {tmpl[0]}\n')

    def copy_password(self) -> None:
        tmpl = self._get_selected_login()
        if tmpl is None:
            return
        self.clipboard_clear()
        self.clipboard_append(tmpl[1])
        self.append_log('Copied password to clipboard\n')

    def copy_domain(self) -> None:
        tmpl = self._get_selected_login()
        if tmpl is None:
            return
        self.clipboard_clear()
        self.clipboard_append(tmpl[2])
        self.append_log(f'Copied domain: {tmpl[2]}\n')

    def on_login_double_click(self, event: tk.Event) -> None:
        self.use_selected_login()

    def on_login_right_click(self, event: tk.Event) -> None:
        if self.login_listbox is None:
            return
        nearest = self.login_listbox.nearest(event.y)
        if nearest < 0:
            return
        self.login_listbox.selection_clear(0, 'end')
        self.login_listbox.selection_set(nearest)
        if self.login_context_menu:
            self.login_context_menu.tk_popup(event.x_root, event.y_root)

    def office_oauth_imap(self) -> None:
        self._start_oauth_login('IMAP')

    def office_oauth_exchange(self) -> None:
        self._start_oauth_login('Exchange')

    def _check_webview_available(self) -> bool:
        try:
            import webview  # noqa: F401
            try:
                import webview.platforms  # noqa: F401
                return True
            except (ImportError, Exception):
                return False
        except ImportError:
            return False

    def _start_oauth_login(self, provider: str) -> None:
        if self._oauth_in_progress:
            messagebox.showinfo('OAuth In Progress', 'An OAuth login is already running.')
            return
        try:
            import msal  # noqa: F401
        except ImportError:
            messagebox.showerror('OAuth Missing', 'The msal library is required for OAuth login. Install it with: pip install msal')
            return

        email = self.email_var.get().strip()
        selected_login = self._get_selected_login()
        if selected_login is not None:
            email = selected_login[0]

        cli_id = self.oauth_client_id or 'e9a7fea1-1cc0-4cd9-a31b-9137ca5deedd'
        authority = self.oauth_authority.strip() if self.oauth_authority else 'https://login.microsoftonline.com/common'
        scope = ['https://outlook.office.com/IMAP.AccessAsUser.All'] if provider == 'IMAP' else ['https://outlook.office365.com/EWS.AccessAsUser.All']
        self._oauth_in_progress = True
        self._set_main_window_enabled(False)
        self.append_log(f'Starting OAuth login for {provider}...\n')

        use_webview = self._check_webview_available()
        if use_webview:
            self.append_log('Using embedded browser (pywebview) for OAuth...\n')
            window_x, window_y, window_width, window_height = self._get_oauth_window_geometry()
            threading.Thread(
                target=self._run_oauth_flow,
                args=(provider, cli_id, authority, email, scope, window_x, window_y, window_width, window_height),
                daemon=True,
            ).start()
        else:
            self.append_log('pywebview unavailable, using system browser for OAuth...\n')
            threading.Thread(
                target=self._run_oauth_flow_system_browser,
                args=(provider, cli_id, authority, email, scope),
                daemon=True,
            ).start()

    def _office_oauth(self, provider: str) -> None:
        if provider in ('IMAP', 'Exchange'):
            self._start_oauth_login(provider)
            return

    def _run_oauth_flow_system_browser(self, provider: str, cli_id: str, authority: str, email: str, scope: list[str]) -> None:
        try:
            import msal
        except ImportError as exc:
            self.after(0, lambda: messagebox.showerror('OAuth Error', f'msal library not available: {exc}'))
            self.after(0, self._clear_oauth_progress)
            self.after(0, lambda: self._set_main_window_enabled(True))
            return

        result_path = self._oauth_result_file
        if result_path is None:
            helper_fd, helper_path = tempfile.mkstemp(prefix='oauth_result_', suffix='.json')
            os.close(helper_fd)
            result_path = Path(helper_path)
            self._oauth_result_file = result_path

        redirect_uri = getattr(self, '_get_oauth_redirect_uri', lambda: 'https://login.microsoftonline.com/common/oauth2/nativeclient')()

        try:
            app = msal.PublicClientApplication(client_id=cli_id, authority=authority)
            flow = app.initiate_auth_code_flow(
                scopes=scope,
                redirect_uri=redirect_uri,
                login_hint=email or None,
                prompt='consent',
            )
            if 'auth_uri' not in flow:
                raise RuntimeError(f'Failed to create OAuth flow. Response: {flow}')

            auth_url = flow['auth_uri']
            self.append_log(f'Opening system browser for Microsoft login...\n')
            self.after(0, lambda: messagebox.showinfo(
                'OAuth Login',
                'A browser window will open for you to sign in to Microsoft.\n\n'
                'After signing in, you will be redirected to a blank page.\n'
                'Copy the FULL URL from the browser address bar and paste it below.\n\n'
                'Click OK to open the browser.'
            ))

            opened = webbrowser.open(auth_url)
            if not opened:
                self.append_log('Could not open browser automatically. Please open this URL manually:\n' + auth_url + '\n')

            self.after(0, lambda: self._prompt_for_oauth_redirect(provider, flow, result_path))

        except Exception as exc:
            self.after(0, lambda: messagebox.showerror('OAuth Error', f'Unable to start OAuth {provider}: {exc}'))
            self.after(0, lambda: self.append_log(f'OAuth {provider} startup failed: {exc}\n'))
            self.after(0, self._clear_oauth_progress)
            self.after(0, lambda: self._set_main_window_enabled(True))

    def _prompt_for_oauth_redirect(self, provider: str, flow: dict, result_path: Path) -> None:
        dialog = tk.Toplevel(self)
        dialog.title('OAuth - Paste Redirect URL')
        dialog.geometry('700x220')
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text='After signing in, copy the full URL from the browser address bar\nand paste it below:').pack(padx=12, pady=(12, 4))
        url_var = tk.StringVar()
        entry = ttk.Entry(dialog, textvariable=url_var, width=90)
        entry.pack(padx=12, pady=4)
        entry.focus_set()

        result_holder = {'payload': None}

        def submit():
            redirect_url = url_var.get().strip()
            if not redirect_url:
                messagebox.showwarning('OAuth', 'Please paste the redirect URL.', parent=dialog)
                return
            parsed = urllib.parse.urlparse(redirect_url)
            auth_response = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
            if 'code' not in auth_response and 'error' not in auth_response:
                auth_response_fragment = urllib.parse.parse_qs(parsed.fragment)
                for k, v_list in auth_response_fragment.items():
                    if v_list:
                        auth_response[k] = v_list[0]
            if 'code' not in auth_response:
                error_desc = auth_response.get('error_description', auth_response.get('error', 'No authorization code found in URL.'))
                result_holder['payload'] = {'status': 'error', 'error': str(error_desc)}
            else:
                try:
                    import msal
                    app = msal.PublicClientApplication(
                        client_id=flow.get('client_id', self.oauth_client_id),
                        authority=flow.get('authority', self.oauth_authority),
                    )
                    token_result = app.acquire_token_by_auth_code_flow(flow, auth_response)
                    if not token_result or 'access_token' not in token_result:
                        error_text = token_result.get('error_description', token_result) if isinstance(token_result, dict) else str(token_result)
                        result_holder['payload'] = {'status': 'error', 'error': str(error_text)}
                    else:
                        result_holder['payload'] = {
                            'status': 'ok',
                            'provider': provider,
                            'email': flow.get('login_hint', self.email_var.get().strip()),
                            'authority': flow.get('authority', self.oauth_authority),
                            'client_id': flow.get('client_id', self.oauth_client_id),
                            'token_result': token_result,
                        }
                except Exception as exc:
                    result_holder['payload'] = {'status': 'error', 'error': f'Token exchange failed: {exc}'}
            dialog.destroy()

        def cancel():
            result_holder['payload'] = {'status': 'error', 'error': 'OAuth cancelled by user.'}
            dialog.destroy()

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=8)
        ttk.Button(btn_frame, text='Submit', command=submit).pack(side='left', padx=8)
        ttk.Button(btn_frame, text='Cancel', command=cancel).pack(side='left', padx=8)

        dialog.protocol('WM_DELETE_WINDOW', cancel)
        self.wait_window(dialog)

        payload = result_holder['payload']
        if payload is not None:
            self._handle_oauth_helper_payload(provider, payload)
        else:
            self._clear_oauth_progress()
            self._set_main_window_enabled(True)

    def _get_oauth_redirect_uri(self) -> str:
        settings = core.IniLoader.load_settings(self.settings_path)
        return getattr(settings, 'oauth_redirect_uri', 'com.emclient.MailClient://oauth')

    def _run_oauth_flow(self, provider: str, cli_id: str, authority: str, email: str, scope: list[str], window_x: int, window_y: int, window_width: int, window_height: int) -> None:
        try:
            self._launch_oauth_helper(provider, cli_id, authority, email, scope, window_x, window_y, window_width, window_height)
        except Exception as exc:
            self.after(0, lambda: messagebox.showerror('OAuth Error', f'Unable to start OAuth {provider}: {exc}'))
            self.after(0, lambda: self.append_log(f'OAuth {provider} startup failed: {exc}\n'))
            self.after(0, self._clear_oauth_progress)
            self.after(0, self._set_main_window_enabled, True)

    def _launch_oauth_helper(self, provider: str, cli_id: str, authority: str, email: str, scope: list[str], window_x: int, window_y: int, window_width: int, window_height: int) -> None:
        helper_fd, helper_path = tempfile.mkstemp(prefix='oauth_result_', suffix='.json')
        os.close(helper_fd)
        result_path = Path(helper_path)
        self._oauth_result_file = result_path

        redirect_uri = self._get_oauth_redirect_uri()

        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            '--oauth-helper',
            '--provider',
            provider,
            '--client-id',
            cli_id,
            '--authority',
            authority,
            '--email',
            email,
            '--scope',
            '|'.join(scope),
            '--result-file',
            str(result_path),
            '--redirect-uri',
            redirect_uri,
            '--window-x',
            str(window_x),
            '--window-y',
            str(window_y),
            '--window-width',
            str(window_width),
            '--window-height',
            str(window_height),
        ]

        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        try:
            self._oauth_helper_proc = subprocess.Popen(cmd, creationflags=creationflags)
        except Exception as exc:
            self.append_log(f'Failed to launch OAuth helper subprocess: {exc}\nFalling back to system browser...\n')
            self._oauth_result_file = result_path
            self.after(0, self._clear_oauth_progress)
            self.after(0, lambda: self._set_main_window_enabled(True))
            threading.Thread(
                target=self._run_oauth_flow_system_browser,
                args=(provider, cli_id, authority, email, scope),
                daemon=True,
            ).start()
            return
        threading.Thread(
            target=self._watch_oauth_helper_result,
            args=(provider, result_path, self._oauth_helper_proc),
            daemon=True,
        ).start()

    def _watch_oauth_helper_result(self, provider: str, result_path: Path, proc: subprocess.Popen) -> None:
        deadline = time.time() + 600
        payload: dict[str, object] | None = None
        while time.time() < deadline:
            if result_path.exists():
                if result_path.stat().st_size == 0:
                    if proc.poll() is not None:
                        payload = {'status': 'error', 'error': f'OAuth helper exited with code {proc.returncode} before returning a result.'}
                        break
                    time.sleep(0.25)
                    continue
                try:
                    with result_path.open('r', encoding='utf-8') as fp:
                        payload = json.load(fp)
                    break
                except json.JSONDecodeError:
                    if proc.poll() is not None:
                        payload = {'status': 'error', 'error': f'OAuth helper exited with code {proc.returncode} before writing a complete result.'}
                        break
                    time.sleep(0.25)
                    continue
                except Exception as exc:
                    payload = {'status': 'error', 'error': f'Unable to read OAuth result: {exc}'}
                    break
            if proc.poll() is not None:
                payload = {'status': 'error', 'error': f'OAuth helper exited with code {proc.returncode} without producing a result.'}
                break
            time.sleep(0.25)

        if payload is None:
            payload = {'status': 'error', 'error': 'OAuth timed out waiting for completion.'}

        self.after(0, lambda: self._handle_oauth_helper_payload(provider, payload))

    def _handle_oauth_helper_payload(self, provider: str, payload: dict[str, object]) -> None:
        try:
            if payload.get('status') != 'ok':
                error_text = str(payload.get('error', 'OAuth failed.'))
                messagebox.showerror('OAuth Failed', error_text)
                self.append_log(f'OAuth {provider} failed: {error_text}\n')
                return

            token_result = payload.get('token_result')
            if not isinstance(token_result, dict):
                raise RuntimeError('OAuth helper returned an invalid token payload.')

            access_token = str(token_result.get('access_token', ''))
            if not access_token:
                raise RuntimeError('OAuth helper did not return an access token.')

            refresh_token = token_result.get('refresh_token', '')
            expires_in = token_result.get('expires_in', 3600)
            expiry_time = time.time() + int(expires_in) if isinstance(expires_in, (int, float)) and expires_in else None

            self._complete_oauth_login(
                provider=provider,
                email=str(payload.get('email', self.email_var.get().strip())),
                access_token=access_token,
                authority=str(payload.get('authority', self.oauth_authority)),
                cli_id=str(payload.get('client_id', self.oauth_client_id)),
                token_result=token_result,
                expires_in=expires_in,
                refresh_token=refresh_token,
                expiry_time=expiry_time,
            )
        finally:
            self._cleanup_oauth_helper()
            self._set_main_window_enabled(True)

    def _cleanup_oauth_helper(self) -> None:
        proc = self._oauth_helper_proc
        self._oauth_helper_proc = None
        result_path = self._oauth_result_file
        self._oauth_result_file = None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        if result_path:
            try:
                result_path.unlink(missing_ok=True)
            except Exception:
                pass
        self._set_main_window_enabled(True)

    def _get_oauth_window_geometry(self) -> tuple[int, int, int, int]:
        self.update_idletasks()
        try:
            main_x = self.winfo_rootx()
            main_y = self.winfo_rooty()
            main_width = max(self.winfo_width(), 1)
            main_height = max(self.winfo_height(), 1)
        except tk.TclError:
            return 120, 120, 840, 620

        width = max(720, min(900, main_width - 180))
        height = max(540, min(680, main_height - 160))
        x = main_x + max((main_width - width) // 2, 0)
        y = main_y + max((main_height - height) // 2, 0)
        return x, y, width, height

    def _set_main_window_enabled(self, enabled: bool) -> None:
        try:
            self.attributes('-disabled', not enabled)
        except tk.TclError:
            pass

    def _complete_oauth_login(self, provider: str, email: str, access_token: str, authority: str, cli_id: str, token_result: dict[str, object] | None = None, expires_in: object = '', refresh_token: str = '', expiry_time: float | None = None) -> None:
        self.oauth_session = OAuthSessionState(
            provider=provider,
            email=email,
            access_token=access_token,
            authority=authority,
            client_id=cli_id,
            token_result=token_result,
            refresh_token=refresh_token or None,
            token_expiry=expiry_time,
        )
        
        # Save token securely
        if token_result:
            self.save_oauth_token(provider, email, {
                'access_token': access_token,
                'refresh_token': refresh_token,
                'expires_in': expires_in,
                'token_result': token_result,
            })
        
        self.email_var.set(email)
        self.provider_var.set(provider)
        if provider == 'IMAP':
            self.server_var.set('outlook.office365.com')
            self.port_var.set('993')
            self.ssl_var.set(True)
        else:
            self.server_var.set('')
            self.port_var.set('')
        self.password_var.set('')
        expiry_note = f' (expires in {expires_in}s)' if isinstance(expires_in, (int, float)) and expires_in else ''
        self.append_log(f'OAuth succeeded for {provider}. Token acquired{expiry_note}.\n')
        try:
            self.oauth_status_label.configure(text=f'OAuth: {provider} token active', foreground='green')
        except Exception:
            pass
        self._clear_oauth_progress()
        
        # Auto-start fetch without messagebox
        self.append_log(f'Starting automatic email fetch after OAuth login...\n')
        self.after(500, self.start_fetch)

    def _clear_oauth_progress(self) -> None:
        self._oauth_in_progress = False

    def _try_refresh_oauth_token(self, provider: str, email_addr: str) -> None:
        if not self.oauth_session or not self.oauth_session.refresh_token:
            return
        try:
            import msal
            app = msal.PublicClientApplication(
                client_id=self.oauth_session.client_id,
                authority=self.oauth_session.authority,
            )
            result = app.acquire_token_by_refresh_token(
                self.oauth_session.refresh_token,
                scopes=['https://outlook.office.com/IMAP.AccessAsUser.All'] if provider == 'IMAP' else ['https://outlook.office365.com/EWS.AccessAsUser.All'],
            )
            if result and 'access_token' in result:
                access_token = result['access_token']
                refresh_token = result.get('refresh_token', self.oauth_session.refresh_token)
                expires_in = result.get('expires_in', 3600)
                expiry_time = time.time() + int(expires_in) if isinstance(expires_in, (int, float)) else None
                self.oauth_session = OAuthSessionState(
                    provider=provider,
                    email=email_addr,
                    access_token=access_token,
                    authority=self.oauth_session.authority,
                    client_id=self.oauth_session.client_id,
                    token_result=result,
                    refresh_token=refresh_token,
                    token_expiry=expiry_time,
                )
                self.append_log(f'OAuth token refreshed successfully (expires in {expires_in}s).\n')
                try:
                    self.oauth_status_label.configure(text=f'OAuth: {provider} token active', foreground='green')
                except Exception:
                    pass
            else:
                error_desc = result.get('error_description', result) if isinstance(result, dict) else str(result)
                self.append_log(f'OAuth token refresh failed: {error_desc}\n')
        except Exception as exc:
            self.append_log(f'OAuth token refresh error: {exc}\n')

    def _build_fetch_settings(self) -> core.FetchSettings:
        settings = core.FetchSettings(
            thread_count=int(self.thread_count_var.get() or '1'),
            save_content=self.save_content_var.get(),
            save_attachments=self.save_attachments_var.get(),
            save_correct_account=self.correct_account_var.get(),
            save_separated_file=self.separated_file_var.get(),
            save_email_result=self.save_results_var.get(),
            keyword=self.keyword_var.get().strip(),
            date_from=core.parse_date_option(self.date_from_var.get().strip()) if self.date_from_var.get().strip() else None,
            date_to=core.parse_date_option(self.date_to_var.get().strip()) if self.date_to_var.get().strip() else None,
            search_subject=self.search_subject_var.get(),
            search_body=self.search_body_var.get(),
            save_log=self.save_logs_var.get(),
            connection_timeout=int(self.timeout_var.get() or '30'),
        )
        return settings

    def _build_fetch_config(self) -> core.FetchConfig:
        def read_list(widget: tk.Text) -> list[str]:
            return [line.strip() for line in widget.get('1.0', 'end').splitlines() if line.strip()]

        return core.FetchConfig(
            folder_whitelist=read_list(self.folder_whitelist_text),
            folder_blacklist=read_list(self.folder_blacklist_text),
            attachment_whitelist=[item.lower() for item in read_list(self.attachment_whitelist_text)],
            attachment_blacklist=[item.lower() for item in read_list(self.attachment_blacklist_text)],
        )

    def start_fetch(self) -> None:
        provider = self.provider_var.get()
        email_addr = self.email_var.get().strip()

        if self.oauth_session and self.oauth_session.provider == provider and self.oauth_session.email == email_addr:
            if self.oauth_session.token_expiry and time.time() > self.oauth_session.token_expiry - 60:
                if self.oauth_session.refresh_token:
                    self.append_log('OAuth token expired, attempting refresh...\n')
                    self._try_refresh_oauth_token(provider, email_addr)
                    if self.oauth_session.token_expiry and time.time() > self.oauth_session.token_expiry - 60:
                        messagebox.showwarning('OAuth Expired', 'OAuth token has expired and refresh failed. Please log in again.')
                        self.oauth_session = None
                        try:
                            self.oauth_status_label.configure(text='OAuth: token expired', foreground='orange')
                        except Exception:
                            pass
                        return
                else:
                    messagebox.showwarning('OAuth Expired', 'OAuth token has expired. Please log in again.')
                    self.oauth_session = None
                    try:
                        self.oauth_status_label.configure(text='OAuth: token expired', foreground='orange')
                    except Exception:
                        pass
                    return

        oauth_state = self.oauth_session if self.oauth_session and self.oauth_session.provider == provider and self.oauth_session.email == email_addr else None
        oauth_token = oauth_state.access_token if oauth_state and provider == 'IMAP' else None
        oauth_token_data = oauth_state.token_result if oauth_state and provider == 'Exchange' else None
        if not email_addr or (not self.password_var.get() and not oauth_token and not oauth_token_data):
            messagebox.showwarning('Missing Credentials', f'Enter email and password, or complete OAuth {provider} login before starting the fetch.')
            return
        try:
            fetcher = self._create_fetcher()
        except Exception as exc:
            messagebox.showerror('Fetch Error', str(exc))
            return
        self.fetcher = fetcher
        self.start_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.status_label.configure(text='Running...')
        self._clear_status_tree()
        self._update_status(email_addr, 'Initializing...', '')
        threading.Thread(target=self._run_fetch, args=(fetcher,), daemon=True).start()

    def stop_fetch(self) -> None:
        if self.fetcher:
            self.fetcher.request_abort()
            self.append_log('Stop requested. Terminating as soon as possible...\n')
            self.stop_button.configure(state='disabled')

    def _create_fetcher(self) -> core.BaseFetcher:
        settings = self._build_fetch_settings()
        config = self._build_fetch_config()
        provider = self.provider_var.get()
        server = self.server_var.get().strip() or None
        port = int(self.port_var.get()) if self.port_var.get().strip().isdigit() else None
        ssl_enabled = self.ssl_var.get()
        email_addr = self.email_var.get().strip()
        password = self.password_var.get().strip()
        output_dir = Path(self.output_var.get().strip() or self.script_dir / 'output')

        oauth_state = self.oauth_session if self.oauth_session and self.oauth_session.provider == provider and self.oauth_session.email == email_addr else None
        oauth_token = oauth_state.access_token if oauth_state and provider == 'IMAP' else None
        oauth_token_data = oauth_state.token_result if oauth_state and provider == 'Exchange' else None

        if provider == 'IMAP' and oauth_token:
            server = 'outlook.office365.com'
            port = 993
            ssl_enabled = True

        if provider in ('IMAP', 'POP3') and not server:
            if '@' not in email_addr:
                raise ValueError('Enter a valid email address to autodetect the server.')
            domain = email_addr.split('@', 1)[1]
            server, port, enc, _ = core.ServerResolver.choose_server(domain, provider, core.IniLoader.load_server_rules(self.server_path))
            if enc and enc.upper() == 'SSL':
                ssl_enabled = True
        if provider in ('IMAP', 'POP3') and not server:
            raise ValueError('Cannot determine server address for the selected provider.')

        if provider == 'IMAP':
            if not password and not oauth_token:
                raise ValueError('Enter a password or complete OAuth IMAP login before starting the fetch.')
            return core.IMAPFetcher(email_addr, password, server, port, ssl_enabled, settings, config, output_dir, oauth_access_token=oauth_token)
        if provider == 'POP3':
            return core.POPFetcher(email_addr, password, server, port, ssl_enabled, settings, config, output_dir)
        if not core.EXCHANGE_AVAILABLE:
            raise RuntimeError('Exchange support requires exchangelib. Install it with pip install exchangelib')
        if not password and not oauth_token_data:
            raise ValueError('Enter a password or complete OAuth Exchange login before starting the fetch.')
        return core.ExchangeFetcher(
            email_addr,
            password,
            server,
            port,
            ssl_enabled,
            settings,
            config,
            output_dir,
            oauth_access_token=None,
            oauth_token_data=oauth_token_data,
            oauth_client_id=self.oauth_client_id,
        )

    def _run_fetch(self, fetcher: core.BaseFetcher) -> None:
        try:
            def status_update_callback(email: str, connection_status: str, fetch_progress: str) -> None:
                """Callback for status updates from the fetch process."""
                self.after(0, self._update_status, email or fetcher.email_address, connection_status, fetch_progress)
            
            sys.stdout = StatusAwareTextRedirector(self.append_log, status_update_callback)
            try:
                self.append_log('Starting fetch operation...\n')
                fetcher.fetch()
                self.append_log('Fetch operation completed.\n')
                self.after(0, self._update_status, fetcher.email_address, '', 'Completed')
            finally:
                sys.stdout = self.stdout_backup

        except InterruptedError:
            self.append_log('Fetch aborted by user.\n')
        except Exception as exc:
            message = str(exc)
            self.append_log(f'Error: {message}\n')
            self.after(0, lambda: messagebox.showerror('Fetch Error', message))
        finally:
            self.after(0, self._finish_fetch)

    def _finish_fetch(self) -> None:
        self.start_button.configure(state='normal')
        self.stop_button.configure(state='disabled')
        self.status_label.configure(text='Ready')

    def append_log(self, message: str) -> None:
        if threading.get_ident() != self.main_thread_id:
            self.log_queue.put(message)
            return
        self._append_log_now(message)

    def _append_log_now(self, message: str) -> None:
        if self.log_text is None:
            print(message, end='')  # Fall back to stdout if log_text not ready
            return
        self.log_text.configure(state='normal')
        self.log_text.insert('end', message)
        self.log_text.see('end')
        self.log_text.configure(state='disabled')

    def _drain_log_queue(self) -> None:
        try:
            while True:
                self._append_log_now(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(75, self._drain_log_queue)

    def _process_status_updates(self) -> None:
        """Process status updates from the fetcher thread."""
        try:
            while True:
                update = self.status_updates_queue.get_nowait()
                self._apply_status_update(update)
        except queue.Empty:
            pass
        self.after(75, self._process_status_updates)

    def _apply_status_update(self, update: dict) -> None:
        """Apply a status update to the tree view."""
        if self.status_tree is None:
            return
        
        email = update.get('email', '')
        connection_status = update.get('connection_status', '')
        fetch_progress = update.get('fetch_progress', '')
        
        # Find or create tree item for this email
        item_id = self.status_tree_items.get(email)
        if item_id is None:
            item_id = self.status_tree.insert('', 'end', values=(email, connection_status, fetch_progress))
            self.status_tree_items[email] = item_id
        else:
            # Update existing item
            self.status_tree.item(item_id, values=(email, connection_status, fetch_progress))

    def _clear_status_tree(self) -> None:
        """Clear all items from the status tree."""
        if self.status_tree is None:
            return
        for item in self.status_tree.get_children():
            self.status_tree.delete(item)
        self.status_tree_items.clear()

    def _update_status(self, email: str, connection_status: str = '', fetch_progress: str = '') -> None:
        """Queue a status update from the main thread."""
        self.status_updates_queue.put({
            'email': email,
            'connection_status': connection_status,
            'fetch_progress': fetch_progress,
        })

    def on_close(self) -> None:
        if self.fetcher and not self.fetcher.abort_requested:
            if not messagebox.askokcancel('Quit', 'A fetch is in progress. Abort and quit?'):
                return
            self.fetcher.request_abort()
        self._cleanup_oauth_helper()
        self.destroy()


def _write_oauth_result(result_path: Path, payload: dict[str, object]) -> None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    with result_path.open('w', encoding='utf-8') as fp:
        json.dump(payload, fp)
        fp.flush()
        os.fsync(fp.fileno())


def _run_oauth_helper_process() -> None:
    result_path = None
    try:
            parser = argparse.ArgumentParser(add_help=False)
            parser.add_argument('--oauth-helper', action='store_true')
            parser.add_argument('--provider', required=True)
            parser.add_argument('--client-id', required=True)
            parser.add_argument('--authority', required=True)
            parser.add_argument('--email', default='')
            parser.add_argument('--scope', required=True)
            parser.add_argument('--result-file', required=True)
            parser.add_argument('--redirect-uri', default='com.emclient.MailClient://oauth')
            parser.add_argument('--window-x', type=int, default=120)
            parser.add_argument('--window-y', type=int, default=120)
            parser.add_argument('--window-width', type=int, default=840)
            parser.add_argument('--window-height', type=int, default=620)
            args, _ = parser.parse_known_args()

            if not args.oauth_helper:
                return

            result_path = Path(args.result_file)

            try:
                import msal
            except ImportError as exc:
                _write_oauth_result(result_path, {'status': 'error', 'error': f'OAuth support is unavailable: {exc}'})
                return

            scope = [item for item in args.scope.split('|') if item]
            redirect_uri = args.redirect_uri

            webview_available = False
            try:
                import webview
                try:
                    import webview.platforms  # noqa: F401
                    webview_available = True
                except (ImportError, Exception):
                    webview_available = False
            except ImportError:
                pass

            # When using custom protocol handlers (not localhost), always use browser flow
            # because the callback happens in a separate process via protocol handler
            if redirect_uri.startswith('com.emclient.MailClient://') or not redirect_uri.startswith('http'):
                webview_available = False

            app = msal.PublicClientApplication(client_id=args.client_id, authority=args.authority)
            flow = app.initiate_auth_code_flow(
                scopes=scope,
                redirect_uri=redirect_uri,
                login_hint=args.email or None,
                prompt='consent',
            )

            if 'auth_uri' not in flow:
                _write_oauth_result(result_path, {'status': 'error', 'error': f'Failed to create OAuth flow: {flow}'})
                return

            if webview_available:
                _oauth_helper_webview_flow(app, flow, scope, result_path, args)
            else:
                _oauth_helper_browser_flow(app, flow, scope, result_path, args)

    except Exception as exc:
        error_msg = f'OAuth helper process failed: {type(exc).__name__}: {exc}'
        if result_path:
            try:
                _write_oauth_result(result_path, {'status': 'error', 'error': error_msg})
            except Exception:
                pass
        sys.stderr.write(error_msg + '\n')
        sys.stderr.flush()
        sys.exit(1)


def _oauth_helper_webview_flow(app, flow: dict, scope: list[str], result_path: Path, args) -> None:
    try:
        import webview
        import msal
    except ImportError as exc:
        _write_oauth_result(result_path, {'status': 'error', 'error': f'Required module not available: {exc}'})
        return

    # Note: With protocol handlers (custom schemes), we should use browser flow instead
    # because webview.start() will block and the callback happens in a separate process
    # Fall through to browser flow
    _oauth_helper_browser_flow(app, flow, scope, result_path, args)


def _oauth_helper_browser_flow(app, flow: dict, scope: list[str], result_path: Path, args) -> None:
    try:
        import webbrowser
        import msal

        auth_url = flow['auth_uri']

        pkce_file = result_path.parent / f'pkce_{result_path.stem}.json'
        with pkce_file.open('w', encoding='utf-8') as fp:
            json.dump({
                'flow': flow,
                'result_file': str(result_path),
                'client_id': args.client_id,
                'authority': args.authority,
            }, fp)

        opened = webbrowser.open(auth_url)

        print(f'OAuth: Sign-in browser opened. Waiting for callback at {args.redirect_uri}')
        if not opened:
            print(f'OAuth: Could not open browser. Open this URL manually:\n{auth_url}')

        deadline = time.time() + 600
        while time.time() < deadline:
            if result_path.exists() and result_path.stat().st_size > 0:
                try:
                    with result_path.open('r', encoding='utf-8') as fp:
                        result = json.load(fp)
                    if result.get('status') in ('ok', 'error'):
                        break
                except json.JSONDecodeError:
                    pass
            time.sleep(0.5)

        try:
            pkce_file.unlink()
        except Exception:
            pass

    except Exception as exc:
        _write_oauth_result(result_path, {'status': 'error', 'error': f'Browser OAuth flow failed: {exc}'})


def _handle_oauth_callback(callback_uri: str) -> None:
    """Handle OAuth callback from protocol handler."""
    log_file = Path(tempfile.gettempdir()) / 'oauth_callback_debug.log'

    def log_msg(msg: str):
        try:
            with log_file.open('a', encoding='utf-8') as f:
                f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {msg}\n')
        except Exception:
            pass
        try:
            print(f'OAuth: {msg}')
        except Exception:
            pass

    log_msg(f'Callback handler invoked with URI: {callback_uri}')

    try:
        import msal

        # Parse callback URI
        callback_params = ProtocolHandler.parse_callback_uri(callback_uri)
        log_msg(f'Parsed callback params: code={callback_params.get("code", "")[:20]}..., state={callback_params.get("state", "")}')

        # Find matching PKCE file
        temp_dir = Path(tempfile.gettempdir())
        pkce_files = list(temp_dir.glob('pkce_oauth_result_*.json'))
        log_msg(f'Found {len(pkce_files)} PKCE files')

        if not pkce_files:
            log_msg('No pending OAuth request found.')
            return

        # Use the most recent PKCE file
        pkce_file = pkce_files[-1]
        log_msg(f'Using PKCE file: {pkce_file.name}')

        try:
            with pkce_file.open('r', encoding='utf-8') as fp:
                pkce_data = json.load(fp)
        except Exception as e:
            log_msg(f'Failed to read PKCE data: {e}')
            return

        log_msg(f'PKCE data keys: {list(pkce_data.keys())}')

        result_file = Path(pkce_data['result_file'])

        # Check for error in callback
        if 'error' in callback_params:
            error_desc = callback_params.get('error_description', callback_params.get('error', 'Unknown error'))
            log_msg(f'Error in callback: {error_desc}')
            _write_oauth_result(result_file, {'status': 'error', 'error': str(error_desc)})
            return

        # Extract code
        if 'code' not in callback_params:
            log_msg('No authorization code in callback.')
            _write_oauth_result(result_file, {'status': 'error', 'error': 'No authorization code in callback.'})
            return

        log_msg('Authorization code found. Exchanging for token...')

        # Exchange authorization code for access token using MSAL
        try:
            # Create MSAL app with stored credentials
            local_app = msal.PublicClientApplication(
                client_id=pkce_data['client_id'],
                authority=pkce_data['authority']
            )
            log_msg(f'Created MSAL app for {pkce_data["client_id"]}')

            # Exchange code for tokens
            flow = pkce_data['flow']
            log_msg('Calling acquire_token_by_auth_code_flow...')
            token_result = local_app.acquire_token_by_auth_code_flow(flow, callback_params)
            log_msg(f'Token exchange result keys: {list(token_result.keys())}')

            # Check if token exchange was successful
            if 'access_token' in token_result:
                # Extract email from token result if available
                email = ''
                if 'account' in token_result and isinstance(token_result['account'], dict):
                    email = token_result['account'].get('username', '')

                log_msg(f'Token exchange successful! Email: {email}')
                _write_oauth_result(result_file, {
                    'status': 'ok',
                    'token_result': token_result,
                    'email': email,
                    'authority': pkce_data['authority'],
                    'client_id': pkce_data['client_id'],
                })
                log_msg('Result written to file.')
            else:
                error = token_result.get('error', 'Unknown error')
                error_desc = token_result.get('error_description', error)
                log_msg(f'Token exchange failed: {error_desc}')
                _write_oauth_result(result_file, {
                    'status': 'error',
                    'error': str(error_desc),
                })
        except Exception as exc:
            log_msg(f'Token exchange exception: {type(exc).__name__}: {exc}')
            import traceback
            log_msg(traceback.format_exc())
            _write_oauth_result(result_file, {
                'status': 'error',
                'error': f'Token exchange failed: {exc}'
            })

    except Exception as exc:
        log_msg(f'Callback handling failed: {type(exc).__name__}: {exc}')
        import traceback
        log_msg(traceback.format_exc())


if __name__ == '__main__':
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w')

    if '--oauth-callback' in sys.argv:
        callback_idx = sys.argv.index('--oauth-callback')
        if callback_idx + 1 < len(sys.argv):
            callback_uri = sys.argv[callback_idx + 1]
            _handle_oauth_callback(callback_uri)
    elif '--oauth-helper' in sys.argv:
        _run_oauth_helper_process()
    else:
        app = MailListFetcherGUI()
        app.mainloop()
