"""Stable task source kinds shared by collection and presentation."""
import re

HEAVY_KINDS = ("parity", "file_integrity", "appdata_backup_plugin", "appdata_backup_script")
BACKUP = re.compile(r'''(?:^|[\s"'])/usr/local/emhttp/plugins/(?:appdata\.backup|ca\.backup2)/scripts/'''
                    r'''(?:backup|startBackup)\.php(?=[\s"';]|$)''')


def script_kind(body: str) -> str:
    return "appdata_backup_script" if BACKUP.search(body) else ""
