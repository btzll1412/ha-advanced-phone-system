#!/usr/bin/env python3
"""
AGI script to verify admin PIN from database.
Returns ADMIN_PIN variable to Asterisk dialplan.
"""

import sys
import os
import sqlite3
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

def get_db_connection():
    """Get database connection"""
    # Primary path - matches api_service.py
    db_path = '/data/database/phone_system.db'
    if not os.path.exists(db_path):
        db_path = '/data/phone_system.db'
    if not os.path.exists(db_path):
        db_path = '/config/phone_system.db'
    return sqlite3.connect(db_path)

def get_admin_pin():
    """Get the admin PIN from the database settings table.

    Returns None if the PIN is unavailable (not configured, or a DB error such
    as a transient lock during a broadcast). The caller MUST treat None as
    "deny access" - previously this fell back to "1234", which meant any error
    silently reopened the admin menu with the default PIN even after the
    operator had changed it (toll-fraud / unauthorized-access risk).
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get admin_pin from settings table
        cursor.execute("SELECT value FROM settings WHERE key = 'admin_pin'")
        result = cursor.fetchone()
        conn.close()

        if result and result[0]:
            return result[0]
        # No PIN configured -> fail closed
        return None

    except Exception as e:
        agi_verbose(f"Error getting admin PIN: {e}")
        # On error, deny access rather than falling back to a default PIN
        return None

def main():
    """Main AGI handler"""
    # Read AGI environment
    agi_read()

    agi_verbose("Fetching admin PIN from database")

    # Get the admin PIN from database
    admin_pin = get_admin_pin()

    if not admin_pin:
        agi_verbose("Admin PIN unavailable - denying admin access")
        # A non-numeric sentinel can never equal a digits-only Read() entry,
        # so the dialplan PIN check will always fail (fail closed).
        agi_set_variable("ADMIN_PIN", "__DENY__")
    else:
        agi_verbose(f"Admin PIN retrieved (length: {len(admin_pin)})")
        # Set the ADMIN_PIN variable for the dialplan
        agi_set_variable("ADMIN_PIN", admin_pin)

if __name__ == "__main__":
    main()
