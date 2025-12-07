#!/usr/bin/env python3
"""
AGI script for broadcast callback lookup.
Finds all broadcast recordings associated with a caller ID.
Returns them in order from newest to oldest.
"""

import sys
import os
import sqlite3
from datetime import datetime
import re

# AGI environment variables
agi_env = {}

def agi_read():
    """Read AGI environment variables"""
    while True:
        line = sys.stdin.readline().strip()
        if not line:
            break
        if ':' in line:
            key, value = line.split(':', 1)
            agi_env[key.strip()] = value.strip()

def agi_command(cmd):
    """Send AGI command and get response"""
    sys.stdout.write(cmd + '\n')
    sys.stdout.flush()
    return sys.stdin.readline().strip()

def agi_verbose(message, level=1):
    """Log a verbose message"""
    agi_command(f'VERBOSE "{message}" {level}')

def agi_set_variable(name, value):
    """Set an Asterisk channel variable"""
    agi_command(f'SET VARIABLE {name} "{value}"')

def agi_get_variable(name):
    """Get an Asterisk channel variable"""
    result = agi_command(f'GET VARIABLE {name}')
    # Result format: 200 result=1 (value) or 200 result=0
    if 'result=1' in result:
        match = re.search(r'\((.+)\)', result)
        if match:
            return match.group(1)
    return ''

def normalize_phone_number(phone):
    """Normalize phone number by removing non-digits"""
    if not phone:
        return ""
    return ''.join(c for c in phone if c.isdigit())

def get_db_connection():
    """Get database connection"""
    # Primary path - matches api_service.py
    db_path = '/data/database/phone_system.db'
    if not os.path.exists(db_path):
        db_path = '/data/phone_system.db'
    if not os.path.exists(db_path):
        db_path = '/config/phone_system.db'

    conn = sqlite3.connect(db_path)

    # Ensure the broadcast_callbacks table exists
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS broadcast_callbacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            caller_id TEXT NOT NULL,
            audio_file TEXT NOT NULL,
            broadcast_id TEXT NOT NULL,
            broadcast_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            enabled INTEGER DEFAULT 1
        )
    ''')
    conn.commit()

    return conn

def lookup_callbacks(called_number):
    """
    Look up all broadcast callbacks for a called number.
    Returns list of audio files from newest to oldest.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Normalize the called number for matching
        normalized = normalize_phone_number(called_number)

        # Get current time for expiration check
        now = datetime.now().isoformat()

        # Look up all enabled, non-expired callbacks for this caller ID
        # Match by the last 10 digits to handle different formats
        cursor.execute('''
            SELECT audio_file, broadcast_name, created_at
            FROM broadcast_callbacks
            WHERE enabled = 1
              AND (expires_at IS NULL OR expires_at > ?)
              AND (
                  caller_id = ?
                  OR caller_id LIKE ?
                  OR ? LIKE '%' || SUBSTR(REPLACE(REPLACE(caller_id, '-', ''), ' ', ''), -10)
              )
            ORDER BY created_at DESC
        ''', (now, called_number, f'%{normalized[-10:]}' if len(normalized) >= 10 else normalized, normalized))

        results = cursor.fetchall()
        conn.close()

        return results

    except Exception as e:
        agi_verbose(f"Error looking up callbacks: {e}")
        return []

def main():
    """Main AGI handler"""
    # Read AGI environment
    agi_read()

    # Get the CALLER's number (who is calling in) - this is who we look up callbacks for
    # When a broadcast recipient calls back, we want to play them the message
    caller_number = agi_env.get('agi_callerid', '')

    # Also try from CALLERID(num) variable
    if not caller_number:
        caller_number = agi_get_variable('CALLERID(num)')

    # Get the called DID for logging purposes
    called_did = agi_env.get('agi_extension', '') or agi_get_variable('EXTEN')

    agi_verbose(f"Callback lookup - caller: {caller_number}, DID: {called_did}")

    if not caller_number:
        agi_verbose("No caller number available")
        agi_set_variable("CALLBACK_COUNT", "0")
        return

    # Look up callbacks by caller's number (the recipient of a broadcast calling back)
    callbacks = lookup_callbacks(caller_number)

    if not callbacks:
        agi_verbose(f"No callbacks found for {caller_number}")
        agi_set_variable("CALLBACK_COUNT", "0")
        return

    agi_verbose(f"Found {len(callbacks)} callback recordings")

    # Set the count
    agi_set_variable("CALLBACK_COUNT", str(len(callbacks)))

    # Set each audio file as a variable (CALLBACK_1, CALLBACK_2, etc.)
    for i, (audio_file, broadcast_name, created_at) in enumerate(callbacks, 1):
        agi_set_variable(f"CALLBACK_{i}", audio_file)
        agi_set_variable(f"CALLBACK_{i}_NAME", broadcast_name or "Unknown")
        agi_verbose(f"  {i}. {broadcast_name}: {audio_file}")

if __name__ == "__main__":
    main()
