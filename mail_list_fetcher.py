import argparse
import configparser
import csv
import datetime
import email
import imaplib
import logging
import os
import poplib
import re
import socket
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from exchangelib import Account, Configuration, Credentials, DELEGATE, Q, OAuth2AuthorizationCodeCredentials
    EXCHANGE_AVAILABLE = True
except ImportError:
    EXCHANGE_AVAILABLE = False

logger = logging.getLogger(__name__)

IMAP_TIMEOUT = 30
POP3_TIMEOUT = 30


def setup_file_logging(output_dir: Path) -> Optional[logging.FileHandler]:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / 'fetch_log.txt'
    handler = logging.FileHandler(log_path, encoding='utf-8')
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG)
    return handler


def connect_with_retry(connect_fn, retries: int = 2, delay: float = 3.0, label: str = 'server'):
    last_exc = None
    for attempt in range(1, retries + 2):
        try:
            return connect_fn()
        except (socket.timeout, ConnectionRefusedError, ConnectionResetError, TimeoutError, ssl.SSLError, OSError) as exc:
            last_exc = exc
            if attempt <= retries:
                print(f'{label}: connection attempt {attempt} failed ({exc}), retrying in {delay}s...')
                time.sleep(delay)
            else:
                raise


@dataclass
class FetchConfig:
    folder_whitelist: List[str] = field(default_factory=list)
    folder_blacklist: List[str] = field(default_factory=list)
    attachment_whitelist: List[str] = field(default_factory=list)
    attachment_blacklist: List[str] = field(default_factory=list)


@dataclass
class FetchSettings:
    thread_count: int = 1
    save_content: bool = True
    save_attachments: bool = True
    save_correct_account: bool = False
    save_separated_file: bool = True
    save_email_result: bool = True
    keyword: str = ""
    date_from: Optional[datetime.date] = None
    date_to: Optional[datetime.date] = None
    search_subject: bool = True
    search_body: bool = True
    save_log: bool = True
    oauth_client_id: str = 'e9a7fea1-1cc0-4cd9-a31b-9137ca5deedd'
    oauth_authority: str = 'https://login.microsoftonline.com/common'
    oauth_redirect_uri: str = 'com.emclient.MailClient://oauth'
    connection_timeout: int = 30
    connection_retries: int = 2


@dataclass
class ServerRule:
    matcher: str
    include_type: str
    server: Optional[str] = None
    url: Optional[str] = None
    port: Optional[int] = None
    encryption: Optional[str] = None


class IniLoader:
    @staticmethod
    def read_list_section(file_path: Path, section_name: str) -> List[str]:
        lines: List[str] = []
        active = False
        if not file_path.exists():
            return lines
        with file_path.open('r', encoding='utf-8', errors='ignore') as stream:
            for raw in stream:
                line = raw.strip()
                if not line or line.startswith('#'):
                    continue
                if line.startswith('[') and line.endswith(']'):
                    active = line.lower() == f'[{section_name.lower()}]'
                    continue
                if active:
                    lines.append(line)
        return lines

    @staticmethod
    def load_config(path: Path) -> FetchConfig:
        return FetchConfig(
            folder_whitelist=[item.strip() for item in IniLoader.read_list_section(path, 'FoldNameWhiteList')],
            folder_blacklist=[item.strip() for item in IniLoader.read_list_section(path, 'FoldNameBlackList')],
            attachment_whitelist=[item.strip().lower() for item in IniLoader.read_list_section(path, 'AttachmentExtensionWhiteList')],
            attachment_blacklist=[item.strip().lower() for item in IniLoader.read_list_section(path, 'AttachmentExtensionBlackList')],
        )

    @staticmethod
    def load_server_rules(path: Path) -> List[ServerRule]:
        rules: List[ServerRule] = []
        if not path.exists():
            return rules
        with path.open('r', encoding='utf-8', errors='ignore') as stream:
            for raw in stream:
                line = raw.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                fields = [part.strip() for part in value.split('|') if part.strip()]
                data = {field.split('=', 1)[0].strip(): field.split('=', 1)[1].strip() if '=' in field else '' for field in fields}
                server = data.get('Server')
                url = data.get('Url')
                port = int(data['Port']) if data.get('Port', '').isdigit() else None
                encryption = data.get('Encryption')
                rules.append(ServerRule(matcher=value.split('|', 1)[0].replace(key + '=', ''), include_type=data.get('Type', ''), server=server, url=url, port=port, encryption=encryption))
        return rules

    @staticmethod
    def load_settings(path: Path) -> FetchSettings:
        config = configparser.ConfigParser()
        if not path.exists():
            return FetchSettings()
        with path.open('r', encoding='utf-8', errors='ignore') as fp:
            config.read_file(fp)
        section = config['MailListFetcher'] if 'MailListFetcher' in config else {}

        def _parse_date(value: str) -> Optional[datetime.date]:
            value = value.strip()
            if not value:
                return None
            for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%d-%m-%Y', '%d/%m/%Y'):
                try:
                    return datetime.datetime.strptime(value, fmt).date()
                except ValueError:
                    continue
            return None

        return FetchSettings(
            thread_count=int(section.get('ThreadCount', 1)),
            save_content=section.get('ChkSaveContent', '1') != '0',
            save_attachments=section.get('ChkSaveAttachment', '1') != '0',
            save_correct_account=section.get('ChkSaveCorrectAccount', '0') == '1',
            save_separated_file=section.get('ChkToSeparatedFile', '1') == '1',
            save_email_result=section.get('ChkSaveEmailResult', '1') != '0',
            keyword=section.get('Keyword', '').strip(),
            date_from=_parse_date(section.get('DateFrom', '').strip()),
            date_to=_parse_date(section.get('DateTo', '').strip()),
            search_subject=section.get('ChkSearchFromSubject', '1') != '0',
            search_body=section.get('ChkSearchFromBody', '1') != '0',
            save_log=section.get('ChkSaveLogFiles', '1') != '0',
            oauth_client_id=section.get('OauthClientId', 'e9a7fea1-1cc0-4cd9-a31b-9137ca5deedd'),
            oauth_authority=section.get('OauthAuthority', 'https://login.microsoftonline.com/common'),
            oauth_redirect_uri=section.get('OauthRedirectUri', 'com.emclient.MailClient://oauth'),
        connection_timeout=int(section.get('ConnectionTimeout', '30')),
        connection_retries=int(section.get('ConnectionRetries', '2')),
    )


class ServerResolver:
    @staticmethod
    def normalize(text: str) -> str:
        return text.strip().lower()

    @staticmethod
    def value_for(domain: str, mx: Optional[str], provider_type: str, rules: List[ServerRule]) -> Tuple[Optional[str], Optional[int], Optional[str], Optional[str]]:
        domain = ServerResolver.normalize(domain)
        mx = ServerResolver.normalize(mx or '')
        provider_type = provider_type.upper()
        fallback = None
        for rule in rules:
            matcher = ServerResolver.normalize(rule.matcher)
            if matcher == '.':
                continue
            matched = False
            if matcher.startswith('.') and (domain.endswith(matcher) or domain == matcher[1:] or mx.endswith(matcher)):
                matched = True
            elif matcher == domain or matcher in mx:
                matched = True
            if not matched:
                continue
            if provider_type in rule.include_type or 'ALL' in rule.include_type:
                if rule.server or rule.url:
                    return rule.server, rule.port, rule.encryption, rule.url
                if fallback is None:
                    fallback = (rule.server, rule.port, rule.encryption, rule.url)
        for rule in rules:
            matcher = ServerResolver.normalize(rule.matcher)
            if matcher != '.':
                continue
            if provider_type in rule.include_type or 'ALL' in rule.include_type:
                server = rule.server or ''
                url = rule.url or ''
                if '#domain#' in server:
                    server = server.replace('#domain#', domain)
                if '#domain#' in url:
                    url = url.replace('#domain#', domain)
                if '#mx#' in server:
                    continue
                if '#mx#' in url:
                    continue
                if server or url:
                    return server or None, rule.port, rule.encryption, url or None
                if fallback is None:
                    fallback = (server or rule.server, rule.port, rule.encryption, url or rule.url)
        return fallback if fallback else (None, None, None, None)

    @staticmethod
    def choose_server(domain: str, provider_type: str, rules: List[ServerRule], mx: Optional[str] = None) -> Tuple[Optional[str], Optional[int], Optional[str], Optional[str]]:
        result = ServerResolver.value_for(domain, mx, provider_type, rules)
        if result[0] or result[3]:
            return result
        if provider_type == 'IMAP':
            return f'imap.{domain}', 993, 'SSL', None
        if provider_type == 'POP3':
            return f'pop.{domain}', 995, 'SSL', None
        return (None, None, None, None)

    @staticmethod
    def find_all_servers(domain: str, rules: List[ServerRule], mx: Optional[str] = None) -> List[Tuple[str, Optional[str], Optional[int], Optional[str], Optional[str]]]:
        domain_n = ServerResolver.normalize(domain)
        mx_n = ServerResolver.normalize(mx or '')
        seen: set = set()
        concrete_results: List[Tuple[str, Optional[str], Optional[int], Optional[str], Optional[str]]] = []
        template_results: List[Tuple[str, Optional[str], Optional[int], Optional[str], Optional[str]]] = []
        for rule in rules:
            matcher = ServerResolver.normalize(rule.matcher)
            matched = False
            if matcher.startswith('.') and (domain_n.endswith(matcher) or domain_n == matcher[1:] or mx_n.endswith(matcher)):
                matched = True
            elif matcher == '.' or matcher == domain_n or matcher in mx_n:
                matched = True
            if not matched:
                continue
            if not (rule.server or rule.url):
                continue
            for proto in rule.include_type.replace('&', '|').replace(',', '|').split('|'):
                proto = proto.strip().upper()
                if not proto or proto == 'ALL':
                    continue
                server = rule.server or ''
                url = rule.url or ''
                is_template = '#domain#' in server or '#mx#' in server or '#domain#' in url or '#mx#' in url or 'replace(' in server
                key = (proto, server, url)
                if key in seen:
                    continue
                seen.add(key)
                entry = (proto, rule.server, rule.port, rule.encryption, rule.url)
                if is_template:
                    template_results.append(entry)
                else:
                    concrete_results.append(entry)
        if concrete_results:
            return concrete_results
        resolved: List[Tuple[str, Optional[str], Optional[int], Optional[str], Optional[str]]] = []
        for proto, server, port, enc, url in template_results:
            if server and ('#domain#' in server or '#mx#' in server):
                if '#domain#' in server:
                    server = server.replace('#domain#', domain)
                else:
                    continue
            if url and '#domain#' in url:
                url = url.replace('#domain#', domain)
            elif url and '#mx#' in url:
                continue
            resolved.append((proto, server or None, port, enc, url or None))
        if not resolved:
            resolved.append(('IMAP', f'imap.{domain}', 993, 'SSL', None))
            resolved.append(('POP3', f'pop.{domain}', 995, 'SSL', None))
        return resolved


def decode_header_value(raw: str) -> str:
    value = ''
    for part, encoding in decode_header(raw):
        if isinstance(part, bytes):
            try:
                value += part.decode(encoding or 'utf-8', errors='replace')
            except LookupError:
                value += part.decode('utf-8', errors='replace')
        else:
            value += part
    return value


def decode_message_text(msg: Message) -> Tuple[str, str, str, str]:
    subject = decode_header_value(msg.get('Subject', ''))
    from_ = decode_header_value(msg.get('From', ''))
    date = decode_header_value(msg.get('Date', ''))
    body_text = ''
    html_text = ''
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get('Content-Disposition', '')).lower()
            if 'attachment' in disp:
                continue
            if ctype == 'text/plain' and not body_text:
                try:
                    body_text = part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', errors='replace')
                except Exception:
                    body_text = part.get_payload(decode=True).decode('utf-8', errors='replace')
            elif ctype == 'text/html' and not html_text:
                try:
                    html_text = part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', errors='replace')
                except Exception:
                    html_text = part.get_payload(decode=True).decode('utf-8', errors='replace')
    else:
        ctype = msg.get_content_type()
        try:
            raw = msg.get_payload(decode=True).decode(msg.get_content_charset() or 'utf-8', errors='replace')
        except Exception:
            raw = msg.get_payload(decode=True).decode('utf-8', errors='replace')
        if ctype == 'text/html':
            html_text = raw
        else:
            body_text = raw
    if not body_text and html_text:
        body_text = re.sub(r'<[^>]+>', ' ', html_text)
        body_text = re.sub(r'\s+', ' ', body_text).strip()
    return subject, from_, date, body_text


def normalize_folder_name(name: str) -> str:
    return name.strip().lower()


def folder_is_allowed(folder: str, config: FetchConfig) -> bool:
    folder_norm = normalize_folder_name(folder)
    if config.folder_whitelist:
        return any(folder_norm == normalize_folder_name(f) for f in config.folder_whitelist)
    if config.folder_blacklist:
        return not any(folder_norm == normalize_folder_name(f) for f in config.folder_blacklist)
    return True


def attachment_is_allowed(filename: str, config: FetchConfig) -> bool:
    ext = Path(filename).suffix.lower().lstrip('.')
    if not ext:
        return False
    if config.attachment_whitelist:
        return ext in config.attachment_whitelist
    if config.attachment_blacklist:
        return ext not in config.attachment_blacklist
    return True


def safe_filename(name: str, fallback: str = 'message') -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', Path(name).name).strip(' .')
    cleaned = re.sub(r'_+', '_', cleaned)
    if not cleaned:
        cleaned = fallback
    max_len = 180
    if len(cleaned) > max_len:
        stem = Path(cleaned).stem
        suffix = Path(cleaned).suffix
        if suffix and len(suffix) < max_len:
            stem = stem[:max_len - len(suffix)]
            cleaned = stem + suffix
        else:
            cleaned = cleaned[:max_len]
    return cleaned


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 2
    while True:
        candidate = parent / f'{stem}_{counter}{suffix}'
        if not candidate.exists():
            return candidate
        counter += 1


def message_date_in_range(date_header: str, settings: FetchSettings) -> bool:
    if not settings.date_from and not settings.date_to:
        return True
    try:
        received = parsedate_to_datetime(date_header)
    except (TypeError, ValueError, IndexError, OverflowError):
        return True
    if received.tzinfo is not None:
        received = received.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    received_date = received.date()
    if settings.date_from and received_date < settings.date_from:
        return False
    if settings.date_to and received_date > settings.date_to:
        return False
    return True


def message_matches_keyword(subject: str, body: str, settings: FetchSettings) -> bool:
    keyword = settings.keyword.strip().lower()
    if not keyword:
        return True
    targets: List[str] = []
    if settings.search_subject:
        targets.append(subject)
    if settings.search_body:
        targets.append(body)
    if not targets:
        return True
    return keyword in '\n'.join(targets).lower()


def build_search_criteria(settings: FetchSettings) -> str:
    terms = []
    if settings.keyword:
        keyword = settings.keyword.replace('"', '')
        if settings.search_subject and settings.search_body:
            terms.append(f'OR SUBJECT "{keyword}" BODY "{keyword}"')
        elif settings.search_subject:
            terms.append(f'SUBJECT "{keyword}"')
        elif settings.search_body:
            terms.append(f'BODY "{keyword}"')
    if settings.date_from:
        terms.append(f'SINCE {settings.date_from.strftime("%d-%b-%Y").upper()}')
    if settings.date_to:
        date_to = settings.date_to + datetime.timedelta(days=1)
        terms.append(f'BEFORE {date_to.strftime("%d-%b-%Y").upper()}')
    if not terms:
        return 'ALL'
    return ' '.join(terms)


def save_attachment(part: Message, out_dir: Path, email_index: int, config: FetchConfig) -> List[str]:
    saved_files: List[str] = []
    filename = part.get_filename()
    if not filename:
        filename = f'attachment-{email_index}.bin'
    filename = decode_header_value(filename)
    if not attachment_is_allowed(filename, config):
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    path = unique_path(out_dir / safe_filename(filename, f'attachment-{email_index}.bin'))
    payload = part.get_payload(decode=True)
    if payload is None:
        return []
    with path.open('wb') as fp:
        fp.write(payload)
    saved_files.append(str(path))
    return saved_files


def save_email_body(msg: Message, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open('w', encoding='utf-8', errors='replace') as fp:
        fp.write(msg.as_string())


class BaseFetcher:
    def __init__(
        self,
        email_address: str,
        password: str,
        server: Optional[str],
        port: Optional[int],
        use_ssl: bool,
        settings: FetchSettings,
        config: FetchConfig,
        output_dir: Path,
        oauth_access_token: Optional[str] = None,
        oauth_token_data: Optional[dict] = None,
        oauth_client_id: Optional[str] = None,
        progress_callback: Optional[callable] = None,
    ):
        self.email_address = email_address
        self.password = password
        self.server = server
        self.port = port
        self.use_ssl = use_ssl
        self.settings = settings
        self.config = config
        self.output_dir = output_dir
        self.oauth_access_token = oauth_access_token
        self.oauth_token_data = oauth_token_data
        self.oauth_client_id = oauth_client_id
        self.results: List[Dict[str, str]] = []
        self.abort_requested = False
        self._log_handler: Optional[logging.FileHandler] = None
        self.progress_callback = progress_callback

    def _report_progress(self, status: str, current: int = 0, total: int = 0, folder: str = '') -> None:
        if self.progress_callback:
            try:
                self.progress_callback(self.email_address, status, current, total, folder)
            except Exception:
                pass

    def _setup_logging(self) -> None:
        if self.settings.save_log:
            self._log_handler = setup_file_logging(self.output_dir)

    def _teardown_logging(self) -> None:
        if self._log_handler:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler.close()
            self._log_handler = None

    def fetch(self) -> List[Dict[str, str]]:
        raise NotImplementedError()

    def request_abort(self) -> None:
        self.abort_requested = True

    def check_abort(self) -> None:
        if self.abort_requested:
            raise InterruptedError('Fetch aborted by user')

    def save_results(self) -> None:
        if not self.settings.save_email_result:
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        csv_path = self.output_dir / 'mail_list_results.csv'
        with csv_path.open('w', newline='', encoding='utf-8-sig') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=['provider', 'folder', 'subject', 'from', 'date', 'has_attachments', 'attachments', 'summary', 'source_file'])
            writer.writeheader()
            for result in self.results:
                writer.writerow(result)
        print(f'Saved mail list result to {csv_path}')


class IMAPFetcher(BaseFetcher):
    def fetch(self) -> List[Dict[str, str]]:
        self._setup_logging()
        mode = 'SSL' if self.use_ssl else 'PLAIN'
        timeout = getattr(self.settings, 'connection_timeout', IMAP_TIMEOUT) or IMAP_TIMEOUT
        retries = getattr(self.settings, 'connection_retries', 2)
        print(f'Connecting IMAP to {self.server}:{self.port} ({mode}, timeout={timeout}s, retries={retries})')
        self._report_progress('Connecting IMAP...')
        mail = None
        try:
            def _imap_connect():
                if self.use_ssl:
                    ctx = ssl.create_default_context()
                    conn = imaplib.IMAP4_SSL(self.server, self.port or 993, ssl_context=ctx, timeout=timeout)
                else:
                    conn = imaplib.IMAP4(self.server, self.port or 143, timeout=timeout)
                try:
                    ctx = ssl.create_default_context()
                    conn.starttls(ctx)
                    print('IMAP STARTTLS upgraded successfully.')
                except imaplib.IMAP4.error:
                    print('IMAP STARTTLS not supported by server, continuing without encryption.')
                if self.oauth_access_token:
                    auth_string = f'user={self.email_address}\x01auth=Bearer {self.oauth_access_token}\x01\x01'
                    conn.authenticate('XOAUTH2', lambda _: auth_string.encode('utf-8'))
                    print('IMAP XOAUTH2 authentication succeeded.')
                    self._report_progress('IMAP XOAUTH2 login succeeded')
                else:
                    conn.login(self.email_address, self.password)
                    print('IMAP login succeeded.')
                    self._report_progress('IMAP login succeeded')
                return conn

            mail = connect_with_retry(_imap_connect, retries=retries, label='IMAP')
            status, folders = mail.list()
            if status != 'OK':
                raise RuntimeError('Cannot list IMAP folders. The server may not support LIST or the account may have restricted access.')
            folder_names = []
            for folder in folders:
                if not folder:
                    continue
                decoded = folder.decode('utf-8', errors='ignore')
                parts = decoded.rsplit('"/"', 2) if '"/"' in decoded else decoded.rsplit('/', 2)
                last_part = parts[-1].strip() if parts else decoded.strip()
                match = re.search(r'"([^"]+)"', last_part)
                if match:
                    folder_names.append(match.group(1))
                else:
                    name = last_part.strip('"').strip()
                    if name:
                        folder_names.append(name)
            print(f'Found {len(folder_names)} folders on server.')
            self._report_progress(f'Found {len(folder_names)} folders', 0, 0, '')
            total_fetched = 0
            for folder in folder_names:
                self.check_abort()
                if not folder_is_allowed(folder, self.config):
                    continue
                print(f'Fetching folder {folder}')
                self._report_progress(f'Searching folder: {folder}', 0, 0, folder)
                status, _ = mail.select(f'"{folder}"', readonly=True)
                if status != 'OK':
                    print(f'Could not select folder {folder}, skipping.')
                    continue
                criteria = build_search_criteria(self.settings)
                status, data = mail.search(None, criteria)
                if status != 'OK' or not data or not data[0]:
                    continue
                uids = data[0].split()
                total_in_folder = len(uids)
                print(f'Folder {folder}: {total_in_folder} messages matching criteria.')
                self._report_progress(f'Fetching {folder}', 0, total_in_folder, folder)
                thread_count = max(1, self.settings.thread_count) if len(uids) > 20 else 1
                if thread_count > 1:
                    self._fetch_imap_folder_parallel(folder, uids, thread_count, timeout)
                else:
                    self._fetch_imap_folder_sequential(mail, folder, uids)
                total_fetched += len(uids)
                self._report_progress(f'Folder {folder} done', total_fetched, total_fetched, folder)
        except imaplib.IMAP4.error as exc:
            error_msg = str(exc)
            if 'AUTHENTICATE FAILED' in error_msg.upper() or 'LOGIN FAILED' in error_msg.upper():
                raise RuntimeError(f'IMAP authentication failed: {error_msg}. Check your email/password or use an App Password if 2FA is enabled.') from exc
            if ' TOO ' in error_msg.upper() or 'LIMIT' in error_msg.upper():
                raise RuntimeError(f'IMAP rate limited: {error_msg}. Wait a few minutes and try again.') from exc
            raise RuntimeError(f'IMAP error: {error_msg}') from exc
        except socket.timeout:
            raise RuntimeError(f'IMAP connection to {self.server}:{self.port} timed out after {timeout}s. Check your server address and port, or increase the timeout in settings.') from None
        except ConnectionRefusedError:
            raise RuntimeError(f'IMAP connection refused by {self.server}:{self.port}. Check that the server address and port are correct and SSL/TLS is enabled if required.') from None
        except ConnectionResetError:
            raise RuntimeError(f'IMAP connection to {self.server}:{self.port} was reset by the server. This may indicate SSL/TLS mismatch - try toggling SSL.') from None
        except ssl.SSLError as exc:
            raise RuntimeError(f'IMAP SSL/TLS error connecting to {self.server}:{self.port}: {exc}. Try toggling SSL on/off or check the server configuration.') from exc
        except socket.gaierror:
            raise RuntimeError(f'Cannot resolve IMAP server hostname: {self.server}. Check the server address.') from None
        except TimeoutError:
            raise RuntimeError(f'IMAP connection to {self.server}:{self.port} timed out. The server may be unreachable or blocking connections.') from None
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass
            self.save_results()
            self._teardown_logging()
        return self.results


class POPFetcher(BaseFetcher):
    def fetch(self) -> List[Dict[str, str]]:
        self._setup_logging()
        timeout = getattr(self.settings, 'connection_timeout', POP3_TIMEOUT) or POP3_TIMEOUT
        retries = getattr(self.settings, 'connection_retries', 2)
        print(f'Connecting POP3 to {self.server}:{self.port} (timeout={timeout}s, retries={retries})')
        self._report_progress('Connecting POP3...')
        mail = None
        try:
            def _pop3_connect():
                if self.use_ssl:
                    ctx = ssl.create_default_context()
                    conn = poplib.POP3_SSL(self.server, self.port or 995, context=ctx, timeout=timeout)
                else:
                    conn = poplib.POP3(self.server, self.port or 110, timeout=timeout)
                try:
                    ctx = ssl.create_default_context()
                    conn.stls(context=ctx)
                    print('POP3 STARTTLS upgraded successfully.')
                except poplib.error_proto:
                    print('POP3 STARTTLS not supported by server, continuing without encryption.')
                conn.user(self.email_address)
                conn.pass_(self.password)
                print('POP3 login succeeded.')
                self._report_progress('POP3 login succeeded')
                return conn

            mail = connect_with_retry(_pop3_connect, retries=retries, label='POP3')
            count, _ = mail.stat()
            print(f'POP3: {count} messages on server.')
            self._report_progress(f'Found {count} messages', 0, count, 'POP3')
            for msg_num in range(1, count + 1):
                self.check_abort()
                if msg_num % 10 == 0 or msg_num == count:
                    self._report_progress('Fetching POP3', msg_num, count, 'POP3')
                    print(f'Progress: {msg_num}/{count}')
                try:
                    raw = b"\n".join(mail.retr(msg_num)[1])
                except poplib.error_proto as exc:
                    print(f'Error retrieving message {msg_num}: {exc}')
                    continue
                msg = email.message_from_bytes(raw)
                subject, from_, date, body = decode_message_text(msg)
                if not message_date_in_range(date, self.settings):
                    continue
                if not message_matches_keyword(subject, body, self.settings):
                    continue
                attachments: List[str] = []
                if self.settings.save_attachments:
                    for part in msg.walk():
                        if part.get_content_maintype() == 'multipart':
                            continue
                        if part.get('Content-Disposition', '').strip().startswith('attachment'):
                            attachments.extend(save_attachment(part, self.output_dir / 'attachments' / 'POP3', msg_num, self.config))
                if self.settings.save_content:
                    safe_subject = safe_filename(subject or 'message')
                    msg_file = unique_path(self.output_dir / 'messages' / 'POP3' / f'{msg_num}_{safe_subject}.eml')
                    save_email_body(msg, msg_file)
                self.results.append({
                    'provider': 'POP3',
                    'folder': 'POP3',
                    'subject': subject,
                    'from': from_,
                    'date': date,
                    'has_attachments': str(bool(attachments)),
                    'attachments': ';'.join(attachments),
                    'summary': body[:200].replace('\n', ' ').replace('\r', ' '),
                    'source_file': ''
                })
        except poplib.error_proto as exc:
            error_msg = str(exc)
            if 'auth' in error_msg.lower() or 'pass' in error_msg.lower() or 'login' in error_msg.lower():
                raise RuntimeError(f'POP3 authentication failed: {error_msg}. Check your email/password or use an App Password if 2FA is enabled.') from exc
            raise RuntimeError(f'POP3 error: {error_msg}') from exc
        except socket.timeout:
            raise RuntimeError(f'POP3 connection to {self.server}:{self.port} timed out after {timeout}s. Check your server address and port.') from None
        except ConnectionRefusedError:
            raise RuntimeError(f'POP3 connection refused by {self.server}:{self.port}. Check that the server address and port are correct.') from None
        except ssl.SSLError as exc:
            raise RuntimeError(f'POP3 SSL/TLS error connecting to {self.server}:{self.port}: {exc}. Try toggling SSL on/off.') from exc
        except socket.gaierror:
            raise RuntimeError(f'Cannot resolve POP3 server hostname: {self.server}. Check the server address.') from None
        except TimeoutError:
            raise RuntimeError(f'POP3 connection to {self.server}:{self.port} timed out. The server may be unreachable.') from None
        finally:
            if mail is not None:
                try:
                    mail.quit()
                except Exception:
                    pass
            self.save_results()
            self._teardown_logging()
        return self.results


class ExchangeFetcher(BaseFetcher):
    def connect(self):
        if not EXCHANGE_AVAILABLE:
            raise ImportError('exchangelib is required for Exchange support. Install it with pip install exchangelib')
        if self.oauth_token_data is not None:
            if not self.oauth_client_id:
                raise ValueError('OAuth Exchange login requires a client ID.')
            creds = OAuth2AuthorizationCodeCredentials(client_id=self.oauth_client_id, access_token=self.oauth_token_data)
            print('Using OAuth2 credentials for Exchange.')
        else:
            creds = Credentials(username=self.email_address, password=self.password)
            print('Using basic credentials for Exchange.')
        if self.server:
            config = Configuration(server=self.server, credentials=creds)
            account = Account(primary_smtp_address=self.email_address, credentials=creds, config=config, autodiscover=False, access_type=DELEGATE)
        else:
            print('Attempting Exchange autodiscover...')
            account = Account(primary_smtp_address=self.email_address, credentials=creds, autodiscover=True, access_type=DELEGATE)
        return account

    def fetch(self) -> List[Dict[str, str]]:
        self._setup_logging()
        self._report_progress('Connecting Exchange...')
        try:
            account = self.connect()
            self._report_progress('Exchange login succeeded')
        except Exception as exc:
            error_msg = str(exc)
            if 'auth' in error_msg.lower() or 'credential' in error_msg.lower() or '401' in error_msg:
                raise RuntimeError(f'Exchange authentication failed: {error_msg}. Check credentials or try OAuth login.') from exc
            if 'autodiscover' in error_msg.lower():
                raise RuntimeError(f'Exchange autodiscover failed: {error_msg}. Try specifying the EWS server URL manually.') from exc
            raise RuntimeError(f'Exchange connection failed: {error_msg}') from exc
        filter_kwargs = {}
        if self.settings.date_from:
            filter_kwargs['datetime_received__gte'] = datetime.datetime.combine(self.settings.date_from, datetime.time.min)
        if self.settings.date_to:
            filter_kwargs['datetime_received__lte'] = datetime.datetime.combine(self.settings.date_to, datetime.time.max)
        keyword = self.settings.keyword.strip()
        for folder in account.root.walk():
            self.check_abort()
            folder_name = folder.name
            if not folder_name or not folder_is_allowed(folder_name, self.config):
                continue
            if not hasattr(folder, 'all'):
                continue
            print(f'Fetching Exchange folder {folder_name}')
            items = folder.filter(**filter_kwargs) if filter_kwargs else folder.all()
            if keyword:
                query = None
                if self.settings.search_subject:
                    query = Q(subject__contains=keyword)
                if self.settings.search_body:
                    body_query = Q(body__contains=keyword)
                    query = body_query if query is None else query | body_query
                if query is not None:
                    items = items.filter(query)
            try:
                ordered = items.order_by('-datetime_received')
            except Exception as exc:
                print(f'Error querying folder {folder_name}: {exc}')
                continue
            idx = 0
            page_size = 500
            offset = 0
            while True:
                self.check_abort()
                try:
                    page = ordered[offset:offset + page_size]
                except Exception as exc:
                    print(f'Error paging folder {folder_name} at offset {offset}: {exc}')
                    break
                if not page:
                    break
                for item in page:
                    idx += 1
                    subject = item.subject or ''
                    from_ = str(item.author or item.sender or '')
                    date = str(item.datetime_received)
                    body = str(item.body or '')
                    attachments: List[str] = []
                    if self.settings.save_attachments and getattr(item, 'attachments', None):
                        for attachment in item.attachments:
                            if hasattr(attachment, 'content') and attachment.name:
                                if not attachment_is_allowed(attachment.name, self.config):
                                    continue
                                path = unique_path(self.output_dir / 'attachments' / safe_filename(folder_name, 'folder') / safe_filename(attachment.name, 'attachment.bin'))
                                path.parent.mkdir(parents=True, exist_ok=True)
                                with path.open('wb') as fp:
                                    fp.write(attachment.content)
                                attachments.append(str(path))
                    if self.settings.save_content:
                        safe_subject = safe_filename(subject or 'message')
                        msg_file = unique_path(self.output_dir / 'messages' / safe_filename(folder_name, 'folder') / f'{idx}_{safe_subject}.eml')
                        msg_file.parent.mkdir(parents=True, exist_ok=True)
                        with msg_file.open('w', encoding='utf-8', errors='replace') as fp:
                            fp.write(f'Subject: {subject}\nFrom: {from_}\nDate: {date}\n\n{body}')
                    self.results.append({
                        'provider': 'Exchange',
                        'folder': folder_name,
                        'subject': subject,
                        'from': from_,
                        'date': date,
                        'has_attachments': str(bool(attachments)),
                        'attachments': ';'.join(attachments),
                        'summary': body[:200].replace('\n', ' ').replace('\r', ' '),
                        'source_file': ''
                    })
                if len(page) < page_size:
                    break
                offset += page_size
        self.save_results()
        self._teardown_logging()
        return self.results

    def _fetch_imap_folder_sequential(self, mail: imaplib.IMAP4, folder: str, uids: list) -> None:
        total = len(uids)
        for idx, uid in enumerate(uids, 1):
            self.check_abort()
            if idx % 25 == 0 or idx == total:
                self._report_progress(f'Fetching {folder}', idx, total, folder)
                print(f'Progress: {idx}/{total} in folder {folder}')
            try:
                status, msg_data = mail.fetch(uid, '(BODY.PEEK[])')
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error) as exc:
                print(f'Error fetching message {uid} in folder {folder}: {exc}')
                continue
            if status != 'OK' or not msg_data or not msg_data[0]:
                continue
            raw_email = msg_data[0][1]
            self._process_imap_message(raw_email, folder, idx)

    def _fetch_imap_folder_parallel(self, folder: str, uids: list, thread_count: int, timeout: int) -> None:
        def _open_imap():
            if self.use_ssl:
                ctx = ssl.create_default_context()
                conn = imaplib.IMAP4_SSL(self.server, self.port or 993, ssl_context=ctx, timeout=timeout)
            else:
                conn = imaplib.IMAP4(self.server, self.port or 143, timeout=timeout)
                try:
                    ctx = ssl.create_default_context()
                    conn.starttls(ctx)
                except imaplib.IMAP4.error:
                    pass
            if self.oauth_access_token:
                auth_string = f'user={self.email_address}\x01auth=Bearer {self.oauth_access_token}\x01\x01'
                conn.authenticate('XOAUTH2', lambda _: auth_string.encode('utf-8'))
            else:
                conn.login(self.email_address, self.password)
            conn.select(f'"{folder}"', readonly=True)
            return conn

        def _fetch_uid(args):
            conn, uid, idx = args
            self.check_abort()
            try:
                status, msg_data = conn.fetch(uid, '(BODY.PEEK[])')
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error) as exc:
                print(f'Error fetching message {uid} in folder {folder}: {exc}')
                return None
            if status != 'OK' or not msg_data or not msg_data[0]:
                return None
            return msg_data[0][1], idx

        connections: list = []
        try:
            for _ in range(min(thread_count, len(uids))):
                try:
                    connections.append(_open_imap())
                except Exception as exc:
                    print(f'Warning: could not open parallel IMAP connection: {exc}')
                    break
            if not connections:
                print('Parallel IMAP failed: no connections. Falling back to sequential.')
                return
            work_items = [(connections[i % len(connections)], uid, idx + 1) for idx, uid in enumerate(uids)]
            with ThreadPoolExecutor(max_workers=len(connections)) as pool:
                for result in pool.map(_fetch_uid, work_items):
                    if result is None:
                        continue
                    raw_email, idx = result
                    self._process_imap_message(raw_email, folder, idx)
        finally:
            for conn in connections:
                try:
                    conn.logout()
                except Exception:
                    pass

    def _process_imap_message(self, raw_email: bytes, folder: str, idx: int) -> None:
        msg = email.message_from_bytes(raw_email)
        subject, from_, date, body = decode_message_text(msg)
        if not message_matches_keyword(subject, body, self.settings):
            return
        attachments: List[str] = []
        if self.settings.save_attachments:
            for part in msg.walk():
                if part.get_content_maintype() == 'multipart':
                    continue
                if part.get('Content-Disposition', '').strip().startswith('attachment'):
                    attachments.extend(save_attachment(part, self.output_dir / 'attachments' / safe_filename(folder, 'folder'), idx, self.config))
        if self.settings.save_content:
            safe_subject = safe_filename(subject or 'message')
            msg_file = unique_path(self.output_dir / 'messages' / safe_filename(folder, 'folder') / f'{idx}_{safe_subject}.eml')
            save_email_body(msg, msg_file)
        self.results.append({
            'provider': 'IMAP',
            'folder': folder,
            'subject': subject,
            'from': from_,
            'date': date,
            'has_attachments': str(bool(attachments)),
            'attachments': ';'.join(attachments),
            'summary': body[:200].replace('\n', ' ').replace('\r', ' '),
            'source_file': ''
        })


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description='Mail List Fetcher v2.5 compatible implementation')
    parser.add_argument('--provider', choices=['IMAP', 'POP3', 'Exchange'], required=True, help='Choose mail provider type')
    parser.add_argument('--email', required=True, help='Email address or account login')
    parser.add_argument('--password', required=True, help='Password or app password')
    parser.add_argument('--server', help='SMTP/IMAP/POP/Exchange server host or EWS endpoint')
    parser.add_argument('--port', type=int, help='Server port')
    parser.add_argument('--ssl', action='store_true', help='Use SSL/TLS for IMAP/POP3')
    parser.add_argument('--output', default='output', help='Output folder for saved content and results')
    parser.add_argument('--config', default=str(script_dir / 'Config.ini'), help='Path to configuration file')
    parser.add_argument('--servers', default=str(script_dir / 'Server_List.ini'), help='Path to server rules file')
    parser.add_argument('--settings', default=str(script_dir / 'Setting.ini'), help='Path to settings file')
    parser.add_argument('--keyword', default=None, help='Keyword to search in subject/body')
    parser.add_argument('--date-from', default=None, help='Start date YYYY-MM-DD')
    parser.add_argument('--date-to', default=None, help='End date YYYY-MM-DD')
    parser.add_argument('--search-subject', action='store_true', help='Search keyword in subject')
    parser.add_argument('--search-body', action='store_true', help='Search keyword in email body')
    parser.add_argument('--no-save-attachments', action='store_true', help='Do not download attachments')
    parser.add_argument('--no-save-content', action='store_true', help='Do not save full email content')
    parser.add_argument('--no-save-results', action='store_true', help='Do not write the CSV result file')
    parser.add_argument('--timeout', type=int, default=None, help='Connection timeout in seconds (default: 30)')
    return parser.parse_args()


def parse_date_option(value: Optional[str]) -> Optional[datetime.date]:
    if not value:
        return None
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%d-%m-%Y', '%d/%m/%Y'):
        try:
            return datetime.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f'Invalid date format: {value}')


def main() -> None:
    args = parse_args()
    config_path = Path(args.config)
    server_path = Path(args.servers)
    settings_path = Path(args.settings)
    config = IniLoader.load_config(config_path)
    rules = IniLoader.load_server_rules(server_path)
    settings = IniLoader.load_settings(settings_path)
    if args.keyword is not None:
        settings.keyword = args.keyword
    if args.date_from is not None:
        settings.date_from = parse_date_option(args.date_from)
    if args.date_to is not None:
        settings.date_to = parse_date_option(args.date_to)
    if args.search_subject:
        settings.search_subject = True
    if args.search_body:
        settings.search_body = True
    if args.no_save_attachments:
        settings.save_attachments = False
    if args.no_save_content:
        settings.save_content = False
    if args.no_save_results:
        settings.save_email_result = False
    if args.timeout is not None:
        settings.connection_timeout = args.timeout
    output_dir = Path(args.output)

    domain = args.email.split('@')[-1] if '@' in args.email else ''
    provider = args.provider
    server = args.server
    port = args.port
    use_ssl = args.ssl
    if not server and provider in ('IMAP', 'POP3'):
        server, port, encryption, _ = ServerResolver.choose_server(domain, provider, rules)
        if encryption and encryption.upper() == 'SSL':
            use_ssl = True
        elif encryption and encryption.upper() in ('TLS', 'STARTTLS'):
            use_ssl = False
    print(f'Provider={provider}, email={args.email}, server={server}:{port}, ssl={use_ssl}, timeout={settings.connection_timeout}s')

    fetcher: BaseFetcher
    if provider == 'IMAP':
        if not server:
            raise ValueError('IMAP server is required or use the server rules file for autodiscovery.')
        fetcher = IMAPFetcher(args.email, args.password, server, port, use_ssl, settings, config, output_dir)
    elif provider == 'POP3':
        if not server:
            raise ValueError('POP3 server is required or use the server rules file for autodiscovery.')
        fetcher = POPFetcher(args.email, args.password, server, port, use_ssl, settings, config, output_dir)
    else:
        if not EXCHANGE_AVAILABLE:
            print('Exchange support requires exchangelib. Install it with pip install exchangelib')
            sys.exit(1)
        fetcher = ExchangeFetcher(args.email, args.password, args.server, args.port, use_ssl, settings, config, output_dir)

    try:
        fetcher.fetch()
    except RuntimeError as exc:
        print(f'FATAL: {exc}')
        sys.exit(1)


if __name__ == '__main__':
    main()
