import os
from urllib.parse import quote

import requests

API_BASE_URL = os.getenv("SYNC_API_BASE_URL", "").strip().rstrip("/")


def _request_json(path, params=None):
    if not API_BASE_URL:
        raise RuntimeError("SYNC_API_BASE_URL is not configured")

    response = requests.get(f"{API_BASE_URL}{path}", params=params, timeout=10)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def get_member_total_xp(rsn):
    return _request_json(f"/member/{quote(rsn)}")


def get_all_member_xp():
    return _request_json("/members") or []


def get_member_xp_gains():
    return _request_json("/member-xp-gains") or []


def get_inactive_members():
    return _request_json("/inactive-members") or []


def get_inactive_members_by_rank_and_days(rank, days):
    return _request_json("/inactive-members-by-rank", params={"rank": rank, "days": days}) or []


def get_members_by_rank(rank):
    return _request_json(f"/members-by-rank/{quote(rank)}") or []


def get_member_xp_history(rsn, days=7):
    return _request_json(f"/xp-history/{quote(rsn)}", params={"days": days}) or []


def get_private_members():
    return _request_json("/private-members") or []


def rename_member(old_rsn, new_rsn):
    """Rename a member via the API, preserving XP history."""
    try:
        url = f"{API_BASE_URL}/rename"
        response = requests.post(url, params={"old_rsn": old_rsn, "new_rsn": new_rsn}, timeout=10)
        if response.status_code == 404:
            return False, f"Member **{old_rsn}** not found in database."
        if response.status_code == 409:
            return False, f"Member **{new_rsn}** already exists in database."
        response.raise_for_status()
        data = response.json()
        return True, data['message']
    except Exception as e:
        return False, f"Error during rename: {str(e)}"