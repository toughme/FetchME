# Mail List Fetcher v2.5 (Private Python Version)

A private Python implementation inspired by Mail List Fetcher v2.5.
This version supports:
- IMAP
- POP3
- Microsoft Exchange via `exchangelib`
- folder whitelist/blacklist filtering
- attachment extension filtering
- keyword and date range search
- save matching messages and attachments
- CSV result export

## Files
- `mail_list_fetcher.py` — main fetcher script
- `gui_mail_list_fetcher.py` — desktop GUI launcher
- `Config.ini` — folder and attachment filter settings
- `Server_List.ini` — server autodiscovery rules
- `Setting.ini` — fetcher options
- `requirements.txt` — Python dependency list

## Requirements
Install Python 3.12+ and dependencies:

```cmd
pip install -r MailListFetcher_v2_5/requirements.txt
```

The OAuth IMAP flow uses an embedded browser window provided by `pywebview`, so the dependency install step matters even if the app already launches without it.

## Usage

```cmd
python MailListFetcher_v2_5/mail_list_fetcher.py --provider IMAP --email user@example.com --password secret --server imap.example.com --ssl
```

Example with server autodiscovery from `Server_List.ini`:

```cmd
python MailListFetcher_v2_5/mail_list_fetcher.py --provider IMAP --email user@gmail.com --password secret --ssl
```

Example using Exchange:

```cmd
python MailListFetcher_v2_5/mail_list_fetcher.py --provider Exchange --email user@domain.com --password secret
```

## GUI Mode

Launch the desktop GUI with:

```cmd
python MailListFetcher_v2_5/gui_mail_list_fetcher.py
```

The GUI provides the same IMAP, POP3, and Exchange flow, plus folder/attachment filters, keyword/date search, and output controls.

Selecting `Login with OAuth IMAP` opens the Microsoft sign-in page inside the app, waits for the callback on a local loopback URL, closes the auth window automatically, and starts the IMAP fetch as soon as token exchange completes.

## Output
Saved files appear in the `output` folder by default.
Results are written to `output/mail_list_results.csv`.
