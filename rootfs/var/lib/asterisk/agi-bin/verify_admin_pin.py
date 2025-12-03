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
    db_path = '/data/phone_system.db'
    if not os.path.exists(db_path):
        db_path = '/config/phone_system.db'
    if not os.path.exists(db_path):
        db_path = '/tmp/phone_system.db'
    return sqlite3.connect(db_path)

def get_admin_pin():
    """Get the admin PIN from the database settings table"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get admin_pin from settings table
        cursor.execute("SELECT value FROM settings WHERE key = 'admin_pin'")
        result = cursor.fetchone()
        conn.close()

        if result:
            return result[0]
        else:
            # Return default PIN if not found
            return "1234"

    except Exception as e:
        agi_verbose(f"Error getting admin PIN: {e}")
        # Return default PIN on error
        return "1234"

def main():
    """Main AGI handler"""
    # Read AGI environment
    agi_read()

    agi_verbose("Fetching admin PIN from database")

    # Get the admin PIN from database
    admin_pin = get_admin_pin()

    agi_verbose(f"Admin PIN retrieved (length: {len(admin_pin)})")

    # Set the ADMIN_PIN variable for the dialplan
    agi_set_variable("ADMIN_PIN", admin_pin)

if __name__ == "__main__":
    main()
