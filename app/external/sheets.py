import os
import uuid
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

VALID_ITEMS_SHEET = 'Discord bot item db'
EVENT_LOG_SHEET = 'Discord bot log'
SCOREBOARD_SHEET = 'Discord bot scoreboard'
SIGNUPS_SHEET = 'Discord bot signups'

EVENT_LOG_HEADERS = ['Drop ID', 'Timestamp', 'User', 'Item', 'Team', 'Archive URL']
SIGNUPS_HEADERS = ['Discord Username', 'In-Game Name', 'Avg Daily Playtime']


class DuplicateSignupError(Exception):
    pass


def build_log_row(drop_id: str, timestamp: str, user: str, item_name: str, team: str, archive_url: str | None = None) -> list[str]:
    return [drop_id, timestamp, user, item_name, team, archive_url or ""]


def ensure_event_log_headers(sheet) -> None:
    if not sheet.get_all_values():
        sheet.append_row(EVENT_LOG_HEADERS)


def _get_credentials() -> Credentials:
    account_info = {
        "type": "service_account",
        "project_id": os.environ["GOOGLE_PROJECT_ID"],
        "client_email": os.environ["GOOGLE_CLIENT_EMAIL"],
        "private_key": os.environ["GOOGLE_PRIVATE_KEY"].replace('\\n', '\n'),
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    return Credentials.from_service_account_info(account_info, scopes=SCOPES)


def get_sheet(sheet_name: str):
    client = gspread.authorize(_get_credentials())
    spreadsheet_id = os.environ["GOOGLE_SPREADSHEET_ID"]
    return client.open_by_key(spreadsheet_id).worksheet(sheet_name)


def log_item(item_name: str, user: str, timestamp: str, team: str, archive_url: str | None = None):
    drop_id = str(uuid.uuid4())[:8]
    sheet = get_sheet(EVENT_LOG_SHEET)
    ensure_event_log_headers(sheet)
    sheet.append_row(build_log_row(drop_id, timestamp, user, item_name, team, archive_url))
    return drop_id


def undo_item(drop_id: str):
    sheet = get_sheet(EVENT_LOG_SHEET)
    rows = sheet.get_all_values()
    for i, row in enumerate(rows):
        if row and row[0] == drop_id:
            sheet.delete_rows(i + 1)
            return True
    return False


def get_scoreboard_scores() -> dict[int, str]:
    """
    Fetch scoreboard scores from the scoreboard sheet.
    Returns a dict mapping team number (1-5) to their score.
    """
    sheet = get_sheet(SCOREBOARD_SHEET)
    scores = {}

    try:
        cells = sheet.range('B1:B5')
        for i, cell in enumerate(cells, start=1):
            scores[i] = cell.value if cell.value else "0"
    except Exception:
        for i in range(1, 6):
            scores[i] = "0"

    return scores


def ensure_signups_headers(sheet) -> None:
    if not sheet.get_all_values():
        sheet.append_row(SIGNUPS_HEADERS)


def signup_exists(sheet, discord_username: str) -> bool:
    normalized_username = discord_username.strip().casefold()
    rows = sheet.get_all_values()
    for row in rows:
        if not row:
            continue
        first_col = row[0].strip()
        if first_col.casefold() == SIGNUPS_HEADERS[0].casefold():
            continue
        if first_col.casefold() == normalized_username:
            return True
    return False


def user_has_signup(discord_username: str) -> bool:
    sheet = get_sheet(SIGNUPS_SHEET)
    ensure_signups_headers(sheet)
    return signup_exists(sheet, discord_username)


def log_signup(discord_username: str, in_game_name: str, daily_playtime: str) -> None:
    sheet = get_sheet(SIGNUPS_SHEET)
    ensure_signups_headers(sheet)
    if signup_exists(sheet, discord_username):
        raise DuplicateSignupError("You are already signed up for this event.")
    sheet.append_row([discord_username, in_game_name, daily_playtime])


def get_signup_names() -> list[str]:
    sheet = get_sheet(SIGNUPS_SHEET)
    ensure_signups_headers(sheet)
    names = []
    for row in sheet.get_all_values()[1:]:
        if len(row) > 1 and row[1].strip():
            names.append(row[1].strip())
    return names


class EventItemCache:
    def __init__(self):
        self.items: list[str] = []
        self.last_sync: datetime | None = None

    def sync_items(self):
        sheet = get_sheet(VALID_ITEMS_SHEET)
        self.items = [row[0] for row in sheet.get_all_values() if row and row[0]]
        self.last_sync = datetime.utcnow()

    def get_items(self) -> list[str]:
        return self.items


item_cache = EventItemCache()
