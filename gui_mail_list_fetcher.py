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
from dataclasses import dataclass, field
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
        return base64.urlsafe_b64encode(secrets.token_bytes(length // 2)).decode('utf-8').rstrip('=')

    @staticmethod
    def generate_code_challenge(code_verifier: str) -> str:
        code_sha = hashlib.sha256(code_verifier.encode('utf-8')).digest()
        return base64.urlsafe_b64encode(code_sha).decode('utf-8').rstrip('=')


class ProtocolHandler:
    PROTOCOL_SCHEME = 'com.emclient.MailClient'
    REGISTRY_PATH = f'HKEY_CLASSES_ROOT\\{PROTOCOL_SCHEME}'

    @staticmethod
    def register_protocol_handler(app_exe_path: str | None = None) -> bool:
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
        parsed = urllib.parse.urlparse(uri)
        params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        return params


class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for OAuth callback from the browser."""
    
    callback_data = {}
    result_file_path = None
    
    def do_GET(self):
        """Handle GET request from OAuth callback."""
        try:
            # Parse query parameters
            parsed_url = urllib.parse.urlparse(self.path)
            params = dict(urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=True))
            
            # Store the callback data
            OAuthCallbackHandler.callback_data = params
            
            # Send success response
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            
            html_response = """
            <html>
            <head><title>OAuth Sign-in Successful</title></head>
            <body style="font-family: Arial, sans-serif; text-align: center; padding: 50px;">
                <h1>✓ Sign-in Successful</h1>
                <p>You have successfully signed in.</p>
                <p>You can close this window and return to the application.</p>
            </body>
            </html>
            """
            self.wfile.write(html_response.encode())
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f'Error: {e}'.encode())
    
    def log_message(self, format, *args):
        """Suppress default logging."""
        pass


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


STATUS_PENDING = 'Pending'
STATUS_DETECTING = 'Detecting servers...'
STATUS_TRYING_IMAP = 'Trying IMAP login...'
STATUS_TRYING_POP3 = 'Trying POP3 login...'
STATUS_IMAP_FAILED = 'IMAP login failed'
STATUS_POP3_FAILED = 'POP3 login failed'
STATUS_LOGIN_FAILED = 'Login failed'
STATUS_LOGGED_IN = 'Logged in'
STATUS_OAUTH_PENDING = 'OAuth pending...'
STATUS_FETCHING = 'Fetching...'
STATUS_COMPLETED = 'Completed'
STATUS_ERROR = 'Error'
STATUS_STOPPED = 'Stopped'


@dataclass
class LoginEntry:
    email: str
    password: str
    domain: str
    status: str = STATUS_PENDING
    protocol: str = ''
    server: str = ''
    progress: str = ''
    oauth_session: OAuthSessionState | None = None
    fetcher: core.BaseFetcher | None = None
    fetching: bool = False


class MailListFetcherGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title('Mail List Fetcher v2.5')
        self.geometry('1280x860')
        self.minsize(1100, 780)
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

        self.provider_var = tk.StringVar(value='IMAP')
        self.email_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.server_var = tk.StringVar()
        self.port_var = tk.StringVar(value='993')
        self.ssl_var = tk.BooleanVar(value=True)
        self.output_var = tk.StringVar(value=str(self.script_dir / 'output'))
        self.login_file = self.script_dir / 'logins.txt'
        self.login_entries: list[LoginEntry] = []
        self.oauth_client_id = 'e9a7fea1-1cc0-4cd9-a31b-9137ca5deedd'
        self.oauth_authority = 'https://login.microsoftonline.com/common'
        self.oauth_session: OAuthSessionState | None = None
        self._oauth_in_progress = False
        self._oauth_helper_proc: subprocess.Popen | None = None
        self._oauth_result_file: Path | None = None
        self._auto_login_for_entry: LoginEntry | None = None
        self._server_options: list[tuple[str, str, str | None, int | None, str | None]] = []

        self.keyword_var = tk.StringVar()
        self.date_from_var = tk.StringVar()
        self.date_to_var = tk.StringVar()
        self.search_subject_var = tk.BooleanVar(value=True)
        self.search_body_var = tk.BooleanVar(value=True)

        # Defaults: only extract email addresses by default. Turn off saving content/attachments/logs.
        self.save_content_var = tk.BooleanVar(value=False)
        self.save_attachments_var = tk.BooleanVar(value=False)
        self.save_results_var = tk.BooleanVar(value=True)
        self.save_logs_var = tk.BooleanVar(value=False)
        self.separated_file_var = tk.BooleanVar(value=False)
        self.correct_account_var = tk.BooleanVar(value=False)
        self.thread_count_var = tk.StringVar(value='5')
        self.batch_size_var = tk.StringVar(value='50')
        
        self.extract_subject_var = tk.BooleanVar(value=False)
        self.extract_date_var = tk.BooleanVar(value=False)
        self.extract_attachments_list_var = tk.BooleanVar(value=False)
        self.extract_summary_var = tk.BooleanVar(value=False)

        self.folder_whitelist_text: tk.Text | None = None
        self.folder_blacklist_text: tk.Text | None = None
        self.attachment_whitelist_text: tk.Text | None = None
        self.attachment_blacklist_text: tk.Text | None = None
        self.log_text: tk.Text | None = None
        self.login_tree: ttk.Treeview | None = None
        self.server_combo: ttk.Combobox | None = None

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
        style.configure('Status.Treeview', rowheight=24)
        style.configure('Status.Treeview.Heading', font=('Segoe UI', 9, 'bold'))
        self.configure(background='#f5f7fb')

    def _register_oauth_protocol_handler(self) -> None:
        try:
            script_path = Path(__file__).resolve()
            success = ProtocolHandler.register_protocol_handler(str(script_path))
            if success:
                self.append_log('OAuth protocol handler registered successfully.\n')
            else:
                self.append_log('Note: OAuth protocol handler registration requires admin privileges.\n')
        except Exception as e:
            self.append_log(f'Protocol handler registration skipped: {e}\n')

    def _get_secure_token_dir(self) -> Path:
        token_dir = self.script_dir / '.oauth_tokens'
        token_dir.mkdir(exist_ok=True, mode=0o700)
        return token_dir

    def save_oauth_token(self, provider: str, email: str, token_data: dict) -> bool:
        try:
            token_dir = self._get_secure_token_dir()
            token_file = token_dir / f'{provider}_{email}.json'
            token_data['provider'] = provider
            token_data['email'] = email
            token_data['saved_at'] = time.time()
            with token_file.open('w', encoding='utf-8') as fp:
                json.dump(token_data, fp)
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
        connection_frame.pack(fill='x', pady=(0, 6))

        provider_frame = ttk.Frame(connection_frame)
        provider_frame.grid(row=0, column=0, columnspan=6, sticky='w', pady=(4, 8))
        ttk.Label(provider_frame, text='Provider:').pack(side='left', padx=(0, 8))
        for provider in ['IMAP', 'POP3', 'Exchange']:
            ttk.Radiobutton(provider_frame, text=provider, variable=self.provider_var, value=provider, command=self._update_port_label).pack(side='left', padx=6)

        ttk.Label(connection_frame, text='Email:').grid(row=1, column=0, sticky='w', padx=4, pady=2)
        ttk.Entry(connection_frame, textvariable=self.email_var, width=32).grid(row=1, column=1, sticky='w', padx=4, pady=2)
        ttk.Label(connection_frame, text='Password:').grid(row=1, column=2, sticky='w', padx=4, pady=2)
        ttk.Entry(connection_frame, textvariable=self.password_var, show='*', width=32).grid(row=1, column=3, sticky='w', padx=4, pady=2)

        ttk.Label(connection_frame, text='Server:').grid(row=2, column=0, sticky='w', padx=4, pady=2)
        server_frame = ttk.Frame(connection_frame)
        server_frame.grid(row=2, column=1, columnspan=3, sticky='w', padx=4, pady=2)
        self.server_combo = ttk.Combobox(server_frame, textvariable=self.server_var, width=38, state='normal')
        self.server_combo.pack(side='left', fill='x', expand=True)
        self.server_combo.bind('<<ComboboxSelected>>', self._on_server_selected)

        ttk.Label(connection_frame, text='Port:').grid(row=2, column=4, sticky='w', padx=4, pady=2)
        ttk.Entry(connection_frame, textvariable=self.port_var, width=8).grid(row=2, column=5, sticky='w', padx=4, pady=2)

        ttk.Checkbutton(connection_frame, text='Use SSL/TLS', variable=self.ssl_var).grid(row=3, column=0, sticky='w', padx=4, pady=2)
        ttk.Button(connection_frame, text='Auto Detect Server', command=self.auto_detect_server).grid(row=3, column=1, sticky='w', padx=4, pady=2)
        ttk.Button(connection_frame, text='Detect All Servers', command=self._detect_all_servers).grid(row=3, column=2, sticky='w', padx=4, pady=2)

        oauth_frame = ttk.Frame(connection_frame)
        oauth_frame.grid(row=4, column=0, columnspan=6, sticky='w', pady=(4, 2))
        ttk.Label(oauth_frame, text='OAuth:').pack(side='left', padx=(0, 4))
        ttk.Button(oauth_frame, text='OAuth IMAP (Microsoft)', command=self.office_oauth_imap).pack(side='left', padx=4)
        ttk.Button(oauth_frame, text='OAuth Exchange (Microsoft)', command=self.office_oauth_exchange).pack(side='left', padx=4)
        self.oauth_status_label = ttk.Label(oauth_frame, text='No OAuth token', foreground='gray')
        self.oauth_status_label.pack(side='left', padx=(12, 0))

        action_frame = ttk.Frame(container)
        action_frame.pack(fill='x', pady=(0, 6))
        self.start_button = ttk.Button(action_frame, text='Start Fetch', command=self.start_fetch)
        self.start_button.pack(side='left', padx=(0, 6))
        self.stop_button = ttk.Button(action_frame, text='Stop', command=self.stop_fetch, state='disabled')
        self.stop_button.pack(side='left', padx=(0, 6))
        ttk.Button(action_frame, text='Login All', command=self._login_all_entries).pack(side='left', padx=(0, 6))
        ttk.Button(action_frame, text='Fetch All Logged In', command=self._fetch_all_logged_in).pack(side='left', padx=(0, 6))
        ttk.Button(action_frame, text='Save Current Settings', command=self.save_current_settings).pack(side='left', padx=(0, 6))
        ttk.Button(action_frame, text='Reload Config Files', command=self.load_configuration).pack(side='left', padx=(0, 6))

        notebook = ttk.Notebook(container)
        notebook.pack(fill='both', expand=True, pady=(0, 6))

        self._build_login_tab(notebook)
        self._build_search_tab(notebook)
        self._build_filters_tab(notebook)
        self._build_options_tab(notebook)

        log_detail_frame = ttk.LabelFrame(container, text='Detailed Logs')
        log_detail_frame.pack(fill='both', expand=True, pady=(0, 6))
        self.log_text = tk.Text(log_detail_frame, wrap='word', state='disabled', height=6)
        log_scroll = ttk.Scrollbar(log_detail_frame, orient='vertical', command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side='left', fill='both', expand=True, padx=(4, 0), pady=4)
        log_scroll.pack(side='right', fill='y', pady=4)

        status_frame = ttk.Frame(container)
        status_frame.pack(fill='x', pady=(0, 0))
        self.status_label = ttk.Label(status_frame, text='Ready')
        self.status_label.pack(side='left', padx=4)

    def _build_login_tab(self, notebook: ttk.Notebook) -> None:
        login_tab = ttk.Frame(notebook)
        notebook.add(login_tab, text='Login & Status Log')

        top_frame = ttk.Frame(login_tab)
        top_frame.pack(fill='x', padx=4, pady=(4, 2))

        btn_frame = ttk.Frame(top_frame)
        btn_frame.pack(side='left', fill='y')
        ttk.Button(btn_frame, text='Load Logins...', command=self.choose_login_file).pack(fill='x', padx=2, pady=1)
        ttk.Button(btn_frame, text='Add Current Login', command=self.add_current_login).pack(fill='x', padx=2, pady=1)
        ttk.Button(btn_frame, text='Save Login File', command=self.save_login_file).pack(fill='x', padx=2, pady=1)
        ttk.Button(btn_frame, text='Clear List', command=self.clear_login_list).pack(fill='x', padx=2, pady=1)
        ttk.Button(btn_frame, text='Remove Selected', command=self._remove_selected_login).pack(fill='x', padx=2, pady=1)

        info_label = ttk.Label(top_frame, text='Format: email|password|domain(optional)  |  Double-click: auto-login  |  Right-click: OAuth options', foreground='#6b7280')
        info_label.pack(side='left', padx=12)

        tree_frame = ttk.Frame(login_tab)
        tree_frame.pack(fill='both', expand=True, padx=4, pady=4)

        columns = ('email', 'protocol', 'server', 'status', 'progress')
        self.login_tree = ttk.Treeview(tree_frame, columns=columns, show='headings', selectmode='extended', height=14)
        self.login_tree.heading('email', text='Email Address')
        self.login_tree.heading('protocol', text='Protocol')
        self.login_tree.heading('server', text='Server')
        self.login_tree.heading('status', text='Status')
        self.login_tree.heading('progress', text='Progress')
        self.login_tree.column('email', width=220, anchor='w')
        self.login_tree.column('protocol', width=70, anchor='center')
        self.login_tree.column('server', width=200, anchor='w')
        self.login_tree.column('status', width=140, anchor='w')
        self.login_tree.column('progress', width=160, anchor='w')

        vscroll = ttk.Scrollbar(tree_frame, orient='vertical', command=self.login_tree.yview)
        self.login_tree.configure(yscrollcommand=vscroll.set)
        self.login_tree.pack(side='left', fill='both', expand=True)
        vscroll.pack(side='right', fill='y')

        self.login_tree.bind('<Double-Button-1>', self._on_login_double_click)
        self.login_tree.bind('<Button-3>', self._on_login_right_click)

        self.login_context_menu = tk.Menu(self, tearoff=0)
        self.login_context_menu.add_command(label='Auto Login (IMAP then POP3)', command=self._auto_login_selected)
        self.login_context_menu.add_command(label='Login with OAuth IMAP', command=self._oauth_login_selected_imap)
        self.login_context_menu.add_command(label='Login with OAuth Exchange', command=self._oauth_login_selected_exchange)
        self.login_context_menu.add_separator()
        self.login_context_menu.add_command(label='Start Fetch for Selected', command=self._fetch_selected)
        self.login_context_menu.add_separator()
        self.login_context_menu.add_command(label='Use in Top Section', command=self._use_selected_in_top)
        self.login_context_menu.add_command(label='Copy Email', command=self.copy_email_address)
        self.login_context_menu.add_command(label='Copy Password', command=self.copy_password)
        self.login_context_menu.add_separator()
        self.login_context_menu.add_command(label='Reset Status', command=self._reset_selected_status)
        self.login_context_menu.add_command(label='Remove Entry', command=self._remove_selected_login)

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
        
        # CSV Extraction Options
        extraction_frame = ttk.LabelFrame(options_tab, text='Extract to CSV (Email address always included)')
        extraction_frame.pack(fill='x', padx=8, pady=8)
        ttk.Checkbutton(extraction_frame, text='Extract Subject', variable=self.extract_subject_var).grid(row=0, column=0, sticky='w', padx=8, pady=4)
        ttk.Checkbutton(extraction_frame, text='Extract Date', variable=self.extract_date_var).grid(row=0, column=1, sticky='w', padx=8, pady=4)
        ttk.Checkbutton(extraction_frame, text='Extract Attachments Info', variable=self.extract_attachments_list_var).grid(row=1, column=0, sticky='w', padx=8, pady=4)
        ttk.Checkbutton(extraction_frame, text='Extract Summary', variable=self.extract_summary_var).grid(row=1, column=1, sticky='w', padx=8, pady=4)

        ttk.Label(options_frame, text='Thread Count:').grid(row=3, column=0, sticky='w', padx=8, pady=4)
        ttk.Entry(options_frame, textvariable=self.thread_count_var, width=8).grid(row=3, column=1, sticky='w', padx=8, pady=4)
        ttk.Label(options_frame, text='Batch Size:').grid(row=3, column=2, sticky='w', padx=8, pady=4)
        ttk.Entry(options_frame, textvariable=self.batch_size_var, width=8).grid(row=3, column=3, sticky='w', padx=8, pady=4)

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

    def _on_server_selected(self, event: tk.Event | None = None) -> None:
        idx = self.server_combo.current()
        if 0 <= idx < len(self._server_options):
            proto, server, port, encryption, url = self._server_options[idx]
            self.provider_var.set(proto if proto in ('IMAP', 'POP3', 'Exchange') else 'IMAP')
            if server:
                self.server_var.set(server)
            if port:
                self.port_var.set(str(port))
            if encryption and encryption.upper() == 'SSL':
                self.ssl_var.set(True)
            elif encryption and encryption.upper() in ('', 'NONE', 'STARTTLS'):
                self.ssl_var.set(False)

    def _detect_all_servers(self) -> None:
        email = self.email_var.get().strip()
        if '@' not in email:
            messagebox.showwarning('Detect Servers', 'Please enter a valid email address.')
            return
        domain = email.split('@', 1)[1]
        rules = core.IniLoader.load_server_rules(self.server_path)
        servers = core.ServerResolver.find_all_servers(domain, rules)
        self._server_options = servers
        values = []
        for proto, server, port, enc, url in servers:
            display = f'{proto}: {server or url}'
            if port:
                display += f':{port}'
            if enc:
                display += f' ({enc})'
            values.append(display)
        self.server_combo['values'] = values
        if values:
            self.server_combo.current(0)
            self._on_server_selected()
        self.append_log(f'Detected {len(servers)} server(s) for {domain}\n')

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
            self.extract_subject_var.set(settings.extract_subject)
            self.extract_date_var.set(settings.extract_date)
            self.extract_attachments_list_var.set(settings.extract_attachments_list)
            self.extract_summary_var.set(settings.extract_summary)
            self.thread_count_var.set(str(settings.thread_count))
            self.oauth_client_id = settings.oauth_client_id
            self.oauth_authority = settings.oauth_authority
            self.timeout_var.set(str(settings.connection_timeout))
            self.batch_size_var.set(str(settings.batch_size))

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
        settings.extract_subject = self.extract_subject_var.get()
        settings.extract_date = self.extract_date_var.get()
        settings.extract_attachments_list = self.extract_attachments_list_var.get()
        settings.extract_summary = self.extract_summary_var.get()
        settings.thread_count = int(self.thread_count_var.get() or '5')
        settings.oauth_client_id = self.oauth_client_id
        settings.oauth_authority = self.oauth_authority
        settings.connection_timeout = int(self.timeout_var.get() or '30')
        settings.batch_size = int(self.batch_size_var.get() or '50')

        parser = core.configparser.ConfigParser()
        parser['MailListFetcher'] = {
            'ThreadCount': str(settings.thread_count),
            'ChkSaveContent': '1' if settings.save_content else '0',
            'ChkSaveAttachment': '1' if settings.save_attachments else '0',
            'ChkSaveCorrectAccount': '1' if settings.save_correct_account else '0',
            'ChkToSeparatedFile': '1' if settings.save_separated_file else '0',
            'ChkSaveEmailResult': '1' if settings.save_email_result else '0',
            'ChkExtractSubject': '1' if settings.extract_subject else '0',
            'ChkExtractDate': '1' if settings.extract_date else '0',
            'ChkExtractAttachmentsList': '1' if settings.extract_attachments_list else '0',
            'ChkExtractSummary': '1' if settings.extract_summary else '0',
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
            'BatchSize': str(settings.batch_size),
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

    # ─── Login list management ───

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
                    if email:
                        self.login_entries.append(LoginEntry(email=email, password=password, domain=domain))
            self._refresh_login_tree()
            self.append_log(f'Loaded {len(self.login_entries)} login entries from {self.login_file}\n')
        except Exception as e:
            self.append_log(f'Error loading login file: {e}\n')

    def save_login_file(self) -> None:
        if not self.login_entries:
            messagebox.showinfo('Save Login File', 'No login entries to save.')
            return
        with self.login_file.open('w', encoding='utf-8', newline='') as fp:
            for entry in self.login_entries:
                parts = [entry.email, entry.password]
                if entry.domain:
                    parts.append(entry.domain)
                fp.write('|'.join(parts) + '\n')
        self.append_log(f'Saved {len(self.login_entries)} login entries to {self.login_file}\n')

    def clear_login_list(self) -> None:
        self.login_entries = []
        self._refresh_login_tree()
        self.append_log('Cleared login list\n')

    def add_current_login(self) -> None:
        email = self.email_var.get().strip()
        password = self.password_var.get().strip()
        if not email:
            messagebox.showwarning('Add Login', 'Email is required to add a login entry.')
            return
        domain = email.split('@', 1)[-1] if '@' in email else ''
        self.login_entries.append(LoginEntry(email=email, password=password, domain=domain))
        self._refresh_login_tree()
        self.append_log(f'Added login entry: {email}\n')

    def _refresh_login_tree(self) -> None:
        if self.login_tree is None:
            return
        self.login_tree.delete(*self.login_tree.get_children())
        for entry in self.login_entries:
            self.login_tree.insert('', 'end', values=(entry.email, entry.protocol, entry.server, entry.status, entry.progress))

    def _update_login_tree_row(self, entry: LoginEntry) -> None:
        if self.login_tree is None:
            return
        idx = self.login_entries.index(entry) if entry in self.login_entries else -1
        if idx < 0:
            return
        children = self.login_tree.get_children()
        if idx < len(children):
            self.login_tree.item(children[idx], values=(entry.email, entry.protocol, entry.server, entry.status, entry.progress))

    def _selected_login_indices(self) -> list[int]:
        if self.login_tree is None:
            return []
        selection = self.login_tree.selection()
        indices = []
        children = self.login_tree.get_children()
        for sel in selection:
            try:
                indices.append(children.index(sel))
            except ValueError:
                pass
        return indices

    def _selected_login_index(self) -> int:
        indices = self._selected_login_indices()
        return indices[0] if indices else -1

    def _get_selected_login(self) -> LoginEntry | None:
        idx = self._selected_login_index()
        if idx < 0 or idx >= len(self.login_entries):
            return None
        return self.login_entries[idx]

    def _set_login_fields(self, email: str, password: str, domain: str = '') -> None:
        self.email_var.set(email)
        self.password_var.set(password)
        if domain:
            rules = core.IniLoader.load_server_rules(self.server_path)
            server, port, enc, _ = core.ServerResolver.choose_server(domain, 'IMAP', rules)
            if server:
                self.server_var.set(server)
            if port:
                self.port_var.set(str(port))
            if enc and enc.upper() == 'SSL':
                self.ssl_var.set(True)

    def _use_selected_in_top(self) -> None:
        entry = self._get_selected_login()
        if entry is None:
            messagebox.showwarning('Use Login', 'No login entry selected.')
            return
        self._set_login_fields(entry.email, entry.password, entry.domain)
        self.append_log(f'Selected login: {entry.email}\n')

    def copy_email_address(self) -> None:
        entry = self._get_selected_login()
        if entry is None:
            return
        self.clipboard_clear()
        self.clipboard_append(entry.email)

    def copy_password(self) -> None:
        entry = self._get_selected_login()
        if entry is None:
            return
        self.clipboard_clear()
        self.clipboard_append(entry.password)

    def _remove_selected_login(self) -> None:
        indices = sorted(self._selected_login_indices(), reverse=True)
        for idx in indices:
            if 0 <= idx < len(self.login_entries):
                del self.login_entries[idx]
        self._refresh_login_tree()

    def _reset_selected_status(self) -> None:
        for idx in self._selected_login_indices():
            if 0 <= idx < len(self.login_entries):
                self.login_entries[idx].status = STATUS_PENDING
                self.login_entries[idx].protocol = ''
                self.login_entries[idx].server = ''
                self.login_entries[idx].progress = ''
                self.login_entries[idx].fetching = False
                self._update_login_tree_row(self.login_entries[idx])

    def _on_login_double_click(self, event: tk.Event) -> None:
        entry = self._get_selected_login()
        if entry is None:
            return
        self._auto_login_entry(entry)

    def _on_login_right_click(self, event: tk.Event) -> None:
        if self.login_tree is None:
            return
        item = self.login_tree.identify_row(event.y)
        if item:
            self.login_tree.selection_set(item)
            if self.login_context_menu:
                self.login_context_menu.tk_popup(event.x_root, event.y_root)

    def _auto_login_selected(self) -> None:
        entry = self._get_selected_login()
        if entry is None:
            return
        self._auto_login_entry(entry)

    def _oauth_login_selected_imap(self) -> None:
        entry = self._get_selected_login()
        if entry is None:
            return
        self.email_var.set(entry.email)
        self._start_oauth_login('IMAP', entry)

    def _oauth_login_selected_exchange(self) -> None:
        entry = self._get_selected_login()
        if entry is None:
            return
        self.email_var.set(entry.email)
        self._start_oauth_login('Exchange', entry)

    def _fetch_selected(self) -> None:
        for idx in self._selected_login_indices():
            if 0 <= idx < len(self.login_entries):
                entry = self.login_entries[idx]
                if entry.status == STATUS_LOGGED_IN and not entry.fetching:
                    self._start_fetch_for_entry(entry)

    # ─── Auto login logic ───

    def _auto_login_entry(self, entry: LoginEntry) -> None:
        # First, check if an OAuth token is already saved for this email
        for provider in ['IMAP', 'Exchange']:
            token_data = self.load_oauth_token(provider, entry.email)
            if token_data:
                self.append_log(f'{entry.email}: Found saved {provider} OAuth token. Using it...\n')
                try:
                    oauth_state = OAuthSessionState(
                        provider=provider,
                        email=entry.email,
                        access_token=token_data.get('access_token', ''),
                        authority='https://login.microsoftonline.com/common',
                        client_id=self.oauth_client_id,
                        token_result=token_data.get('token_result'),
                        refresh_token=token_data.get('refresh_token'),
                    )
                    entry.oauth_session = oauth_state
                    entry.status = STATUS_LOGGED_IN
                    entry.protocol = provider
                    entry.server = 'outlook.office365.com' if provider == 'IMAP' else 'outlook.office365.com'
                    entry.progress = 'Ready to fetch (OAuth)'
                    self._update_login_tree_row(entry)
                    self.append_log(f'{entry.email}: OAuth token loaded successfully. Ready to fetch.\n')
                    return
                except Exception as exc:
                    self.append_log(f'{entry.email}: Failed to load OAuth token: {exc}. Trying password login...\n')

        if not entry.password:
            entry.status = STATUS_LOGIN_FAILED
            entry.protocol = '-'
            entry.server = '-'
            entry.progress = 'No password - right-click for OAuth'
            self._update_login_tree_row(entry)
            self.append_log(f'{entry.email}: No password provided. Right-click to use OAuth.\n')
            return

        domain = entry.domain or (entry.email.split('@', 1)[-1] if '@' in entry.email else '')
        if not domain:
            entry.status = STATUS_ERROR
            entry.progress = 'Cannot determine domain'
            self._update_login_tree_row(entry)
            return

        entry.status = STATUS_DETECTING
        entry.progress = ''
        self._update_login_tree_row(entry)

        rules = core.IniLoader.load_server_rules(self.server_path)
        servers = core.ServerResolver.find_all_servers(domain, rules)

        imap_servers = [(s, p, e) for proto, s, p, e, _ in servers if proto == 'IMAP' and s]
        pop3_servers = [(s, p, e) for proto, s, p, e, _ in servers if proto == 'POP3' and s]

        if not imap_servers and not pop3_servers:
            imap_servers = [(f'imap.{domain}', 993, 'SSL')]
            pop3_servers = [(f'pop.{domain}', 995, 'SSL')]

        threading.Thread(
            target=self._try_auto_login,
            args=(entry, imap_servers, pop3_servers),
            daemon=True,
        ).start()

    def _try_auto_login(self, entry: LoginEntry, imap_servers: list, pop3_servers: list) -> None:
        self.after(0, lambda: self.append_log(f'{entry.email}: Auto-login starting (IMAP then POP3)...\n'))

        for server, port, encryption in imap_servers:
            entry.status = STATUS_TRYING_IMAP
            entry.protocol = 'IMAP'
            entry.server = server
            entry.progress = f'Trying {server}:{port}...'
            self.after(0, lambda e=entry: self._update_login_tree_row(e))
            self.after(0, lambda s=server, p=port: self.append_log(f'{entry.email}: Trying IMAP {s}:{p}...\n'))
            if self._test_imap_login(entry.email, entry.password, server, port, encryption):
                entry.status = STATUS_LOGGED_IN
                entry.progress = 'Ready to fetch'
                self.after(0, lambda e=entry: self._update_login_tree_row(e))
                self.after(0, lambda: self.append_log(f'{entry.email}: IMAP login succeeded on {server}\n'))
                return

        for server, port, encryption in pop3_servers:
            entry.status = STATUS_TRYING_POP3
            entry.protocol = 'POP3'
            entry.server = server
            entry.progress = f'Trying {server}:{port}...'
            self.after(0, lambda e=entry: self._update_login_tree_row(e))
            self.after(0, lambda s=server, p=port: self.append_log(f'{entry.email}: Trying POP3 {s}:{p}...\n'))
            if self._test_pop3_login(entry.email, entry.password, server, port, encryption):
                entry.status = STATUS_LOGGED_IN
                entry.progress = 'Ready to fetch'
                self.after(0, lambda e=entry: self._update_login_tree_row(e))
                self.after(0, lambda: self.append_log(f'{entry.email}: POP3 login succeeded on {server}\n'))
                return

        entry.status = STATUS_LOGIN_FAILED
        entry.protocol = '-'
        entry.progress = 'Right-click for OAuth'
        self.after(0, lambda e=entry: self._update_login_tree_row(e))
        self.after(0, lambda: self.append_log(f'{entry.email}: Auto-login failed. Right-click to try OAuth.\n'))

    def _test_imap_login(self, email: str, password: str, server: str, port: int | None, encryption: str | None) -> bool:
        import imaplib
        import ssl as ssl_mod
        timeout = int(self.timeout_var.get() or '30')
        use_ssl = encryption and encryption.upper() == 'SSL'
        conn = None
        try:
            if use_ssl:
                ctx = ssl_mod.create_default_context()
                conn = imaplib.IMAP4_SSL(server, port or 993, ssl_context=ctx, timeout=timeout)
            else:
                conn = imaplib.IMAP4(server, port or 143, timeout=timeout)
            conn.login(email, password)
            conn.logout()
            return True
        except Exception:
            if conn:
                try:
                    conn.logout()
                except Exception:
                    pass
            return False

    def _test_pop3_login(self, email: str, password: str, server: str, port: int | None, encryption: str | None) -> bool:
        import poplib
        import ssl as ssl_mod
        timeout = int(self.timeout_var.get() or '30')
        use_ssl = encryption and encryption.upper() == 'SSL'
        conn = None
        try:
            if use_ssl:
                ctx = ssl_mod.create_default_context()
                conn = poplib.POP3_SSL(server, port or 995, context=ctx, timeout=timeout)
            else:
                conn = poplib.POP3(server, port or 110, timeout=timeout)
            conn.user(email)
            conn.pass_(password)
            conn.quit()
            return True
        except Exception:
            if conn:
                try:
                    conn.quit()
                except Exception:
                    pass
            return False

    def _login_all_entries(self) -> None:
        for entry in self.login_entries:
            if entry.status in (STATUS_PENDING, STATUS_LOGIN_FAILED, STATUS_ERROR):
                self._auto_login_entry(entry)
                time.sleep(0.3)

    # ─── Fetch for entries ───

    def _fetch_all_logged_in(self) -> None:
        for entry in self.login_entries:
            if entry.status == STATUS_LOGGED_IN and not entry.fetching:
                self._start_fetch_for_entry(entry)
                time.sleep(0.2)

    def _start_fetch_for_entry(self, entry: LoginEntry) -> None:
        if entry.fetching:
            return
        if entry.status != STATUS_LOGGED_IN:
            messagebox.showwarning('Fetch', f'{entry.email} is not logged in.')
            return

        settings = self._build_fetch_settings()
        config = self._build_fetch_config()
        output_dir = Path(self.output_var.get().strip() or self.script_dir / 'output') / entry.email.replace('@', '_at_')
        use_ssl = True
        server = entry.server
        port = None
        protocol = entry.protocol

        rules = core.IniLoader.load_server_rules(self.server_path)
        if server and '@' not in server and '#' not in server:
            try:
                port_str = server.split(':')[-1] if ':' in server else None
                if port_str and port_str.isdigit():
                    port = int(port_str)
                    server = server.rsplit(':', 1)[0]
            except Exception:
                pass

        if not port:
            if protocol == 'IMAP':
                port = 993
            elif protocol == 'POP3':
                port = 995

        oauth_token = None
        oauth_token_data = None
        if entry.oauth_session:
            if protocol == 'IMAP':
                oauth_token = entry.oauth_session.access_token
                server = 'outlook.office365.com'
                port = 993
                use_ssl = True
            elif protocol == 'Exchange':
                oauth_token_data = entry.oauth_session.token_result

        def progress_cb(email_addr, status_msg, current, total, folder):
            if total > 0:
                prog = f'{current}/{total}'
                if folder:
                    prog = f'{folder}: {prog}'
            else:
                prog = status_msg
            entry.progress = prog
            entry.status = STATUS_FETCHING
            self.after(0, lambda: self._update_login_tree_row(entry))

        fetcher: core.BaseFetcher
        if protocol == 'IMAP':
            fetcher = core.IMAPFetcher(
                entry.email, entry.password, server, port, use_ssl, settings, config, output_dir,
                oauth_access_token=oauth_token, progress_callback=progress_cb
            )
        elif protocol == 'POP3':
            fetcher = core.POPFetcher(
                entry.email, entry.password, server, port, use_ssl, settings, config, output_dir,
                progress_callback=progress_cb
            )
        else:
            if not core.EXCHANGE_AVAILABLE:
                self.append_log(f'{entry.email}: Exchange not available (install exchangelib)\n')
                return
            fetcher = core.ExchangeFetcher(
                entry.email, entry.password, server, port, use_ssl, settings, config, output_dir,
                oauth_token_data=oauth_token_data, oauth_client_id=self.oauth_client_id,
                progress_callback=progress_cb
            )

        entry.fetcher = fetcher
        entry.fetching = True
        entry.status = STATUS_FETCHING
        entry.progress = 'Starting...'
        self._update_login_tree_row(entry)

        threading.Thread(
            target=self._run_entry_fetch,
            args=(entry, fetcher),
            daemon=True,
        ).start()

    def _run_entry_fetch(self, entry: LoginEntry, fetcher: core.BaseFetcher) -> None:
        old_stdout = sys.stdout
        try:
            sys.stdout = TextRedirector(self.append_log)
            self.append_log(f'{entry.email}: Starting email fetch...\n')
            fetcher.fetch()
            self.after(0, lambda: self.append_log(f'{entry.email}: Fetch completed.\n'))
            entry.status = STATUS_COMPLETED
            entry.progress = f'{len(fetcher.results)} emails'
        except InterruptedError:
            entry.status = STATUS_STOPPED
            entry.progress = 'Aborted'
            self.after(0, lambda: self.append_log(f'{entry.email}: Fetch aborted.\n'))
        except Exception as exc:
            entry.status = STATUS_ERROR
            entry.progress = str(exc)[:80]
            self.after(0, lambda exc=exc: self.append_log(f'{entry.email}: Fetch error: {exc}\n'))
        finally:
            sys.stdout = old_stdout
            entry.fetching = False
            entry.fetcher = None
            self.after(0, lambda: self._update_login_tree_row(entry))

    # ─── OAuth ───

    def office_oauth_imap(self) -> None:
        self._start_oauth_login('IMAP')

    def office_oauth_exchange(self) -> None:
        self._start_oauth_login('Exchange')

    def _check_webview_available(self) -> bool:
        try:
            import webview
            try:
                import webview.platforms
                return True
            except (ImportError, Exception):
                return False
        except ImportError:
            return False

    def _start_oauth_login(self, provider: str, target_entry: LoginEntry | None = None) -> None:
        if self._oauth_in_progress:
            messagebox.showinfo('OAuth In Progress', 'An OAuth login is already running.')
            return
        try:
            import msal
        except ImportError:
            messagebox.showerror('OAuth Missing', 'The msal library is required for OAuth login. Install it with: pip install msal')
            return

        email = self.email_var.get().strip()
        if target_entry:
            email = target_entry.email

        cli_id = self.oauth_client_id or 'e9a7fea1-1cc0-4cd9-a31b-9137ca5deedd'
        authority = self.oauth_authority.strip() if self.oauth_authority else 'https://login.microsoftonline.com/common'
        scope = ['https://outlook.office.com/IMAP.AccessAsUser.All'] if provider == 'IMAP' else ['https://outlook.office365.com/EWS.AccessAsUser.All']
        self._oauth_in_progress = True
        self._auto_login_for_entry = target_entry

        if target_entry:
            target_entry.status = STATUS_OAUTH_PENDING
            target_entry.protocol = provider
            target_entry.progress = 'Waiting for browser...'
            self._update_login_tree_row(target_entry)

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
        # Use protocol handler URI configured in Azure
        return getattr(settings, 'oauth_redirect_uri', 'com.emclient.MailClient://oauth')

    def _run_oauth_flow(self, provider: str, cli_id: str, authority: str, email: str, scope: list[str], window_x: int, window_y: int, window_width: int, window_height: int) -> None:
        try:
            self._launch_oauth_helper(provider, cli_id, authority, email, scope, window_x, window_y, window_width, window_height)
        except Exception as exc:
            self.after(0, lambda: messagebox.showerror('OAuth Error', f'Unable to start OAuth {provider}: {exc}'))
            self.after(0, lambda: self.append_log(f'OAuth {provider} startup failed: {exc}\n'))
            self.after(0, self._clear_oauth_progress)
            self.after(0, lambda: self._set_main_window_enabled(True))

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
            '--provider', provider,
            '--client-id', cli_id,
            '--authority', authority,
            '--email', email,
            '--scope', '|'.join(scope),
            '--result-file', str(result_path),
            '--redirect-uri', redirect_uri,
            '--window-x', str(window_x),
            '--window-y', str(window_y),
            '--window-width', str(window_width),
            '--window-height', str(window_height),
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
                if self._auto_login_for_entry:
                    self._auto_login_for_entry.status = STATUS_LOGIN_FAILED
                    self._auto_login_for_entry.progress = f'OAuth failed: {error_text[:60]}'
                    self._update_login_tree_row(self._auto_login_for_entry)
                    self._auto_login_for_entry = None
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
            email_addr = str(payload.get('email', self.email_var.get().strip()))

            self._complete_oauth_login(
                provider=provider,
                email=email_addr,
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
        oauth_state = OAuthSessionState(
            provider=provider,
            email=email,
            access_token=access_token,
            authority=authority,
            client_id=cli_id,
            token_result=token_result,
            refresh_token=refresh_token or None,
            token_expiry=expiry_time,
        )
        self.oauth_session = oauth_state

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

        target_entry = self._auto_login_for_entry
        if target_entry:
            target_entry.oauth_session = oauth_state
            target_entry.status = STATUS_LOGGED_IN
            target_entry.protocol = provider
            if provider == 'IMAP':
                target_entry.server = 'outlook.office365.com'
            target_entry.progress = 'OAuth OK - ready to fetch'
            self._update_login_tree_row(target_entry)
            self.append_log(f'{email}: OAuth login succeeded. Starting fetch automatically...\n')
            self._auto_login_for_entry = None
            self._clear_oauth_progress()
            self.after(500, lambda: self._start_fetch_for_entry(target_entry))
        else:
            self.append_log(f'Starting automatic email fetch after OAuth login...\n')
            self._clear_oauth_progress()
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

    # ─── Fetch (top section) ───

    def _build_fetch_settings(self) -> core.FetchSettings:
        settings = core.FetchSettings(
            thread_count=int(self.thread_count_var.get() or '5'),
            save_content=self.save_content_var.get(),
            save_attachments=self.save_attachments_var.get(),
            save_correct_account=self.correct_account_var.get(),
            save_separated_file=self.separated_file_var.get(),
            save_email_result=self.save_results_var.get(),
            extract_subject=self.extract_subject_var.get(),
            extract_date=self.extract_date_var.get(),
            extract_attachments_list=self.extract_attachments_list_var.get(),
            extract_summary=self.extract_summary_var.get(),
            keyword=self.keyword_var.get().strip(),
            date_from=core.parse_date_option(self.date_from_var.get().strip()) if self.date_from_var.get().strip() else None,
            date_to=core.parse_date_option(self.date_to_var.get().strip()) if self.date_to_var.get().strip() else None,
            search_subject=self.search_subject_var.get(),
            search_body=self.search_body_var.get(),
            save_log=self.save_logs_var.get(),
            connection_timeout=int(self.timeout_var.get() or '30'),
            batch_size=int(self.batch_size_var.get() or '50'),
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
        threading.Thread(target=self._run_fetch, args=(fetcher,), daemon=True).start()

    def stop_fetch(self) -> None:
        if self.fetcher:
            self.fetcher.request_abort()
        for entry in self.login_entries:
            if entry.fetcher:
                entry.fetcher.request_abort()
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
            email_addr, password, server, port, ssl_enabled, settings, config, output_dir,
            oauth_access_token=None, oauth_token_data=oauth_token_data, oauth_client_id=self.oauth_client_id,
        )

    def _run_fetch(self, fetcher: core.BaseFetcher) -> None:
        old_stdout = sys.stdout
        try:
            sys.stdout = TextRedirector(self.append_log)
            self.append_log('Starting fetch operation...\n')
            fetcher.fetch()
            self.append_log('Fetch operation completed.\n')
        except InterruptedError:
            self.append_log('Fetch aborted by user.\n')
        except Exception as exc:
            message = str(exc)
            self.append_log(f'Error: {message}\n')
            self.after(0, lambda: messagebox.showerror('Fetch Error', message))
        finally:
            sys.stdout = old_stdout
            self.after(0, self._finish_fetch)

    def _finish_fetch(self) -> None:
        self.start_button.configure(state='normal')
        self.stop_button.configure(state='disabled')
        self.status_label.configure(text='Ready')

    # ─── Logging ───

    def append_log(self, message: str) -> None:
        if threading.get_ident() != self.main_thread_id:
            self.log_queue.put(message)
            return
        self._append_log_now(message)

    def _append_log_now(self, message: str) -> None:
        if self.log_text is None:
            print(message, end='')
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
        try:
            while True:
                update = self.status_updates_queue.get_nowait()
        except queue.Empty:
            pass
        self.after(75, self._process_status_updates)

    def on_close(self) -> None:
        if self.fetcher and not self.fetcher.abort_requested:
            if not messagebox.askokcancel('Quit', 'A fetch is in progress. Abort and quit?'):
                return
            self.fetcher.request_abort()
        for entry in self.login_entries:
            if entry.fetcher and not entry.fetcher.abort_requested:
                entry.fetcher.request_abort()
        self._cleanup_oauth_helper()
        self.destroy()


def _write_oauth_result(result_path: Path, payload: dict[str, object]) -> None:
    try:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        with result_path.open('w', encoding='utf-8') as fp:
            json.dump(payload, fp)
            fp.flush()
            try:
                os.fsync(fp.fileno())
            except Exception:
                pass
    except Exception as e:
        print(f'Error writing OAuth result: {type(e).__name__}: {e}')
        import traceback
        traceback.print_exc()


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
                import webview.platforms
                webview_available = True
            except (ImportError, Exception):
                webview_available = False
        except ImportError:
            pass

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
                'email': args.email or '',
            }, fp)

        opened = webbrowser.open(auth_url)

        print(f'OAuth: Sign-in browser opened. Waiting for callback...')
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
        import traceback
        print(f'OAuth: Browser flow exception: {type(exc).__name__}: {exc}')
        print(traceback.format_exc())
        _write_oauth_result(result_path, {'status': 'error', 'error': f'Browser OAuth flow failed: {exc}'})


def _handle_oauth_callback(callback_uri: str) -> None:
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

        callback_params = ProtocolHandler.parse_callback_uri(callback_uri)
        log_msg(f'Parsed callback params: code={callback_params.get("code", "")[:20]}..., state={callback_params.get("state", "")}')

        temp_dir = Path(tempfile.gettempdir())
        pkce_files = list(temp_dir.glob('pkce_oauth_result_*.json'))
        log_msg(f'Found {len(pkce_files)} PKCE files')

        # Find PKCE file that matches the state parameter
        callback_state = callback_params.get('state', '')
        pkce_file = None
        pkce_data = None
        
        for pf in reversed(pkce_files):  # Start from most recent
            try:
                with pf.open('r', encoding='utf-8') as fp:
                    pd = json.load(fp)
                    if pd.get('flow', {}).get('state') == callback_state:
                        pkce_file = pf
                        pkce_data = pd
                        log_msg(f'Found matching PKCE file: {pf.name} with state: {callback_state}')
                        break
            except (json.JSONDecodeError, Exception):
                pass
        
        if not pkce_file or not pkce_data:
            # Fallback to most recent file if state matching fails
            if pkce_files:
                pkce_file = pkce_files[-1]
                try:
                    with pkce_file.open('r', encoding='utf-8') as fp:
                        pkce_data = json.load(fp)
                    log_msg(f'Using most recent PKCE file as fallback: {pkce_file.name}')
                except (json.JSONDecodeError, Exception) as e:
                    log_msg(f'Failed to read PKCE data: {e}')
                    return
            else:
                log_msg('No pending OAuth request found.')
                return

        result_file = Path(pkce_data['result_file'])

        if 'error' in callback_params:
            error_desc = callback_params.get('error_description', callback_params.get('error', 'Unknown error'))
            log_msg(f'Error in callback: {error_desc}')
            _write_oauth_result(result_file, {'status': 'error', 'error': str(error_desc)})
            return

        if 'code' not in callback_params:
            log_msg('No authorization code in callback.')
            _write_oauth_result(result_file, {'status': 'error', 'error': 'No authorization code in callback.'})
            return

        log_msg('Authorization code found. Exchanging for token...')

        try:
            local_app = msal.PublicClientApplication(
                client_id=pkce_data['client_id'],
                authority=pkce_data['authority']
            )
            log_msg(f'Created MSAL app for {pkce_data["client_id"]}')

            flow = pkce_data['flow']
            log_msg('Calling acquire_token_by_auth_code_flow...')
            token_result = local_app.acquire_token_by_auth_code_flow(flow, callback_params)
            log_msg(f'Token exchange result keys: {list(token_result.keys())}')

            if 'access_token' in token_result:
                email = ''
                
                # Try to extract email from token claims
                if 'id_token_claims' in token_result:
                    email = token_result['id_token_claims'].get('email', '')
                    log_msg(f'Email from id_token_claims: {email}')
                
                # Fallback: try account.username
                if not email and 'account' in token_result:
                    if isinstance(token_result['account'], dict):
                        email = token_result['account'].get('username', '')
                    log_msg(f'Email from account.username: {email}')
                
                # Fallback: use email from PKCE data (the email that was requested)
                if not email:
                    email = pkce_data.get('email', '')
                    log_msg(f'Email from PKCE data: {email}')

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
        # Try to write error to any available result file
        try:
            temp_dir = Path(tempfile.gettempdir())
            result_files = list(temp_dir.glob('oauth_result_*.json'))
            if result_files:
                result_file = result_files[-1]
                _write_oauth_result(result_file, {
                    'status': 'error',
                    'error': f'Callback handler failed: {exc}'
                })
        except Exception:
            pass


if __name__ == '__main__':
    if '--oauth-callback' in sys.argv:
        callback_idx = sys.argv.index('--oauth-callback')
        if callback_idx + 1 < len(sys.argv):
            callback_uri = sys.argv[callback_idx + 1]
            _handle_oauth_callback(callback_uri)
        sys.exit(0)
    
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w')

    if '--oauth-helper' in sys.argv:
        _run_oauth_helper_process()
    else:
        app = MailListFetcherGUI()
        app.mainloop()
