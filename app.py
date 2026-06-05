import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

class ProtocolHandler:
    PROTOCOL_SCHEME = 'com.emclient.MailClient'
    REGISTRY_PATH = f'HKEY_CLASSES_ROOT\\{PROTOCOL_SCHEME}'

    @staticmethod
    def register_protocol_handler(app_exe_path: str | None = None) -> bool:
        if os.name != 'nt':
            return False
        try:
            import winreg
        except ImportError:
            return False

        try:
            python_exe = sys.executable
            pythonw_exe = python_exe.replace('python.exe', 'pythonw.exe')
            runner_exe = pythonw_exe if Path(pythonw_exe).exists() else python_exe
            script_path = str(Path(app_exe_path or __file__).resolve())
            with winreg.CreateKey(winreg.HKEY_CLASSES_ROOT, ProtocolHandler.PROTOCOL_SCHEME) as key:
                winreg.SetValueEx(key, '', 0, winreg.REG_SZ, f'URL:{ProtocolHandler.PROTOCOL_SCHEME} Protocol')
                winreg.SetValueEx(key, 'URL Protocol', 0, winreg.REG_SZ, '')
            cmd_path = f'{ProtocolHandler.PROTOCOL_SCHEME}\\shell\\open\\command'
            with winreg.CreateKey(winreg.HKEY_CLASSES_ROOT, cmd_path) as key:
                cmd = f'"{runner_exe}" "{script_path}" --oauth-callback "%1"'
                winreg.SetValueEx(key, '', 0, winreg.REG_SZ, cmd)
            return True
        except Exception:
            return False

    @staticmethod
    def parse_callback_uri(uri: str) -> dict[str, str]:
        parsed = urllib.parse.urlparse(uri)
        return dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))

from PySide6.QtCore import Qt, QThread, Signal, QModelIndex, QTimer
from PySide6.QtGui import QKeySequence, QShortcut, QStandardItemModel, QStandardItem
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QDialog,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QHBoxLayout,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QStackedWidget,
    QMenu,
    QScrollArea,
    QSizePolicy,
)

import mail_list_fetcher as core


class FetchWorker(QThread):
    log_message = Signal(str)
    status_update = Signal(str)
    dashboard_update = Signal(str, str, str, str, str)  # email, protocol, server, status, progress
    finished_signal = Signal(bool)

    def __init__(self, fetcher: core.BaseFetcher):
        super().__init__()
        self.fetcher = fetcher

    def run(self) -> None:
        self.fetcher.progress_callback = self._progress_callback
        debug_path = Path(tempfile.gettempdir()) / 'fetch_worker_debug.log'
        try:
            with debug_path.open('a', encoding='utf-8') as dbg:
                dbg.write(f'FetchWorker.run START {datetime.now().isoformat()}\n')
        except Exception:
            pass
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = self
        sys.stderr = self
        success = True
        try:
            self.fetcher.fetch()
        except InterruptedError as exc:
            try:
                self.log_message.emit(f'Fetch interrupted: {exc}\n')
            except Exception:
                print(f'Fetch interrupted: {exc}\n', file=old_stderr)
            success = False
        except Exception as exc:
            try:
                self.log_message.emit(f'ERROR: {exc}\n')
                self.log_message.emit(traceback.format_exc())
            except Exception:
                print(f'ERROR: {exc}\n', file=old_stderr)
                print(traceback.format_exc(), file=old_stderr)
            success = False
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            try:
                self.finished_signal.emit(success)
            except Exception as exc:
                print(f'ERROR emitting finished_signal: {exc}', file=sys.stderr)
            try:
                with debug_path.open('a', encoding='utf-8') as dbg:
                    dbg.write(f'FetchWorker.run END success={success} {datetime.now().isoformat()}\n')
            except Exception:
                pass

    def write(self, text: str) -> None:
        if text:
            try:
                self.log_message.emit(str(text))
            except Exception:
                pass

    def flush(self) -> None:
        pass

    def _progress_callback(self, email: str, status: str, current: int, total: int, folder: str) -> None:
        try:
            text = status
            progress_text = ''
            if total:
                text += f' [{current}/{total}]'
                progress_text = f'[{current}/{total}]'
            if folder:
                text += f' folder={folder}'
            
            # Emit progress update
            try:
                self.status_update.emit(text)
            except Exception:
                pass
            
            # Emit dashboard update with email and status info
            protocol = 'IMAP' if isinstance(self.fetcher, core.IMAPFetcher) else 'POP3' if isinstance(self.fetcher, core.POPFetcher) else 'Exchange'
            server = getattr(self.fetcher, 'server', '')
            status_text = f'Fetching {folder}' if folder else status
            try:
                self.dashboard_update.emit(email, protocol, server, status_text, progress_text)
            except Exception:
                pass
        except Exception:
            pass

    def request_abort(self) -> None:
        self.fetcher.request_abort()


class LoginTestWorker(QThread):
    row_update = Signal(str, str, str, str, str)
    finished = Signal()

    def __init__(self, login_entries: list[tuple[str, str, str]], config: core.FetchConfig, rules: list[core.ServerRule], settings: core.FetchSettings, root_dir: Path = None):
        super().__init__()
        self.login_entries = login_entries
        self.config = config
        self.rules = rules
        self.settings = settings
        self.root_dir = root_dir or Path(__file__).resolve().parent.parent
        self._abort_requested = False

    def request_abort(self) -> None:
        self._abort_requested = True

    def run(self) -> None:
        for email, password, domain in self.login_entries:
            if self._abort_requested:
                self.finished.emit()
                return
            
            # Check if a saved OAuth token exists for this email
            oauth_token = self._try_load_oauth_token('IMAP', email)
            if oauth_token:
                self.row_update.emit(email, 'IMAP', 'oauth', 'Login succeeded (OAuth)', 'outlook.office365.com')
                continue
            
            if not password:
                self.row_update.emit(email, '', '', 'Missing password - use OAuth', '')
                continue

            server_hint = domain.strip() or email.split('@')[-1]
            protocol = 'IMAP'
            status = 'Testing IMAP...'
            progress = ''
            self.row_update.emit(email, protocol, '', status, '')

            imap_success, imap_server, imap_port, imap_reason = self._attempt_login(email, password, server_hint, 'IMAP')
            if self._abort_requested:
                self.finished.emit()
                return
            if imap_success:
                status = 'Login succeeded (IMAP)'
                progress = f'{imap_server}:{imap_port}' if imap_server else ''
                self.row_update.emit(email, 'IMAP', imap_server or '', status, progress)
                continue

            protocol = 'POP3'
            status = 'Testing POP3...'
            self.row_update.emit(email, protocol, '', status, '')

            pop_success, pop_server, pop_port, pop_reason = self._attempt_login(email, password, server_hint, 'POP3')
            if self._abort_requested:
                self.finished.emit()
                return
            if pop_success:
                status = 'Login succeeded (POP3)'
                progress = f'{pop_server}:{pop_port}' if pop_server else ''
                self.row_update.emit(email, 'POP3', pop_server or '', status, progress)
                continue

            status = 'Login failed'
            reason = pop_reason or imap_reason or 'Unknown error'
            self.row_update.emit(email, 'Failed', '', f'{status}: {reason}', '')

        self.finished.emit()
    
    def _try_load_oauth_token(self, provider: str, email: str) -> Optional[dict]:
        """Load and validate saved OAuth token for email."""
        try:
            token_dir = self._get_secure_token_dir()
            token_file = token_dir / f'{provider}_{email}.json'
            if not token_file.exists():
                return None
            with token_file.open('r', encoding='utf-8') as fp:
                token_data = json.load(fp)
            # Check if token is still valid
            if self._is_oauth_token_valid(token_data):
                return token_data
            return None
        except Exception:
            return None
    
    def _get_secure_token_dir(self) -> Path:
        token_dir = self.root_dir / '.oauth_tokens'
        token_dir.mkdir(parents=True, exist_ok=True)
        return token_dir
    
    def _is_oauth_token_valid(self, token_data: dict) -> bool:
        if not token_data or 'access_token' not in token_data:
            return False
        now = time.time()
        expires_at = token_data.get('expires_at')
        if expires_at is not None:
            try:
                if float(expires_at) <= now:
                    return False
            except Exception:
                return True
        return True

    def _attempt_login(self, email: str, password: str, domain: str, provider: str) -> tuple[bool, str, Optional[int], str]:
        server, port, encryption, _ = core.ServerResolver.choose_server(domain, provider, self.rules)
        if not server and domain and '.' in domain:
            server = domain
        if not server:
            return False, '', None, 'No server found'

        use_ssl = encryption and encryption.upper() == 'SSL'
        port = port or (993 if provider == 'IMAP' else 995 if use_ssl else 110)

        try:
            if provider == 'IMAP':
                import imaplib
                ctx = ssl.create_default_context()
                if use_ssl:
                    conn = imaplib.IMAP4_SSL(server, port, ssl_context=ctx, timeout=getattr(self.settings, 'connection_timeout', 30))
                else:
                    conn = imaplib.IMAP4(server, port, timeout=getattr(self.settings, 'connection_timeout', 30))
                    try:
                        conn.starttls(ctx)
                    except imaplib.IMAP4.error:
                        pass
                conn.login(email, password)
                conn.logout()
            else:
                import poplib
                ctx = ssl.create_default_context()
                if use_ssl:
                    conn = poplib.POP3_SSL(server, port, context=ctx, timeout=getattr(self.settings, 'connection_timeout', 30))
                else:
                    conn = poplib.POP3(server, port, timeout=getattr(self.settings, 'connection_timeout', 30))
                    try:
                        conn.stls(context=ctx)
                    except poplib.error_proto:
                        pass
                conn.user(email)
                conn.pass_(password)
                conn.quit()
            return True, server, port, ''
        except Exception as exc:
            return False, server, port, str(exc)


class MailFetcherWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle('FetchME Qt')
        self.setMinimumSize(700, 400)

        self.root_dir = Path(__file__).resolve().parent.parent
        self.config_path = self.root_dir / 'Config.ini'
        self.server_path = self.root_dir / 'Server_List.ini'
        self.settings_path = self.root_dir / 'Setting.ini'
        self.output_dir = self.root_dir / 'output'

        self.default_settings = core.IniLoader.load_settings(self.settings_path)
        self.default_config = core.IniLoader.load_config(self.config_path)
        self.default_rules = core.IniLoader.load_server_rules(self.server_path)

        self.login_file_path = self.root_dir / 'logins.txt'
        self.login_entries: list[tuple[str, str, str]] = []
        self.login_test_worker: Optional[LoginTestWorker] = None
        self.oauth_client_id = self.default_settings.oauth_client_id
        self.oauth_authority = self.default_settings.oauth_authority
        self.oauth_redirect_uri = self.default_settings.oauth_redirect_uri
        self.oauth_access_token: Optional[str] = None
        self.oauth_token_data: Optional[dict] = None
        self.oauth_provider: Optional[str] = None
        self._oauth_in_progress = False
        self._oauth_helper_proc: Optional[subprocess.Popen] = None
        self.worker: Optional[FetchWorker] = None
        self.current_fetcher: Optional[core.BaseFetcher] = None

        self._build_ui()
        self._load_defaults()

    def save_settings(self, settings: core.FetchSettings) -> bool:
        success = core.IniLoader.save_settings(self.settings_path, settings)
        if success:
            self.append_log('Settings saved to Setting.ini\n')
        else:
            self.append_log('Failed to save settings to Setting.ini\n')
        return success

    def _build_ui(self) -> None:
        central = QWidget()
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self.stack = QStackedWidget()
        page_main = QWidget()
        page_layout = QVBoxLayout(page_main)
        page_layout.setSpacing(12)
        page_layout.setContentsMargins(12, 12, 12, 12)
        page_layout.addWidget(self._build_connection_group())
        page_layout.addLayout(self._build_control_row())
        page_layout.addWidget(self._build_log_group(), stretch=1)

        self.stack.addWidget(page_main)
        self.settings_widget = SettingsDialog(self, self.default_settings)
        self.stack.addWidget(self.settings_widget)

        main_layout.addWidget(self.stack)
        self.setCentralWidget(central)

    def _build_connection_group(self) -> QGroupBox:
        group = QGroupBox('Connection & OAuth')
        layout = QGridLayout(group)
        layout.setSpacing(8)

        row = 0
        layout.addWidget(QLabel('Provider:'), row, 0)
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(['IMAP', 'POP3', 'Exchange'])
        layout.addWidget(self.provider_combo, row, 1)
        layout.addWidget(QLabel('Email:'), row, 2)
        self.email_edit = QLineEdit()
        layout.addWidget(self.email_edit, row, 3, 1, 2)

        row += 1
        layout.addWidget(QLabel('Password:'), row, 0)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        layout.addWidget(self.password_edit, row, 1)
        layout.addWidget(QLabel('Server:'), row, 2)
        self.server_edit = QLineEdit()
        layout.addWidget(self.server_edit, row, 3)
        layout.addWidget(QLabel('Port:'), row, 4)
        self.port_edit = QLineEdit()
        self.port_edit.setMaximumWidth(80)
        layout.addWidget(self.port_edit, row, 5)

        row += 1
        self.ssl_checkbox = QCheckBox('Use SSL / TLS')
        layout.addWidget(self.ssl_checkbox, row, 0, 1, 2)
        self.manual_login_button = QPushButton('Login')
        self.manual_login_button.clicked.connect(self.on_test_login)
        layout.addWidget(self.manual_login_button, row, 2)
        layout.addWidget(QLabel('Output:'), row, 3)
        self.output_edit = QLineEdit(str(self.output_dir))
        layout.addWidget(self.output_edit, row, 4, 1, 1)
        self.browse_button = QPushButton('Browse')
        self.browse_button.clicked.connect(self.on_browse_output)
        layout.addWidget(self.browse_button, row, 5)

        row += 1
        layout.addWidget(QLabel('Login file:'), row, 0)
        self.login_file_label = QLabel(str(self.login_file_path))
        self.login_file_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.login_file_label, row, 1, 1, 2)
        self.login_import_button = QPushButton('Import')
        self.login_import_button.clicked.connect(self.on_import_login_file)
        layout.addWidget(self.login_import_button, row, 3)
        self.oauth_imap_button = QPushButton('OAuth IMAP')
        self.oauth_imap_button.clicked.connect(lambda: self.on_oauth_login('IMAP'))
        layout.addWidget(self.oauth_imap_button, row, 4, 1, 2)

        row += 1
        self.oauth_exchange_button = QPushButton('OAuth Exchange')
        self.oauth_exchange_button.clicked.connect(lambda: self.on_oauth_login('Exchange'))
        layout.addWidget(self.oauth_exchange_button, row, 3)

        row += 1
        self.oauth_status_label = QLabel('No OAuth token')
        self.oauth_status_label.setWordWrap(True)
        layout.addWidget(self.oauth_status_label, row, 0, 1, 6)

        group.setLayout(layout)
        return group

    def _build_control_row(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        layout.setSpacing(12)

        self.start_button = QPushButton('Start Fetch')
        self.start_button.clicked.connect(self.on_start_button)
        layout.addWidget(self.start_button)

        self.open_output_button = QPushButton('Open Output')
        self.open_output_button.clicked.connect(self.on_open_output)
        layout.addWidget(self.open_output_button)

        self.settings_button = QPushButton('Settings')
        self.settings_button.clicked.connect(self.on_open_settings)
        layout.addWidget(self.settings_button)

        self.status_label = QLabel('Ready')
        self.status_label.setStyleSheet('color: #333333;')
        layout.addWidget(self.status_label, stretch=1)
        return layout

    def _build_log_group(self) -> QGroupBox:
        group = QGroupBox('Activity Dashboard')
        layout = QVBoxLayout(group)

        self.login_table = QTableWidget()
        self.login_table.setColumnCount(5)
        self.login_table.setHorizontalHeaderLabels(['Email', 'Protocol', 'Server', 'Status', 'Progress'])
        self.login_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.login_table.customContextMenuRequested.connect(self.on_login_table_context_menu)
        self.login_table.itemDoubleClicked.connect(self.on_login_table_double_click)
        layout.addWidget(self.login_table)

        group.setLayout(layout)
        return group

    def _load_defaults(self) -> None:
        settings = self.default_settings
        self.provider_combo.setCurrentText('IMAP')
        self.email_edit.clear()
        self.password_edit.clear()
        self.server_edit.clear()
        self.port_edit.setText('993')
        self.ssl_checkbox.setChecked(True)
        self.output_edit.setText(str(self.output_dir))
        self._update_oauth_status('No OAuth token')

    def on_test_login(self) -> None:
        if self.login_test_worker and self.login_test_worker.isRunning():
            QMessageBox.information(self, 'Login In Progress', 'A login check is already running.')
            return

        email = self.email_edit.text().strip()
        password = self.password_edit.text()
        provider = self.provider_combo.currentText()
        if not email:
            QMessageBox.warning(self, 'Email Required', 'Please enter an email address before testing login.')
            return
        if not password:
            QMessageBox.warning(self, 'Password Required', 'Please enter the password before testing login.')
            return
        if provider == 'Exchange':
            QMessageBox.information(self, 'Exchange Login', 'Use OAuth Exchange for manual Exchange login. Select IMAP or POP3 for direct login checks.')
            return

        server = self.server_edit.text().strip() or None
        port = self._parse_int(self.port_edit.text().strip()) if self.port_edit.text().strip() else None
        use_ssl = self.ssl_checkbox.isChecked()
        server, port, use_ssl = self._autodetect_server(provider, email, server, port, use_ssl)
        if server:
            self.server_edit.setText(server)
        if port is not None:
            self.port_edit.setText(str(port))
        self.ssl_checkbox.setChecked(use_ssl)

        self._update_login_row(email, provider, server or '', 'Testing login...', '')
        self.manual_login_button.setEnabled(False)

        domain = email.split('@')[-1] if '@' in email else ''
        config = core.IniLoader.load_config(self.config_path)
        rules = core.IniLoader.load_server_rules(self.server_path)
        self.login_test_worker = LoginTestWorker([(email, password, domain)], config, rules, self.default_settings, self.root_dir)
        self.login_test_worker.row_update.connect(self._on_login_test_row_update)
        self.login_test_worker.finished.connect(self._on_manual_login_finished)
        self.login_test_worker.start()

    def _on_manual_login_finished(self) -> None:
        self.manual_login_button.setEnabled(True)
        self.append_log('Manual login test complete.\n')

    def _autodetect_server(self, provider: str, email_address: str, server: Optional[str], port: Optional[int], use_ssl: bool) -> tuple[Optional[str], Optional[int], bool]:
        if provider not in ('IMAP', 'POP3'):
            return server, port, use_ssl

        if not server and '@' in email_address:
            domain = email_address.split('@', 1)[1]
            rules = core.IniLoader.load_server_rules(self.server_path)
            autodiscovered_server, autodiscovered_port, encryption, _ = core.ServerResolver.choose_server(domain, provider, rules)
            if autodiscovered_server:
                server = autodiscovered_server
            if port is None:
                port = autodiscovered_port
            if encryption and encryption.upper() == 'SSL':
                use_ssl = True
        return server, port, use_ssl

    def on_browse_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, 'Select output directory', str(self.output_dir))
        if folder:
            self.output_edit.setText(folder)

    def on_open_output(self) -> None:
        output_path = Path(self.output_edit.text().strip() or self.output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        if os.name == 'nt':
            os.startfile(str(output_path))
        elif sys.platform == 'darwin':
            os.system(f'open "{output_path}"')
        else:
            os.system(f'xdg-open "{output_path}"')



    def on_open_settings(self) -> None:
        self.stack.setCurrentWidget(self.settings_widget)

    def show_main_page(self) -> None:
        self.stack.setCurrentIndex(0)

    def on_import_login_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            'Select login file',
            str(self.root_dir),
            'Text Files (*.txt);;All Files (*)'
        )
        if not file_path:
            return
        self.login_file_path = Path(file_path)
        self.login_file_label.setText(str(self.login_file_path))
        self._load_login_file()

    def _load_login_file(self) -> None:
        self.login_entries = []
        self.login_table.setRowCount(0)
        try:
            if not self.login_file_path.exists():
                self.append_log(f'Login file not found: {self.login_file_path}\n')
                return
            with self.login_file_path.open('r', encoding='utf-8', errors='ignore') as fp:
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
                        self.login_entries.append((email, password, domain))
                        self._add_login_row(email, '', '', 'Pending', '')
            self.append_log(f'Loaded {len(self.login_entries)} login entries from {self.login_file_path}\n')
            if self.login_entries:
                email, password, _ = self.login_entries[0]
                self.email_edit.setText(email)
                self.password_edit.setText(password)
                self.append_log(f'Using first login entry: {email}\n')
                self._start_auto_login_list()
        except Exception as exc:
            self.append_log(f'Error loading login file: {exc}\n')

    def _add_login_row(self, email: str, protocol: str, server: str, status: str, progress: str) -> None:
        row = self.login_table.rowCount()
        self.login_table.insertRow(row)
        self.login_table.setItem(row, 0, QTableWidgetItem(email))
        self.login_table.setItem(row, 1, QTableWidgetItem(protocol))
        self.login_table.setItem(row, 2, QTableWidgetItem(server))
        self.login_table.setItem(row, 3, QTableWidgetItem(status))
        self.login_table.setItem(row, 4, QTableWidgetItem(progress))

    def _start_auto_login_list(self) -> None:
        if self.login_test_worker and self.login_test_worker.isRunning():
            return

        config = core.IniLoader.load_config(self.config_path)
        rules = core.IniLoader.load_server_rules(self.server_path)
        self.login_test_worker = LoginTestWorker(self.login_entries, config, rules, self.default_settings, self.root_dir)
        self.login_test_worker.row_update.connect(self._on_login_test_row_update)
        self.login_test_worker.finished.connect(self._on_login_test_finished)
        self.login_test_worker.start()

    def _on_login_test_row_update(self, email: str, protocol: str, server: str, status: str, progress: str) -> None:
        self._update_login_row(email, protocol, server, status, progress)
        if status:
            note = f' {progress}' if progress else ''
            self.append_log(f'{email} [{protocol}] {status}{note}\n')

    def _on_login_test_finished(self) -> None:
        self.append_log('Login auto-test complete for imported list.\n')

    def _update_login_row(self, email: str, protocol: str = None, server: str = None, status: str = None, progress: str = None) -> None:
        """Update a login table row with current operation information."""
        for row in range(self.login_table.rowCount()):
            email_item = self.login_table.item(row, 0)
            if email_item and email_item.text() == email:
                if protocol:
                    self.login_table.setItem(row, 1, QTableWidgetItem(protocol))
                if server:
                    self.login_table.setItem(row, 2, QTableWidgetItem(server))
                if status:
                    self.login_table.setItem(row, 3, QTableWidgetItem(status))
                if progress:
                    self.login_table.setItem(row, 4, QTableWidgetItem(progress))
                return
        self._add_login_row(email, protocol or '', server or '', status or '', progress or '')

    def on_login_table_context_menu(self, pos) -> None:
        index = self.login_table.indexAt(pos)
        if not index.isValid():
            return
        email = self.login_table.item(index.row(), 0).text()
        menu = QMenu(self)
        menu.addAction(f'OAuth IMAP - {email}', lambda e=email: self._oauth_for_entry('IMAP', e))
        menu.addAction(f'OAuth Exchange - {email}', lambda e=email: self._oauth_for_entry('Exchange', e))
        menu.addSeparator()
        menu.addAction(f'Auto-login - {email}', lambda e=email: self._auto_login_entry(e))
        menu.exec(self.login_table.mapToGlobal(pos))

    def on_login_table_double_click(self, item) -> None:
        email = self.login_table.item(item.row(), 0).text()
        self._auto_login_entry(email)

    def _oauth_for_entry(self, provider: str, email: str) -> None:
        self.email_edit.setText(email)
        self.on_oauth_login(provider)

    def _auto_login_entry(self, email: str) -> None:
        for entry_email, entry_password, _ in self.login_entries:
            if entry_email == email:
                self.email_edit.setText(entry_email)
                self.password_edit.setText(entry_password)
                self.on_start_button()
                return

    def _get_secure_token_dir(self) -> Path:
        token_dir = self.root_dir / '.oauth_tokens'
        token_dir.mkdir(parents=True, exist_ok=True)
        return token_dir

    def save_oauth_token(self, provider: str, email: str, token_data: dict) -> bool:
        try:
            token_dir = self._get_secure_token_dir()
            token_file = token_dir / f'{provider}_{email}.json'
            now = time.time()
            token_data['provider'] = provider
            token_data['email'] = email
            token_data['saved_at'] = now
            if 'expires_in' in token_data and 'expires_at' not in token_data:
                try:
                    token_data['expires_at'] = now + float(token_data.get('expires_in', 0))
                except Exception:
                    pass
            elif 'expires_on' in token_data and 'expires_at' not in token_data:
                try:
                    token_data['expires_at'] = float(token_data.get('expires_on'))
                except Exception:
                    pass
            with token_file.open('w', encoding='utf-8') as fp:
                json.dump(token_data, fp)
            return True
        except Exception as exc:
            self.append_log(f'Failed to save OAuth token: {exc}\n')
            return False

    def _is_oauth_token_valid(self, token_data: dict) -> bool:
        if not token_data or 'access_token' not in token_data:
            return False

        now = time.time()
        expires_at = token_data.get('expires_at')
        if expires_at is not None:
            try:
                if float(expires_at) <= now:
                    return False
            except Exception:
                return True

        expires_on = token_data.get('expires_on')
        if expires_on is not None:
            try:
                if float(expires_on) <= now:
                    return False
            except Exception:
                return True

        expires_in = token_data.get('expires_in')
        if expires_in is not None:
            saved_at = token_data.get('saved_at', now)
            try:
                if float(saved_at) + float(expires_in) <= now:
                    return False
            except Exception:
                return True

        return True

    def load_oauth_token(self, provider: str, email: str) -> Optional[dict]:
        try:
            token_dir = self._get_secure_token_dir()
            token_file = token_dir / f'{provider}_{email}.json'
            if not token_file.exists():
                return None
            with token_file.open('r', encoding='utf-8') as fp:
                token_data = json.load(fp)
            if token_data and self._is_oauth_token_valid(token_data):
                return token_data
            if token_data is not None:
                self.append_log(f'Saved OAuth token for {email} is expired or invalid.\n')
            return None
        except Exception as exc:
            self.append_log(f'Failed to load OAuth token: {exc}\n')
            return None

    def on_oauth_login(self, provider: str) -> None:
        if self._oauth_in_progress:
            QMessageBox.information(self, 'OAuth In Progress', 'An OAuth login is already running.')
            return

        email = self.email_edit.text().strip()
        if not email:
            QMessageBox.warning(self, 'Email Required', 'Please enter an email address before using OAuth.')
            return

        token_data = self.load_oauth_token(provider, email)
        if token_data and self._is_oauth_token_valid(token_data):
            self.oauth_provider = provider
            self.oauth_token_data = token_data
            self.oauth_access_token = str(token_data.get('access_token', ''))
            self._update_oauth_status(f'Loaded saved {provider} token for {email}', 'green')
            self.append_log(f'Using saved OAuth token for {email}.\n')
            self._apply_oauth_connection(provider, email)
            return

        try:
            import msal
        except ImportError:
            QMessageBox.critical(self, 'OAuth Missing', 'The msal library is required for OAuth login. Install it with: pip install msal')
            return

        self._oauth_in_progress = True
        self.oauth_provider = provider
        self._update_oauth_status('OAuth pending...', 'orange')
        self.append_log(f'Starting OAuth login for {provider}...\n')

        try:
            token_result = self._run_oauth_flow(provider, email)
            if not token_result or 'access_token' not in token_result:
                raise RuntimeError('OAuth login did not return a valid access token.')
            self.oauth_access_token = str(token_result.get('access_token', ''))
            self.oauth_token_data = token_result
            self.oauth_provider = provider
            self.save_oauth_token(provider, email, token_result)
            self._update_oauth_status(f'{provider} OAuth active for {email}', 'green')
            self.append_log(f'{provider} OAuth login completed for {email}.\n')
            self._apply_oauth_connection(provider, email)
        except Exception as exc:
            QMessageBox.critical(self, 'OAuth Failed', str(exc))
            self.append_log(f'OAuth {provider} failed: {exc}\n')
            self._update_oauth_status('OAuth failed', 'red')
        finally:
            self._oauth_in_progress = False

    def _run_oauth_flow(self, provider: str, email: str) -> dict:
        import msal

        redirect_uri = self.oauth_redirect_uri or 'com.emclient.MailClient://oauth'
        authority = self.oauth_authority or 'https://login.microsoftonline.com/common'
        client_id = self.oauth_client_id or 'e9a7fea1-1cc0-4cd9-a31b-9137ca5deedd'
        scope = ['https://outlook.office.com/IMAP.AccessAsUser.All'] if provider == 'IMAP' else ['https://outlook.office365.com/EWS.AccessAsUser.All']

        if redirect_uri.startswith('com.emclient.MailClient://') or not redirect_uri.startswith('http'):
            return self._run_oauth_flow_helper(provider, email, scope, redirect_uri, client_id, authority)

        app = msal.PublicClientApplication(client_id=client_id, authority=authority)
        token_result = app.acquire_token_interactive(
            scopes=scope,
            login_hint=email or None,
            prompt='consent',
        )
        if not token_result or 'access_token' not in token_result:
            raise RuntimeError('OAuth exchange did not return an access token.')
        return token_result

    def _run_oauth_flow_helper(self, provider: str, email: str, scope: list[str], redirect_uri: str, client_id: str, authority: str) -> dict:
        helper_script = Path(__file__).resolve()
        if not helper_script.exists():
            raise RuntimeError('OAuth helper script not found: ' + str(helper_script))

        helper_fd, helper_path = tempfile.mkstemp(prefix='oauth_result_', suffix='.json')
        os.close(helper_fd)
        result_path = Path(helper_path)

        cmd = [
            sys.executable,
            str(helper_script),
            '--oauth-helper',
            '--provider', provider,
            '--client-id', client_id,
            '--authority', authority,
            '--email', email,
            '--scope', '|'.join(scope),
            '--result-file', str(result_path),
            '--redirect-uri', redirect_uri,
            '--window-x', '120',
            '--window-y', '120',
            '--window-width', '840',
            '--window-height', '620',
        ]
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        proc = subprocess.Popen(
            cmd,
            creationflags=creationflags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._oauth_helper_proc = proc

        try:
            deadline = time.time() + 600
            payload = None
            while time.time() < deadline:
                if result_path.exists() and result_path.stat().st_size > 0:
                    try:
                        with result_path.open('r', encoding='utf-8') as fp:
                            payload = json.load(fp)
                        break
                    except json.JSONDecodeError:
                        pass
                if proc.poll() is not None and result_path.exists() and result_path.stat().st_size == 0:
                    break
                time.sleep(0.25)
        finally:
            self._oauth_helper_proc = None
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass

        if result_path.exists():
            try:
                result_path.unlink()
            except Exception:
                pass

        if not payload:
            raise RuntimeError('OAuth helper did not return a valid result.')

        if payload.get('status') != 'ok':
            raise RuntimeError(str(payload.get('error', 'OAuth helper failed.')))

        token_result = payload.get('token_result')
        if not token_result or 'access_token' not in token_result:
            raise RuntimeError('OAuth helper did not return an access token.')

        return token_result

    def _update_oauth_status(self, text: str, color: str | None = None) -> None:
        self.oauth_status_label.setText(text)
        if color is None:
            if 'active' in text.lower() or 'ready' in text.lower() or 'loaded' in text.lower():
                color = 'green'
            elif 'pending' in text.lower() or 'waiting' in text.lower():
                color = 'orange'
            elif 'failed' in text.lower() or 'error' in text.lower():
                color = 'red'
            else:
                color = '#333333'
        self.oauth_status_label.setStyleSheet(f'color: {color};')

    def _apply_oauth_connection(self, provider: str, email: str) -> None:
        self.email_edit.setText(email)
        self.provider_combo.setCurrentText(provider)
        if provider == 'IMAP':
            if not self.server_edit.text().strip():
                self.server_edit.setText('outlook.office365.com')
            if not self.port_edit.text().strip():
                self.port_edit.setText('993')
            self.ssl_checkbox.setChecked(True)
            self.password_edit.clear()
        elif provider == 'Exchange':
            self.password_edit.clear()

        QTimer.singleShot(200, self._start_fetch_after_oauth)

    def _start_fetch_after_oauth(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        if self._oauth_in_progress:
            return
        self.on_start_button()

    def _prepare_oauth_for_email(self, provider: str, email: str) -> None:
        token_data = self.load_oauth_token(provider, email)
        if token_data and isinstance(token_data, dict):
            self.oauth_token_data = token_data
            self.oauth_access_token = str(token_data.get('access_token', ''))
            self.oauth_provider = provider
            self._update_oauth_status(f'Loaded saved {provider} token for {email}', 'green')
            self.append_log(f'Using saved {provider} OAuth token for {email}.\n')
        else:
            self.oauth_token_data = None
            self.oauth_access_token = None
            self.oauth_provider = None
            self._update_oauth_status('No OAuth token loaded', 'orange')
            self.append_log(f'No saved {provider} OAuth token found for {email}.\n')

    def on_start_button(self) -> None:
        debug_path = Path(tempfile.gettempdir()) / 'app_debug.log'
        try:
            with debug_path.open('a', encoding='utf-8') as dbg:
                dbg.write(f'on_start_button START {datetime.now().isoformat()} worker_running={bool(self.worker and self.worker.isRunning())}\n')
        except Exception:
            pass

        if self.worker and self.worker.isRunning():
            if self.current_fetcher:
                self.current_fetcher.request_abort()
            self.append_log('Requested cancellation of the current fetch.\n')
            self.start_button.setEnabled(False)
            return

        try:
            fetcher = self._build_fetcher()
        except Exception as exc:
            QMessageBox.critical(self, 'Validation Error', str(exc))
            return

        self.current_fetcher = fetcher
        self.worker = FetchWorker(fetcher)
        self.worker.log_message.connect(self.append_log)
        self.worker.status_update.connect(self.update_status)
        self.worker.dashboard_update.connect(self._update_login_row)
        self.worker.finished_signal.connect(self.on_fetch_finished)
        try:
            self.worker.start()
            with debug_path.open('a', encoding='utf-8') as dbg:
                dbg.write(f'FetchWorker.start called {datetime.now().isoformat()}\n')
        except Exception as exc:
            with debug_path.open('a', encoding='utf-8') as dbg:
                dbg.write(f'FetchWorker.start failed {datetime.now().isoformat()} exc={exc}\n')
            raise

        self.start_button.setText('Stop')
        self.status_label.setText('Fetching...')
        self.append_log('Started fetch.\n')
        try:
            with debug_path.open('a', encoding='utf-8') as dbg:
                dbg.write(f'on_start_button END {datetime.now().isoformat()}\n')
        except Exception:
            pass

    def _build_fetcher(self) -> core.BaseFetcher:
        provider = self.provider_combo.currentText()
        email_address = self.email_edit.text().strip()
        password = self.password_edit.text()
        server = self.server_edit.text().strip() or None
        port = self._parse_int(self.port_edit.text().strip())
        use_ssl = self.ssl_checkbox.isChecked()
        output_dir = Path(self.output_edit.text().strip() or str(self.output_dir))
        output_dir.mkdir(parents=True, exist_ok=True)

        if not email_address:
            raise ValueError('Email / login is required.')

        settings = self.default_settings
        config = core.IniLoader.load_config(self.config_path)
        rules = core.IniLoader.load_server_rules(self.server_path)

        if settings.attachment_extensions:
            config.attachment_whitelist = [ext.lower().lstrip('.') for ext in settings.attachment_extensions]
            config.attachment_blacklist = []

        if provider in ('IMAP', 'POP3'):
            server, port, use_ssl = self._autodetect_server(provider, email_address, server, port, use_ssl)
            if server:
                self.server_edit.setText(server)
            if port is not None:
                self.port_edit.setText(str(port))
            self.ssl_checkbox.setChecked(use_ssl)

        if not server:
            raise ValueError('Server host is required for the selected provider or leave the email domain valid for autodiscovery.')

        if provider == 'IMAP':
            if not self.oauth_access_token:
                self._prepare_oauth_for_email('IMAP', email_address)
            return core.IMAPFetcher(
                email_address,
                password,
                server,
                port,
                use_ssl,
                settings,
                config,
                output_dir,
                oauth_access_token=self.oauth_access_token,
            )
        if provider == 'POP3':
            return core.POPFetcher(email_address, password, server, port, use_ssl, settings, config, output_dir)
        if provider == 'Exchange':
            if not core.EXCHANGE_AVAILABLE:
                raise RuntimeError('Exchange support requires exchangelib. Install it and restart the application.')
            if not self.oauth_token_data:
                self._prepare_oauth_for_email('Exchange', email_address)
            return core.ExchangeFetcher(
                email_address,
                password,
                server,
                port,
                use_ssl,
                settings,
                config,
                output_dir,
                oauth_token_data=self.oauth_token_data,
                oauth_client_id=self.oauth_client_id,
            )
        raise ValueError(f'Unsupported provider: {provider}')

    def _parse_int(self, value: str) -> Optional[int]:
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            raise ValueError('Port must be a number.')

    def update_status(self, text: str) -> None:
        self.status_label.setText(text)

    def append_log(self, text: str) -> None:
        """Log messages to console (dashboard removed)."""
        if text:
            try:
                sys.__stdout__.write(text.rstrip() + '\n')
                sys.__stdout__.flush()
            except Exception:
                pass

    def on_fetch_finished(self, success: bool) -> None:
        self.start_button.setText('Start Fetch')
        self.start_button.setEnabled(True)
        self.status_label.setText('Completed' if success else 'Stopped')
        if success:
            self.append_log('Fetch finished successfully.\n')
        else:
            self.append_log('Fetch stopped or failed.\n')
        self.worker = None
        self.current_fetcher = None

    def closeEvent(self, event) -> None:
        debug_path = Path(tempfile.gettempdir()) / 'fetch_worker_debug.log'
        try:
            with debug_path.open('a', encoding='utf-8') as dbg:
                dbg.write(f'CloseEvent START {datetime.datetime.now().isoformat()}\n')
        except Exception:
            pass
        if self.worker and self.worker.isRunning():
            self.append_log('Closing application: aborting active fetch...\n')
            try:
                self.worker.request_abort()
            except Exception:
                pass
            self.start_button.setEnabled(False)
            self.worker.wait(3000)
            if self.worker.isRunning():
                self.append_log('Fetch thread did not stop before application closed.\n')

        if self._oauth_helper_proc is not None and self._oauth_helper_proc.poll() is None:
            try:
                self._oauth_helper_proc.terminate()
            except Exception:
                pass
            self._oauth_helper_proc = None
        try:
            with debug_path.open('a', encoding='utf-8') as dbg:
                dbg.write(f'CloseEvent END {datetime.datetime.now().isoformat()}\n')
        except Exception:
            pass
        super().closeEvent(event)


class SettingsDialog(QWidget):
    def __init__(self, parent, settings: core.FetchSettings):
        super().__init__()
        self.main_window = parent
        self.setWindowTitle('Settings')
        # Allow smaller windows so the button row can remain visible
        self.setMinimumSize(360, 240)
        self.settings = settings

        layout = QVBoxLayout(self)

        tab = QTabWidget()
        tab.addTab(self._build_search_tab(), 'Search & Filter')
        tab.addTab(self._build_options_tab(), 'Save Options')
        tab.addTab(self._build_advanced_tab(), 'Advanced')

        # Put the tab widget inside a scroll area so its contents scroll
        # when the window is small while the button row stays visible.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(tab)
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(scroll)
        layout.setStretch(0, 1)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        back_btn = QPushButton('Back')
        back_btn.clicked.connect(self.on_back)
        btn_layout.addWidget(back_btn)
        save_btn = QPushButton('Save')
        save_btn.clicked.connect(self.on_save)
        btn_layout.addWidget(save_btn)

        btn_widget = QWidget()
        btn_widget.setLayout(btn_layout)
        btn_widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        btn_widget.setFixedHeight(48)
        layout.addWidget(btn_widget)

        QShortcut(QKeySequence('Esc'), self, activated=self.on_back)

        self.setLayout(layout)

    def _build_search_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        layout.addWidget(QLabel('Keyword:'))
        self.keyword_edit = QLineEdit(self.settings.keyword or '')
        layout.addWidget(self.keyword_edit)

        layout.addWidget(QLabel('Date from (YYYY-MM-DD):'))
        self.date_from_edit = QLineEdit(self.settings.date_from.isoformat() if self.settings.date_from else '')
        layout.addWidget(self.date_from_edit)

        layout.addWidget(QLabel('Date to (YYYY-MM-DD):'))
        self.date_to_edit = QLineEdit(self.settings.date_to.isoformat() if self.settings.date_to else '')
        layout.addWidget(self.date_to_edit)

        self.search_subject_checkbox = QCheckBox('Search subject')
        self.search_subject_checkbox.setChecked(self.settings.search_subject)
        layout.addWidget(self.search_subject_checkbox)

        self.search_body_checkbox = QCheckBox('Search body')
        self.search_body_checkbox.setChecked(self.settings.search_body)
        layout.addWidget(self.search_body_checkbox)

        layout.addStretch()
        return widget

    def _build_options_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        self.save_results_checkbox = QCheckBox('Save contact list (CSV results)')
        self.save_results_checkbox.setChecked(self.settings.save_email_result)
        layout.addWidget(self.save_results_checkbox)

        self.save_content_checkbox = QCheckBox('Save full email content')
        self.save_content_checkbox.setChecked(self.settings.save_content)
        layout.addWidget(self.save_content_checkbox)

        self.save_attachments_checkbox = QCheckBox('Save attachments')
        self.save_attachments_checkbox.setChecked(self.settings.save_attachments)
        layout.addWidget(self.save_attachments_checkbox)

        self.save_log_checkbox = QCheckBox('Save log file')
        self.save_log_checkbox.setChecked(self.settings.save_log)
        layout.addWidget(self.save_log_checkbox)

        layout.addWidget(QLabel('Extract fields from messages:'))
        self.extract_subject_checkbox = QCheckBox('Subject')
        self.extract_subject_checkbox.setChecked(self.settings.extract_subject)
        layout.addWidget(self.extract_subject_checkbox)

        self.extract_date_checkbox = QCheckBox('Date')
        self.extract_date_checkbox.setChecked(self.settings.extract_date)
        layout.addWidget(self.extract_date_checkbox)

        self.extract_attachments_checkbox = QCheckBox('Attachment list')
        self.extract_attachments_checkbox.setChecked(self.settings.extract_attachments_list)
        layout.addWidget(self.extract_attachments_checkbox)

        self.extract_summary_checkbox = QCheckBox('Other extractable list (summary)')
        self.extract_summary_checkbox.setChecked(self.settings.extract_summary)
        layout.addWidget(self.extract_summary_checkbox)

        layout.addWidget(QLabel('Attachment types to save (leave blank for default):'))
        self.attachment_pdf_checkbox = QCheckBox('PDF')
        self.attachment_pdf_checkbox.setChecked('pdf' in self.settings.attachment_extensions)
        layout.addWidget(self.attachment_pdf_checkbox)

        self.attachment_csv_checkbox = QCheckBox('CSV')
        self.attachment_csv_checkbox.setChecked('csv' in self.settings.attachment_extensions)
        layout.addWidget(self.attachment_csv_checkbox)

        self.attachment_docx_checkbox = QCheckBox('DOCX')
        self.attachment_docx_checkbox.setChecked('docx' in self.settings.attachment_extensions)
        layout.addWidget(self.attachment_docx_checkbox)

        self.attachment_xlsx_checkbox = QCheckBox('XLSX')
        self.attachment_xlsx_checkbox.setChecked('xlsx' in self.settings.attachment_extensions)
        layout.addWidget(self.attachment_xlsx_checkbox)

        self.attachment_txt_checkbox = QCheckBox('TXT')
        self.attachment_txt_checkbox.setChecked('txt' in self.settings.attachment_extensions)
        layout.addWidget(self.attachment_txt_checkbox)

        layout.addStretch()
        return widget

    def _build_advanced_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        layout.addWidget(QLabel('Threads:'))
        self.thread_spin = QSpinBox()
        self.thread_spin.setRange(1, 50)
        self.thread_spin.setValue(self.settings.thread_count)
        layout.addWidget(self.thread_spin)

        layout.addWidget(QLabel('IMAP batch size:'))
        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 500)
        self.batch_spin.setValue(self.settings.batch_size)
        layout.addWidget(self.batch_spin)

        layout.addStretch()
        return widget

    def on_save(self) -> None:
        self._apply_settings()
        if self.main_window is not None and hasattr(self.main_window, 'save_settings'):
            self.main_window.save_settings(self.settings)
        if self.main_window is not None and hasattr(self.main_window, 'append_log'):
            self.main_window.append_log('Settings saved.\n')

    def on_back(self) -> None:
        if self.main_window is not None and hasattr(self.main_window, 'show_main_page'):
            self.main_window.show_main_page()

    def on_close(self) -> None:
        self.on_back()

    def closeEvent(self, event) -> None:
        self._apply_settings()
        if self.main_window is not None and hasattr(self.main_window, 'show_main_page'):
            self.main_window.show_main_page()
        super().closeEvent(event)

    def _apply_settings(self) -> None:
        self.settings.save_email_result = self.save_results_checkbox.isChecked()
        self.settings.save_content = self.save_content_checkbox.isChecked()
        self.settings.save_attachments = self.save_attachments_checkbox.isChecked()
        self.settings.save_log = self.save_log_checkbox.isChecked()
        self.settings.extract_subject = self.extract_subject_checkbox.isChecked()
        self.settings.extract_date = self.extract_date_checkbox.isChecked()
        self.settings.extract_attachments_list = self.extract_attachments_checkbox.isChecked()
        self.settings.extract_summary = self.extract_summary_checkbox.isChecked()

        selected_extensions = []
        if self.attachment_pdf_checkbox.isChecked():
            selected_extensions.append('pdf')
        if self.attachment_csv_checkbox.isChecked():
            selected_extensions.append('csv')
        if self.attachment_docx_checkbox.isChecked():
            selected_extensions.append('docx')
        if self.attachment_xlsx_checkbox.isChecked():
            selected_extensions.append('xlsx')
        if self.attachment_txt_checkbox.isChecked():
            selected_extensions.append('txt')

        self.settings.attachment_extensions = selected_extensions

        if self.parent() is not None and hasattr(self.parent(), 'default_config'):
            if selected_extensions:
                self.parent().default_config.attachment_whitelist = selected_extensions
                self.parent().default_config.attachment_blacklist = []

        if self.parent() is not None and hasattr(self.parent(), 'save_settings'):
            self.parent().save_settings(self.settings)


def _write_oauth_result(result_path: Path, payload: dict) -> None:
    """Write OAuth result to temporary file."""
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
        print(f'Error writing OAuth result: {type(e).__name__}: {e}', file=sys.stderr)
        import traceback
        traceback.print_exc()


def _handle_oauth_callback(callback_uri: str) -> None:
    """Handle OAuth protocol callback with full token exchange."""
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
        pkce_files = sorted(temp_dir.glob('pkce_oauth_result_*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        log_msg(f'Found {len(pkce_files)} PKCE files')

        # Find PKCE file that matches the state parameter exactly
        callback_state = callback_params.get('state', '')
        pkce_file = None
        pkce_data = None

        if callback_state:
            for pf in pkce_files:
                try:
                    with pf.open('r', encoding='utf-8') as fp:
                        pd = json.load(fp)
                        if pd.get('flow', {}).get('state') == callback_state:
                            pkce_file = pf
                            pkce_data = pd
                            log_msg(f'Found matching PKCE file: {pf.name} with state: {callback_state}')
                            break
                except (json.JSONDecodeError, Exception):
                    log_msg(f'Failed to read PKCE file {pf.name}; skipping.')
                    continue
        else:
            log_msg('Callback state is missing from the OAuth redirect.')

        if not pkce_file or not pkce_data:
            if callback_state:
                log_msg(f'No PKCE file matched callback state: {callback_state}. Aborting exchange to avoid state mismatch.')
            else:
                log_msg('No PKCE file available for callback without state. Aborting exchange.')
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


def _handle_oauth_helper_mode() -> bool:
    """Check if running in OAuth helper subprocess mode."""
    if '--oauth-helper' not in sys.argv:
        return False
    
    # Running as helper subprocess for OAuth flow
    try:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument('--oauth-helper', action='store_true')
        parser.add_argument('--provider', default='IMAP')
        parser.add_argument('--client-id')
        parser.add_argument('--authority')
        parser.add_argument('--email')
        parser.add_argument('--scope')
        parser.add_argument('--result-file')
        parser.add_argument('--redirect-uri')
        parser.add_argument('--window-x', type=int, default=0)
        parser.add_argument('--window-y', type=int, default=0)
        parser.add_argument('--window-width', type=int, default=840)
        parser.add_argument('--window-height', type=int, default=620)
        
        args = parser.parse_args()
        
        # Register protocol handler for this process
        if os.name == 'nt':
            try:
                ProtocolHandler.register_protocol_handler(str(Path(__file__).resolve()))
            except Exception:
                pass
        
        # Open browser for OAuth login
        import msal
        import webbrowser
        
        scope = args.scope.split('|') if args.scope else []
        result_file = Path(args.result_file)
        
        app = msal.PublicClientApplication(
            client_id=args.client_id,
            authority=args.authority
        )
        
        flow = app.initiate_auth_code_flow(
            scopes=scope,
            redirect_uri=args.redirect_uri,
            login_hint=args.email or None,
            prompt='consent',
        )
        
        # Save PKCE flow data for callback handler
        pkce_file = result_file.parent / f'pkce_oauth_result_{int(time.time() * 1000)}.json'
        pkce_data = {
            'flow': flow,
            'client_id': args.client_id,
            'authority': args.authority,
            'email': args.email,
            'result_file': str(result_file),
        }
        
        with pkce_file.open('w', encoding='utf-8') as fp:
            json.dump(pkce_data, fp)
        
        # Open browser
        auth_uri = flow.get('auth_uri')
        if auth_uri:
            webbrowser.open(auth_uri, new=2)
        
        # Wait for callback (up to 10 minutes)
        deadline = time.time() + 600
        while time.time() < deadline:
            if result_file.exists() and result_file.stat().st_size > 0:
                try:
                    with result_file.open('r', encoding='utf-8') as fp:
                        payload = json.load(fp)
                    # Success - result written by callback handler
                    break
                except json.JSONDecodeError:
                    pass
            time.sleep(0.5)
        
        # Cleanup PKCE file
        try:
            pkce_file.unlink()
        except Exception:
            pass
        
        return True
    except Exception as exc:
        print(f'OAuth helper mode failed: {exc}', file=sys.stderr)
        return True  # Still exit to avoid launching GUI


def _check_for_callback_request() -> bool:
    if '--oauth-callback' in sys.argv:
        idx = sys.argv.index('--oauth-callback')
        if idx + 1 < len(sys.argv):
            _handle_oauth_callback(sys.argv[idx + 1])
            return True
    for arg in sys.argv[1:]:
        if arg.startswith('com.emclient.MailClient://'):
            _handle_oauth_callback(arg)
            return True
    return False


def main() -> None:
    debug_path = Path(tempfile.gettempdir()) / 'app_debug.log'
    try:
        with debug_path.open('a', encoding='utf-8') as dbg:
            dbg.write(f'main START {datetime.now().isoformat()} argv={sys.argv}\n')
    except Exception:
        pass
    if os.name == 'nt':
        try:
            ProtocolHandler.register_protocol_handler(str(Path(__file__).resolve()))
        except Exception:
            pass
    app = QApplication(sys.argv)
    window = MailFetcherWindow()
    window.show()
    exit_code = app.exec()
    try:
        with debug_path.open('a', encoding='utf-8') as dbg:
            dbg.write(f'main EXIT {datetime.now().isoformat()} exit_code={exit_code}\n')
    except Exception:
        pass
    sys.exit(exit_code)


if __name__ == '__main__':
    if _handle_oauth_helper_mode():
        sys.exit(0)
    if _check_for_callback_request():
        sys.exit(0)
    main()
