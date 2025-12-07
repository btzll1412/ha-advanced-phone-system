#!/usr/bin/env python3
"""
AGI script for IVR phone system interactions.
Handles group lookups, call initiation, and scheduling.
"""

import sys
import os
import sqlite3
import json
import requests
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

def get_db_connection():
    """Get database connection"""
    db_path = '/data/phone_system.db'
    if not os.path.exists(db_path):
        db_path = '/config/phone_system.db'
    if not os.path.exists(db_path):
        # Fallback for testing
        db_path = '/tmp/phone_system.db'
    return sqlite3.connect(db_path)

def parse_phone_numbers(phone_input):
    """Parse phone numbers from various input formats into a list of strings.

    Handles:
    - Single number as string: "8455021412" -> ["8455021412"]
    - JSON array: '["8455021412", "8455021413"]' -> ["8455021412", "8455021413"]
    - Comma-separated: "8455021412,8455021413" -> ["8455021412", "8455021413"]
    - Integer (from json.loads): 8455021412 -> ["8455021412"]
    """
    if not phone_input:
        return []

    # If it's already a list, convert all items to strings
    if isinstance(phone_input, list):
        return [str(p).strip() for p in phone_input if str(p).strip()]

    # If it's an int or float, convert to string
    if isinstance(phone_input, (int, float)):
        return [str(int(phone_input))]

    # It's a string - try to parse as JSON first
    phone_str = str(phone_input).strip()

    try:
        parsed = json.loads(phone_str)
        # json.loads might return int, list, or other types
        if isinstance(parsed, list):
            return [str(p).strip() for p in parsed if str(p).strip()]
        elif isinstance(parsed, (int, float)):
            return [str(int(parsed))]
        else:
            return [str(parsed).strip()] if str(parsed).strip() else []
    except (json.JSONDecodeError, ValueError):
        # Not valid JSON - treat as comma-separated or single number
        if ',' in phone_str:
            return [p.strip() for p in phone_str.split(',') if p.strip()]
        else:
            return [phone_str] if phone_str else []

def get_groups():
    """Get all groups from database"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id, name, phone_numbers FROM groups ORDER BY id')
        groups = cursor.fetchall()
        conn.close()
        return groups
    except Exception as e:
        agi_verbose(f"Error getting groups: {e}")
        return []

def get_group_by_index(index):
    """Get a specific group by its index (1-based)"""
    try:
        groups = get_groups()
        if 0 < index <= len(groups):
            return groups[index - 1]
        return None
    except Exception as e:
        agi_verbose(f"Error getting group by index: {e}")
        return None

def initiate_call(phone_numbers, recording_file, caller_id=None):
    """Initiate outbound calls via the API.

    Args:
        phone_numbers: List of phone numbers (already parsed by parse_phone_numbers)
        recording_file: Path to the audio file to play
        caller_id: Optional caller ID to use
    """
    try:
        api_url = "http://127.0.0.1:8088/api"

        # Ensure phone_numbers is a list (should already be from parse_phone_numbers)
        if not isinstance(phone_numbers, list):
            phone_numbers = parse_phone_numbers(phone_numbers)

        if not phone_numbers:
            agi_verbose("No phone numbers to call")
            return False

        for phone in phone_numbers:
            phone_clean = str(phone).strip()
            if not phone_clean:
                continue

            payload = {
                "phone_number": phone_clean,
                "recording_file": recording_file,
                "message": f"IVR recorded message: {recording_file}"
            }
            if caller_id:
                payload["caller_id"] = caller_id

            agi_verbose(f"Calling API: {api_url}/call with phone={phone_clean}")
            response = requests.post(
                f"{api_url}/call",
                json=payload,
                timeout=30
            )
            agi_verbose(f"Call initiated to {phone_clean}: status={response.status_code}")

        return True
    except Exception as e:
        agi_verbose(f"Error initiating call: {e}")
        return False

def schedule_call(phone_numbers, recording_file, schedule_time, name="IVR Scheduled Call", caller_id=None):
    """Schedule a call for later"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Ensure phone_numbers is stored as JSON array
        if isinstance(phone_numbers, str):
            phone_numbers = [phone_numbers]
        phone_json = json.dumps(phone_numbers)

        cursor.execute('''
            INSERT INTO scheduled_calls
            (name, phone_number, message, recording_file, caller_id, scheduled_time, repeat_type, enabled, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            name,
            phone_json,
            f"IVR scheduled message: {recording_file}",
            recording_file,
            caller_id or '',
            schedule_time,
            'none',
            1,
            'pending',
            datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        ))

        conn.commit()
        conn.close()
        agi_verbose(f"Call scheduled for {schedule_time}")
        return True
    except Exception as e:
        agi_verbose(f"Error scheduling call: {e}")
        return False

def main():
    """Main AGI handler"""
    # Read AGI environment
    agi_read()

    # Get the action from command line args
    if len(sys.argv) < 2:
        agi_verbose("No action specified")
        sys.exit(1)

    action = sys.argv[1]

    if action == "get_groups":
        # Get all groups and set variables for each
        groups = get_groups()
        agi_set_variable("GROUP_COUNT", str(len(groups)))

        for i, group in enumerate(groups, 1):
            group_id, name, phone_numbers = group
            agi_set_variable(f"GROUP_{i}_ID", str(group_id))
            agi_set_variable(f"GROUP_{i}_NAME", name)
            agi_set_variable(f"GROUP_{i}_PHONES", phone_numbers)

        agi_verbose(f"Found {len(groups)} groups")

    elif action == "get_group":
        # Get a specific group by index
        if len(sys.argv) < 3:
            agi_verbose("No group index specified")
            sys.exit(1)

        index = int(sys.argv[2])
        group = get_group_by_index(index)

        if group:
            group_id, name, phone_numbers = group
            agi_set_variable("SELECTED_GROUP_ID", str(group_id))
            agi_set_variable("SELECTED_GROUP_NAME", name)
            agi_set_variable("SELECTED_GROUP_PHONES", phone_numbers)
            agi_set_variable("GROUP_FOUND", "1")
            agi_verbose(f"Found group: {name}")
        else:
            agi_set_variable("GROUP_FOUND", "0")
            agi_verbose(f"Group not found at index {index}")

    elif action == "initiate_call":
        # Initiate call(s) immediately
        phone_numbers = agi_get_variable("IVR_PHONE_NUMBERS")
        recording_file = agi_get_variable("IVR_RECORDING_FILE")
        caller_id = agi_get_variable("IVR_CALLER_ID")

        # Parse phone numbers - ensure we always get a list of strings
        phones = parse_phone_numbers(phone_numbers)

        agi_verbose(f"Parsed phones: {phones}, recording: {recording_file}")
        success = initiate_call(phones, recording_file, caller_id)
        agi_set_variable("CALL_INITIATED", "1" if success else "0")

    elif action == "schedule_call":
        # Schedule a call for later
        phone_numbers = agi_get_variable("IVR_PHONE_NUMBERS")
        recording_file = agi_get_variable("IVR_RECORDING_FILE")
        schedule_time = agi_get_variable("IVR_SCHEDULE_TIME")
        caller_id = agi_get_variable("IVR_CALLER_ID")
        call_name = agi_get_variable("IVR_CALL_NAME") or "IVR Scheduled Call"

        # Parse phone numbers - ensure we always get a list of strings
        phones = parse_phone_numbers(phone_numbers)

        success = schedule_call(phones, recording_file, schedule_time, call_name, caller_id)
        agi_set_variable("CALL_SCHEDULED", "1" if success else "0")

    elif action == "format_schedule_time":
        # Convert DTMF input (MMDDYYYYHHMM) to datetime format
        dtmf_input = agi_get_variable("IVR_DTMF_DATETIME")

        if len(dtmf_input) >= 12:
            try:
                month = dtmf_input[0:2]
                day = dtmf_input[2:4]
                year = dtmf_input[4:8]
                hour = dtmf_input[8:10]
                minute = dtmf_input[10:12]

                # Validate and format
                dt = datetime(int(year), int(month), int(day), int(hour), int(minute))
                formatted = dt.strftime("%Y-%m-%dT%H:%M")
                agi_set_variable("IVR_SCHEDULE_TIME", formatted)
                agi_set_variable("DATETIME_VALID", "1")
                agi_verbose(f"Formatted datetime: {formatted}")
            except Exception as e:
                agi_set_variable("DATETIME_VALID", "0")
                agi_verbose(f"Invalid datetime: {e}")
        else:
            agi_set_variable("DATETIME_VALID", "0")
            agi_verbose("Datetime input too short")

    else:
        agi_verbose(f"Unknown action: {action}")
        sys.exit(1)

if __name__ == "__main__":
    main()
