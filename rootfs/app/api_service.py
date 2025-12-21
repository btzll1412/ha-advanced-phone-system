#!/usr/bin/env python3
"""
Advanced Phone System - Main API Service
Handles calls, broadcasts, and Home Assistant integration
"""
import asyncio
import csv
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import subprocess
import uuid
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import List, Dict, Optional

import aiofiles
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, UploadFile, File, Response, Cookie
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from gtts import gTTS
from pydantic import BaseModel

# ============================================================================
# AUTHENTICATION CONFIGURATION
# ============================================================================
WEB_AUTH_ENABLED = os.environ.get('WEB_AUTH_ENABLED', 'false').lower() == 'true'
WEB_AUTH_USERNAME = os.environ.get('WEB_AUTH_USERNAME', 'admin')
WEB_AUTH_PASSWORD = os.environ.get('WEB_AUTH_PASSWORD', 'admin')

# Session storage (in-memory)
# Format: {token: {"expires": datetime, "duration_seconds": int}}
active_sessions: Dict[str, dict] = {}
SESSION_DURATION = timedelta(hours=24)
REMEMBER_ME_DURATION = timedelta(days=30)

class CallDirection(str, Enum):
    INTERNAL = "internal"
    OUTBOUND = "outbound"
    INBOUND = "inbound"


def detect_call_type(number: str) -> tuple:
    """
    Detect if number is internal or external
    Returns: (CallDirection, formatted_number)
    """
    # Remove non-digits
    digits = ''.join(filter(str.isdigit, number))
    
    # Internal: 2-4 digits
    if len(digits) <= 4:
        logger.info(f"🏠 INTERNAL call detected: {digits}")
        return (CallDirection.INTERNAL, digits)
    
    # External: 10+ digits
    elif len(digits) >= 10:
        # Format to E.164
        if len(digits) == 10:
            formatted = f'+1{digits}'
        elif len(digits) == 11 and digits[0] == '1':
            formatted = f'+{digits}'
        else:
            formatted = f'+{digits}'
        
        logger.info(f"📤 OUTBOUND call detected: {formatted}")
        return (CallDirection.OUTBOUND, formatted)
    
    else:
        raise ValueError(f"Invalid number: {number}")


def get_channel_string(direction: CallDirection, number: str) -> str:
    """Generate Asterisk channel string for chan_sip"""
    if direction == CallDirection.INTERNAL:
        return f"SIP/{number}"
    else:
        # Remove + for trunk routing
        clean = number.lstrip('+')
        return f"SIP/trunk_main/{clean}"

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI
app = FastAPI(title="Advanced Phone System API", version="1.1.0")


# ============================================================================
# AUTHENTICATION FUNCTIONS
# ============================================================================

def is_valid_session(session_token: str) -> bool:
    """Check if a session token is valid and not expired"""
    if not session_token or session_token not in active_sessions:
        return False

    session_data = active_sessions[session_token]
    if datetime.now() > session_data["expires"]:
        # Session expired, remove it
        del active_sessions[session_token]
        return False

    return True


def create_session(remember_me: bool = False) -> tuple:
    """Create a new session token. Returns (token, duration_seconds)"""
    token = secrets.token_urlsafe(32)
    duration = REMEMBER_ME_DURATION if remember_me else SESSION_DURATION
    duration_seconds = int(duration.total_seconds())

    active_sessions[token] = {
        "expires": datetime.now() + duration,
        "duration_seconds": duration_seconds
    }

    return token, duration_seconds


def cleanup_expired_sessions():
    """Remove expired sessions"""
    now = datetime.now()
    expired = [token for token, data in active_sessions.items() if now > data["expires"]]
    for token in expired:
        del active_sessions[token]


def check_auth(request: Request) -> bool:
    """Check if request is authenticated"""
    if not WEB_AUTH_ENABLED:
        return True

    session_token = request.cookies.get("session_token")
    return is_valid_session(session_token)


# ============================================================================
# AUTHENTICATION ENDPOINTS
# ============================================================================

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Serve the login page"""
    # If auth is disabled, redirect to main UI
    if not WEB_AUTH_ENABLED:
        return RedirectResponse(url="/", status_code=302)

    # If already logged in, redirect to main UI
    if check_auth(request):
        return RedirectResponse(url="/", status_code=302)

    return FileResponse("/app/web/login.html")


@app.post("/api/auth/login")
async def do_login(request: Request, response: Response):
    """Process login request"""
    if not WEB_AUTH_ENABLED:
        return {"status": "success", "message": "Authentication disabled"}

    try:
        body = await request.json()
        username = body.get("username", "")
        password = body.get("password", "")
        remember_me = body.get("remember_me", False)

        if username == WEB_AUTH_USERNAME and password == WEB_AUTH_PASSWORD:
            # Create session with appropriate duration
            session_token, duration_seconds = create_session(remember_me=remember_me)
            response.set_cookie(
                key="session_token",
                value=session_token,
                httponly=True,
                max_age=duration_seconds,
                samesite="strict"
            )
            duration_desc = "30 days" if remember_me else "24 hours"
            logger.info(f"User '{username}' logged in successfully (session: {duration_desc})")
            return {"status": "success"}
        else:
            logger.warning(f"Failed login attempt for user '{username}'")
            raise HTTPException(status_code=401, detail="Invalid username or password")

    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid request body")


@app.post("/api/auth/logout")
async def do_logout(request: Request, response: Response):
    """Process logout request"""
    session_token = request.cookies.get("session_token")
    if session_token and session_token in active_sessions:
        del active_sessions[session_token]

    response.delete_cookie("session_token")
    return {"status": "success"}


@app.get("/api/auth/status")
async def auth_status(request: Request):
    """Check authentication status"""
    return {
        "authenticated": check_auth(request),
        "auth_enabled": WEB_AUTH_ENABLED
    }


@app.get("/", response_class=HTMLResponse)
async def serve_web_ui(request: Request):
    """Serve the web UI"""
    # If auth is enabled and not logged in, redirect to login
    if WEB_AUTH_ENABLED and not check_auth(request):
        return RedirectResponse(url="/login", status_code=302)

    return FileResponse("/app/web/index.html")


@app.get("/api")
async def api_info():
    """API information endpoint"""
    return {
        "service": "Advanced Phone System API",
        "version": "1.1.0",
        "status": "running",
        "auth_enabled": WEB_AUTH_ENABLED
    }


# Middleware to check authentication for API endpoints
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Check authentication for protected endpoints"""
    path = request.url.path

    # Public endpoints that don't require authentication
    public_paths = [
        "/login",
        "/api/auth/login",
        "/api/auth/status",
        "/api/auth/logout",
        "/api",  # Info endpoint
    ]

    # Static files and assets
    if path.startswith("/static") or path.endswith(".css") or path.endswith(".js"):
        return await call_next(request)

    # Check if path is public
    if any(path == p or path.startswith(p + "/") for p in public_paths):
        return await call_next(request)

    # If auth is enabled and this is an API call, check authentication
    if WEB_AUTH_ENABLED and path.startswith("/api/"):
        if not check_auth(request):
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"}
            )

    # Cleanup expired sessions periodically
    if len(active_sessions) > 100:
        cleanup_expired_sessions()

    return await call_next(request)


# Configuration paths
CONFIG_FILE = "/data/options.json"
DB_PATH = "/data/database/phone_system.db"
ASTERISK_SPOOL = "/var/spool/asterisk/outgoing"
RECORDINGS_PATH = "/data/recordings"
ASTERISK_SOUNDS = "/var/lib/asterisk/sounds/custom"
# CDR file path
CDR_FILE = "/var/log/asterisk/cdr-csv/Master.csv"


# Home Assistant API
HA_URL = os.getenv("SUPERVISOR_TOKEN") and "http://supervisor/core/api" or "http://homeassistant:8123/api"
HA_TOKEN = os.getenv("SUPERVISOR_TOKEN", "")

# ============================================================================
# DATABASE SETUP
# ============================================================================

# Database connection helper with timeout
def get_db_connection():
    """Get database connection with timeout and WAL mode"""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_database():
    """Initialize SQLite database"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Call history table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS call_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id TEXT UNIQUE NOT NULL,
            phone_number TEXT NOT NULL,
            direction TEXT NOT NULL,
            status TEXT NOT NULL,
            audio_file TEXT,
            caller_id TEXT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP,
            duration INTEGER,
            group_name TEXT,
            broadcast_id TEXT
        )
    ''')
    
    # Broadcasts table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            broadcast_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            total_numbers INTEGER NOT NULL,
            completed INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            in_progress INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            caller_id TEXT,
            audio_file TEXT
        )
    ''')

    # Broadcast callbacks table - tracks caller IDs that should trigger playback
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

    # System settings table (key-value store)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Insert default admin PIN if not exists
    cursor.execute('''
        INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_pin', '1234')
    ''')

    # DIDs (Direct Inward Dialing numbers) table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS dids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT UNIQUE NOT NULL,
            name TEXT,
            description TEXT,
            is_default INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Contact groups table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS contact_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            caller_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Group members table WITH contact_name column
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS group_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            phone_number TEXT NOT NULL,
            contact_name TEXT,
            FOREIGN KEY (group_id) REFERENCES contact_groups(id) ON DELETE CASCADE
        )
    ''')
    
    # Call Detail Records table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS call_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uniqueid TEXT UNIQUE,
            caller_id TEXT,
            source_number TEXT,
            destination_number TEXT,
            context TEXT,
            channel TEXT,
            destination_channel TEXT,
            extension TEXT,
            call_type TEXT,
            direction TEXT,
            start_time TEXT,
            answer_time TEXT,
            end_time TEXT,
            duration INTEGER,
            billsec INTEGER,
            disposition TEXT,
            broadcast_id TEXT,
            recording_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Scheduled calls table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scheduled_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone_number TEXT,
            group_name TEXT,
            message TEXT,
            tts_text TEXT,
            recording_file TEXT,
            caller_id TEXT,
            scheduled_time TEXT NOT NULL,
            repeat_type TEXT DEFAULT 'none',
            repeat_days TEXT,
            enabled INTEGER DEFAULT 1,
            last_run TEXT,
            next_run TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            is_broadcast INTEGER DEFAULT 0,
            concurrent_calls INTEGER DEFAULT 5,
            pre_message_delay INTEGER DEFAULT 1,
            max_ring_time INTEGER DEFAULT 45
        )
    ''')

    conn.commit()
    conn.close()
    logger.info("✓ Database initialized")

# CDR monitoring functions
async def monitor_cdr():
    """Monitor Asterisk CDR file and import new call records"""
    last_position = 0
    first_run = True
    
    while True:
        try:
            if os.path.exists(CDR_FILE):
                with open(CDR_FILE, 'r') as f:
                    if first_run:
                        f.seek(0)
                        first_run = False
                    else:
                        f.seek(last_position)
                    
                    reader = csv.reader(f)
                    for row in reader:
                        if len(row) >= 17:
                            await process_cdr_record(row)
                    
                    last_position = f.tell()
            
            await asyncio.sleep(2)
            
        except Exception as e:
            logger.error(f"CDR monitoring error: {e}")
            await asyncio.sleep(5)


async def process_cdr_record(row):
    """Process a single CDR record"""
    try:
        # Skip if not enough columns
        if len(row) < 17:
            return
        
        # Parse CDR with CORRECT column positions (18 total columns)
        # 0: accountcode (empty)
        # 1: src (source number)
        # 2: dst (destination - usually 's' for outbound)
        # 3: dcontext (destination context)
        # 4: clid (caller ID with format)
        # 5: channel
        # 6: dstchannel (destination channel)
        # 7: lastapp
        # 8: lastdata
        # 9: start (call start time)
        # 10: answer (answer time)
        # 11: end (end time)
        # 12: duration (total duration in seconds)
        # 13: billsec (billable seconds - talk time)
        # 14: disposition (ANSWERED/NO ANSWER/BUSY/FAILED)
        # 15: amaflags
        # 16: uniqueid
        # 17: userfield (optional)
        
        source = row[1]
        dest = row[2]
        context = row[3]
        clid = row[4]
        channel = row[5]
        dst_channel = row[6]
        # DEBUG: Log the channel to see what we're working with
        logger.info(f"🔍 DEBUG - channel: {channel}, context: {context}")
        lastapp = row[7]
        lastdata = row[8]
        start = row[9]
        answer = row[10]
        end = row[11]
        duration = row[12]
        billsec = row[13]
        disposition = row[14]
        uniqueid = row[16]
        userfield = row[17] if len(row) > 17 else ""

        # Skip if uniqueid is empty
        if not uniqueid or uniqueid == '':
            return

        # Extract caller_id from clid field
        caller_id = ""
        if "<" in clid and ">" in clid:
            caller_id = clid.split("<")[1].split(">")[0]

        # Parse userfield for enhanced call info (format: TYPE|caller|destination|context_info)
        # This gives us accurate from/to data that may not be in standard CDR fields
        userfield_type = ""
        userfield_caller = ""
        userfield_dest = ""
        userfield_context = ""
        amd_result = ""  # Answering Machine Detection result: HUMAN, MACHINE, NOTSURE, HANGUP
        if userfield and "|" in userfield:
            parts = userfield.split("|")
            if len(parts) >= 4:
                userfield_type = parts[0]
                userfield_caller = parts[1]
                userfield_dest = parts[2]
                userfield_context = parts[3]
                if len(parts) >= 5:
                    amd_result = parts[4]
                logger.info(f"📋 Parsed userfield: type={userfield_type}, caller={userfield_caller}, dest={userfield_dest}, ctx={userfield_context}, amd={amd_result}")

        # Determine call type and direction
        call_type = "unknown"
        direction = "unknown"
        extension = None

        # First, check userfield_type - this gives us the TRUE call direction
        # even if the call ended in a different context (e.g., inbound call that went through IVR)
        if userfield_type == "INBOUND":
            call_type = "inbound"
            direction = "inbound"
            if userfield_caller:
                source = userfield_caller
                caller_id = userfield_caller
            if userfield_dest:
                dest = userfield_dest  # The DID that was called
            extension = userfield_context  # e.g., "callback"
            logger.info(f"📞 Inbound call (from userfield): {source} → DID:{dest}")
        elif userfield_type == "OUTBOUND":
            call_type = "outbound"
            direction = "outbound"
            if userfield_caller:
                caller_id = userfield_caller
                source = "System"
            if userfield_dest:
                dest = userfield_dest
            extension = userfield_context  # e.g., "broadcast"
            logger.info(f"📤 Outbound call (from userfield): {caller_id} → {dest}")

        # If no userfield, determine based on context
        elif context == "outbound-playback":
            call_type = "outbound"
            direction = "outbound"
            logger.info(f"🔍 Processing outbound-playback: channel={channel}")

            # First try userfield for accurate data
            if userfield_type == "OUTBOUND":
                if userfield_caller:
                    caller_id = userfield_caller
                    source = "System"
                if userfield_dest:
                    dest = userfield_dest
                extension = userfield_context  # e.g., "broadcast"
                logger.info(f"📤 Outbound call (from userfield): {caller_id} → {dest}")
            # Fallback: Extract actual destination from channel: SIP/trunk_main/18455021412
            elif "trunk_main/" in channel:
                logger.info(f"✅ Found trunk_main in channel!")
                try:
                    # Get the phone number after trunk_main/
                    actual_dest = channel.split("trunk_main/")[1].split("-")[0]
                    dest = actual_dest  # The number we called (18455021412)
                    source = caller_id if caller_id else "System"  # Who made the call
                    logger.info(f"🔍 Extracted from channel: dest={dest}, source={source}, caller_id={caller_id}")
                except Exception as e:
                    logger.error(f"Failed to parse channel: {channel}, error: {e}")
            elif source and source != "s":
                # Fallback to old method if channel doesn't have trunk_main
                actual_dest = source
                source = "System"
                dest = actual_dest
        elif context == "internal":
            # Check if destination is an internal extension (3 digits starting with 1)
            # Check if destination is an internal extension (3 digits starting with 1)
            if dest.startswith("1") and len(dest) == 3:
                call_type = "internal"
                direction = "internal"
            elif dest == "999":
                call_type = "recording_system"
                direction = "internal"
            # If destination is an external number (10+ digits), it's an outbound call
            elif len(dest) >= 10:
                call_type = "outbound"
                direction = "outbound"
            else:
                call_type = "outbound"
                direction = "outbound"
        elif context == "inbound":
            # Fallback if userfield wasn't set
            call_type = "inbound"
            direction = "inbound"
        elif context == "broadcast":
            call_type = "broadcast"
            direction = "outbound"
        
        # Parse extension from dst_channel
        if "SIP/" in dst_channel:
            try:
                extension = dst_channel.split("/")[1].split("-")[0]
            except (IndexError, AttributeError):
                pass

        # Parse extension from channel if not found
        if not extension and "SIP/" in channel:
            try:
                extension = channel.split("/")[1].split("-")[0]
                # Check if it looks like an extension (3 digits starting with 1)
                if not (extension.startswith("1") and len(extension) == 3):
                    if extension == "trunk_main":
                        extension = None  # This is the trunk, not an extension
            except (IndexError, AttributeError):
                pass

        # Convert to integers
        try:
            duration_int = int(float(duration))
            billsec_int = int(float(billsec))
        except (ValueError, TypeError):
            duration_int = 0
            billsec_int = 0

        # ✅ FIX: Get correct phone number from call_history if this is an outbound call
        if context == "outbound-playback":
            # Try to find the call in call_history by looking for recent calls
            try:
                conn_temp = sqlite3.connect(DB_PATH)
                c_temp = conn_temp.cursor()
        
                # First, let's see what's actually in the table
                c_temp.execute('''
                    SELECT call_id, phone_number, caller_id, status, started_at 
                    FROM call_history 
                    WHERE started_at >= datetime('now', '-120 seconds')
                    ORDER BY started_at DESC
                ''')
                all_recent = c_temp.fetchall()
                logger.info(f"🔍 Recent calls in call_history: {len(all_recent)} found")
                for call in all_recent:
                    logger.info(f"   - {call[0][:8]}... → {call[1]} (status: {call[3]}, time: {call[4]})")
        
                # Now try to get the most recent one
                c_temp.execute('''
                    SELECT phone_number, caller_id 
                    FROM call_history 
                    ORDER BY started_at DESC
                    LIMIT 1
                ''')
                history_row = c_temp.fetchone()
                conn_temp.close()
        
                if history_row:
                    dest = history_row[0]  # Correct phone number
                    caller_id = history_row[1] if history_row[1] else caller_id  # Correct caller ID
                    source = "System"
                    logger.info(f"✅ Retrieved correct data from call_history: dest={dest}, caller_id={caller_id}")
                else:
                    logger.warning(f"⚠️ No calls found in call_history at all!")
            except Exception as e:
                logger.error(f"Error looking up call in call_history: {e}")
                import traceback
                logger.error(traceback.format_exc())

        # Insert into database
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        c.execute('''
            INSERT OR IGNORE INTO call_records
            (uniqueid, caller_id, source_number, destination_number, context,
             channel, destination_channel, extension, call_type, direction,
             start_time, answer_time, end_time, duration, billsec, disposition,
             broadcast_id, recording_id, amd_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (uniqueid, caller_id, source, dest, context, channel, dst_channel,
              extension, call_type, direction, start, answer, end,
              duration_int, billsec_int, disposition, None, None, amd_result or None))

        conn.commit()
        conn.close()

        # Log AMD result if available
        amd_info = f", AMD: {amd_result}" if amd_result else ""
        logger.info(f"📞 Recorded call: {source} → {dest} ({call_type}) - Duration: {duration_int}s, Talk: {billsec_int}s, Status: {disposition}{amd_info}")

        # Update call_history status for ALL outbound calls (not just outbound-playback)
        # This catches calls regardless of their context, as long as they went through the trunk
        is_outbound_trunk_call = "trunk_main" in channel
        is_outbound_context = context in ("outbound-playback", "default", "internal", "from-asterisk")
        is_outbound_number = dest and len(dest) >= 10 and dest.isdigit()

        if is_outbound_trunk_call or (is_outbound_context and is_outbound_number):
            logger.info(f"🔄 Updating call_history for outbound call to {dest}")
            update_call_history_from_cdr(dest, duration_int, billsec_int, disposition)

    except Exception as e:
        logger.error(f"Error processing CDR: {e}")
        logger.error(f"Row data: {row}")


def normalize_phone_number(phone: str) -> str:
    """Normalize phone number by removing non-digits and keeping last 10 digits"""
    if not phone:
        return ""
    digits = ''.join(c for c in phone if c.isdigit())
    # Return last 10 digits (or all if less than 10)
    return digits[-10:] if len(digits) >= 10 else digits


def update_call_history_from_cdr(phone_number: str, duration: int, billsec: int, disposition: str):
    """Update call_history status when a call completes based on CDR data"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Normalize the phone number for matching
        normalized_phone = normalize_phone_number(phone_number)
        logger.info(f"🔍 Looking for call to update: original={phone_number}, normalized={normalized_phone}")

        # Determine voicemail status based on billable seconds
        if billsec >= 3:
            # Human answered and stayed on call
            is_voicemail = 0
            status = "completed"
        elif billsec > 0 and billsec < 3:
            # Quick hangup - likely declined
            is_voicemail = 2
            status = "completed"
        elif billsec == 0 and duration > 0:
            # No talk time but call connected - voicemail
            is_voicemail = 1
            status = "voicemail"
        else:
            # No answer
            is_voicemail = None
            status = "no_answer" if disposition == "NO ANSWER" else "failed"

        # First, find pending calls to see what we're working with
        cursor.execute('''
            SELECT id, phone_number, status, started_at FROM call_history
            WHERE status IN ('initiated', 'ringing', 'answered', 'hangup')
            ORDER BY started_at DESC
            LIMIT 10
        ''')
        pending_calls = cursor.fetchall()
        logger.info(f"🔍 Found {len(pending_calls)} pending calls in call_history")
        for call in pending_calls:
            logger.debug(f"   - ID: {call[0]}, Phone: {call[1]}, Status: {call[2]}, Started: {call[3]}")

        # Find and update the most recent matching call in call_history
        # Match by normalized phone number (last 10 digits)
        # Use subquery since SQLite doesn't support ORDER BY/LIMIT in UPDATE
        cursor.execute('''
            UPDATE call_history
            SET status = ?,
                is_voicemail = ?,
                duration = ?,
                ended_at = CURRENT_TIMESTAMP
            WHERE id = (
                SELECT id FROM call_history
                WHERE (
                    phone_number LIKE ?
                    OR phone_number LIKE ?
                    OR phone_number = ?
                    OR SUBSTR(REPLACE(REPLACE(REPLACE(phone_number, '-', ''), ' ', ''), '(', ''), -10) = ?
                )
                AND status IN ('initiated', 'ringing', 'answered', 'hangup')
                ORDER BY started_at DESC
                LIMIT 1
            )
        ''', (status, is_voicemail, duration,
              f'%{normalized_phone}', f'%{normalized_phone}%', phone_number, normalized_phone))

        rows_updated = cursor.rowcount
        conn.commit()
        conn.close()

        if rows_updated > 0:
            logger.info(f"✅ Updated call_history for {phone_number}: status={status}, is_voicemail={is_voicemail}, duration={duration}s")
        else:
            logger.warning(f"⚠️ No pending call found in call_history for {phone_number} (normalized: {normalized_phone})")

        return rows_updated > 0

    except Exception as e:
        logger.error(f"Failed to update call_history: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


def migrate_database():
    """Migrate database to add contact names"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check if group_members table exists
    cursor.execute('''
        SELECT name FROM sqlite_master 
        WHERE type='table' AND name='group_members'
    ''')
    
    if cursor.fetchone() is None:
        # Table doesn't exist yet, skip migration
        conn.close()
        return
    
    # Check if contact_name column exists
    cursor.execute("PRAGMA table_info(group_members)")
    columns = [column[1] for column in cursor.fetchall()]
    
    if 'contact_name' not in columns:
        logger.info("Migrating database: adding contact_name column")
        cursor.execute('''
            ALTER TABLE group_members 
            ADD COLUMN contact_name TEXT
        ''')
        conn.commit()
        logger.info("✓ Database migration completed")
    
    conn.close()

@app.on_event("startup")
async def startup_event():
    """Initialize on startup"""
    init_database()
    logger.info("✓ Database initialized")

    migrate_database()
    logger.info("✓ Database migrated")

    migrate_database_v2()
    logger.info("✓ Database migrated v2")

    migrate_database_v3()
    logger.info("✓ Database migrated v3")

    migrate_database_v4()
    logger.info("✓ Database migrated v4")

    migrate_database_v5()
    logger.info("✓ Database migrated v5")

    migrate_database_v6()
    logger.info("✓ Database migrated v6")

    asyncio.create_task(monitor_cdr())
    logger.info("✓ CDR monitoring task started")

    asyncio.create_task(run_scheduler())
    logger.info("✓ Call scheduler task started")

    os.makedirs(RECORDINGS_PATH, exist_ok=True)
    os.makedirs(ASTERISK_SOUNDS, exist_ok=True)
    logger.info("✓ API Service started")

def migrate_database_v2():
    """Add is_voicemail column to call_history"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Check if is_voicemail column exists
    cursor.execute("PRAGMA table_info(call_history)")
    columns = [column[1] for column in cursor.fetchall()]

    if 'is_voicemail' not in columns:
        logger.info("Migrating database: adding is_voicemail column")
        cursor.execute('''
            ALTER TABLE call_history
            ADD COLUMN is_voicemail INTEGER DEFAULT NULL
        ''')
        conn.commit()
        logger.info("✓ Database migration v2 completed")

    conn.close()


def migrate_database_v3():
    """Add broadcast-related columns to scheduled_calls"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Check if is_broadcast column exists
    cursor.execute("PRAGMA table_info(scheduled_calls)")
    columns = [column[1] for column in cursor.fetchall()]

    if 'is_broadcast' not in columns:
        logger.info("Migrating database: adding broadcast columns to scheduled_calls")
        cursor.execute('ALTER TABLE scheduled_calls ADD COLUMN is_broadcast INTEGER DEFAULT 0')
        cursor.execute('ALTER TABLE scheduled_calls ADD COLUMN concurrent_calls INTEGER DEFAULT 5')
        cursor.execute('ALTER TABLE scheduled_calls ADD COLUMN pre_message_delay INTEGER DEFAULT 1')
        cursor.execute('ALTER TABLE scheduled_calls ADD COLUMN max_ring_time INTEGER DEFAULT 45')
        conn.commit()
        logger.info("✓ Database migration v3 completed")

    conn.close()


def migrate_database_v4():
    """Add callback-related columns to broadcasts table"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Check if caller_id column exists in broadcasts
    cursor.execute("PRAGMA table_info(broadcasts)")
    columns = [column[1] for column in cursor.fetchall()]

    if 'caller_id' not in columns:
        logger.info("Migrating database: adding callback columns to broadcasts")
        cursor.execute('ALTER TABLE broadcasts ADD COLUMN caller_id TEXT')
        cursor.execute('ALTER TABLE broadcasts ADD COLUMN audio_file TEXT')
        conn.commit()
        logger.info("✓ Database migration v4 completed")

    conn.close()


def migrate_database_v5():
    """Ensure broadcast_callbacks table exists for existing databases"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Check if broadcast_callbacks table exists
    cursor.execute('''
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='broadcast_callbacks'
    ''')

    if cursor.fetchone() is None:
        logger.info("Migrating database: creating broadcast_callbacks table")
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
        logger.info("✓ Database migration v5 completed - broadcast_callbacks table created")

    conn.close()


def migrate_database_v6():
    """Add amd_status column to call_records for Answering Machine Detection results"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Check if amd_status column exists
    cursor.execute('PRAGMA table_info(call_records)')
    columns = [col[1] for col in cursor.fetchall()]

    if 'amd_status' not in columns:
        logger.info("Migrating database: adding amd_status column to call_records")
        cursor.execute('ALTER TABLE call_records ADD COLUMN amd_status TEXT')
        conn.commit()
        logger.info("✓ Database migration v6 completed - amd_status column added")

    conn.close()


async def run_scheduler():
    """Background task to execute scheduled calls"""
    import requests
    import json as json_lib

    while True:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            # Get all enabled scheduled calls that are due
            now = datetime.now().strftime("%Y-%m-%dT%H:%M")
            cursor.execute('''
                SELECT id, name, phone_number, group_name, message, tts_text,
                       recording_file, caller_id, scheduled_time, repeat_type, repeat_days,
                       is_broadcast, concurrent_calls, pre_message_delay, max_ring_time
                FROM scheduled_calls
                WHERE enabled = 1
                  AND status = 'pending'
                  AND scheduled_time <= ?
            ''', (now,))

            due_calls = cursor.fetchall()
            conn.close()

            for call in due_calls:
                (call_id, name, phone_number, group_name, message, tts_text, recording_file,
                 caller_id, scheduled_time, repeat_type, repeat_days, is_broadcast,
                 concurrent_calls, pre_message_delay, max_ring_time) = call

                # Handle None values for broadcast settings
                is_broadcast = is_broadcast or 0
                concurrent_calls = concurrent_calls or 5
                pre_message_delay = pre_message_delay or 1
                max_ring_time = max_ring_time or 45

                logger.info(f"⏰ Executing scheduled {'broadcast' if is_broadcast or group_name else 'call'}: {name}")

                try:
                    # Determine if this is a broadcast
                    is_broadcast_call = is_broadcast or group_name

                    if is_broadcast_call:
                        # It's a broadcast to a group or multiple numbers
                        payload = {
                            "name": f"Scheduled: {name}",
                            "caller_id": caller_id,
                            "concurrent_calls": concurrent_calls,
                            "pre_message_delay": pre_message_delay,
                            "max_ring_time": max_ring_time
                        }

                        if group_name:
                            payload["group_name"] = group_name
                        elif phone_number:
                            # Parse phone numbers if it's a JSON array
                            try:
                                phones = json_lib.loads(phone_number)
                                if isinstance(phones, list):
                                    payload["phone_numbers"] = phones
                                else:
                                    payload["phone_numbers"] = [phone_number]
                            except (json_lib.JSONDecodeError, TypeError):
                                payload["phone_numbers"] = [phone_number]

                        if tts_text:
                            payload["tts_text"] = tts_text
                        elif recording_file:
                            payload["recording_file"] = recording_file
                        elif message:
                            payload["message"] = message

                        # Make internal API call for broadcast
                        # Longer timeout to allow for TTS generation
                        response = requests.post(
                            "http://localhost:8088/api/broadcast",
                            json=payload,
                            timeout=120
                        )
                    else:
                        # Single call
                        payload = {
                            "phone_number": phone_number,
                            "caller_id": caller_id,
                            "pre_message_delay": pre_message_delay,
                            "max_ring_time": max_ring_time
                        }
                        if tts_text:
                            payload["tts_text"] = tts_text
                        elif recording_file:
                            payload["recording_file"] = recording_file
                        elif message:
                            payload["message"] = message

                        # Longer timeout to allow for TTS generation
                        response = requests.post(
                            "http://localhost:8088/api/call",
                            json=payload,
                            timeout=120
                        )

                    if response.status_code == 200:
                        logger.info(f"✅ Scheduled {'broadcast' if is_broadcast_call else 'call'} executed: {name}")
                    else:
                        logger.error(f"❌ Scheduled {'broadcast' if is_broadcast_call else 'call'} failed: {name} - {response.status_code}")

                except Exception as e:
                    logger.error(f"Error executing scheduled call {name}: {e}")

                # Update the scheduled call status
                conn = get_db_connection()
                cursor = conn.cursor()

                if repeat_type == 'none':
                    # One-time call - mark as completed
                    cursor.execute('''
                        UPDATE scheduled_calls
                        SET status = 'completed', last_run = ?
                        WHERE id = ?
                    ''', (datetime.now().isoformat(), call_id))
                else:
                    # Recurring call - calculate next run time
                    next_run = calculate_next_run(scheduled_time, repeat_type, repeat_days)
                    cursor.execute('''
                        UPDATE scheduled_calls
                        SET last_run = ?, scheduled_time = ?, next_run = ?
                        WHERE id = ?
                    ''', (datetime.now().isoformat(), next_run, next_run, call_id))

                conn.commit()
                conn.close()

            # Check every 30 seconds
            await asyncio.sleep(30)

        except Exception as e:
            logger.error(f"Scheduler error: {e}")
            await asyncio.sleep(60)


def calculate_next_run(current_time: str, repeat_type: str, repeat_days: str) -> str:
    """Calculate the next run time for recurring scheduled calls"""
    from datetime import timedelta

    try:
        # Parse the current scheduled time
        dt = datetime.fromisoformat(current_time.replace('Z', '+00:00').replace('.000', ''))
    except ValueError:
        dt = datetime.strptime(current_time[:16], "%Y-%m-%dT%H:%M")

    if repeat_type == 'daily':
        next_dt = dt + timedelta(days=1)
    elif repeat_type == 'weekly':
        next_dt = dt + timedelta(weeks=1)
    elif repeat_type == 'monthly':
        # Add roughly a month
        next_dt = dt + timedelta(days=30)
    else:
        next_dt = dt

    return next_dt.strftime("%Y-%m-%dT%H:%M")


# ============================================================================
# MODELS
# ============================================================================

class CallRequest(BaseModel):
    phone_number: str
    message: Optional[str] = None
    recording_file: Optional[str] = None
    tts_text: Optional[str] = None
    caller_id: Optional[str] = None
    max_retries: int = 3
    pre_message_delay: int = 1  # Default: 1 second
    max_ring_time: int = 45  # Default: 45 seconds

class BroadcastRequest(BaseModel):
    name: str
    phone_numbers: Optional[List[str]] = None
    group_name: Optional[str] = None
    message: Optional[str] = None
    recording_file: Optional[str] = None
    tts_text: Optional[str] = None
    caller_id: Optional[str] = None
    concurrent_calls: int = 5
    pre_message_delay: int = 1  # Default: 1 second
    max_ring_time: int = 45  # Default: 45 seconds

class ContactMember(BaseModel):
    name: str
    phone_number: str

class ContactGroup(BaseModel):
    name: str
    contacts: List[ContactMember]  # Changed from phone_numbers
    caller_id: Optional[str] = None

class ScheduledCall(BaseModel):
    name: str
    phone_number: Optional[str] = None
    group_name: Optional[str] = None
    message: Optional[str] = None
    tts_text: Optional[str] = None
    recording_file: Optional[str] = None
    caller_id: Optional[str] = None
    scheduled_time: str  # ISO format datetime
    repeat_type: str = "none"  # none, daily, weekly, monthly
    repeat_days: Optional[str] = None  # comma-separated days for weekly (0-6, 0=Monday)
    enabled: bool = True
    is_broadcast: bool = False  # Whether this is a broadcast or single call
    concurrent_calls: int = 5  # For broadcasts
    pre_message_delay: int = 1  # For broadcasts
    max_ring_time: int = 45  # For broadcasts

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def load_config():
    """Load add-on configuration"""
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        return {}

def fire_ha_event(event_type: str, event_data: dict):
    """Fire event to Home Assistant"""
    try:
        import requests
        headers = {
            "Authorization": f"Bearer {HA_TOKEN}",
            "Content-Type": "application/json"
        }
        requests.post(
            f"{HA_URL}/events/phone_system_{event_type}",
            headers=headers,
            json=event_data,
            timeout=5
        )
        logger.info(f"Event fired: phone_system_{event_type}")
    except Exception as e:
        logger.error(f"Error firing HA event: {e}")

def get_db():
    """Get database connection"""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    return conn

def get_default_did() -> Optional[str]:
    """Get the default DID (caller ID) from the database.

    Returns the phone number of the default DID, or None if not set.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT phone_number FROM dids WHERE is_default = 1 LIMIT 1')
        row = cursor.fetchone()
        conn.close()
        if row:
            return row['phone_number']
        return None
    except Exception as e:
        logger.error(f"Error getting default DID: {e}")
        return None

async def generate_tts(text: str) -> Optional[str]:
    """Generate TTS audio file using Google TTS"""
    try:
        import hashlib
        from gtts import gTTS
        
        # ✅ FIX 1: Validate input text
        if not text or text.strip() == '':
            logger.warning("⚠️ Empty text provided, using default message")
            text = "This is a test message from your phone system"
        
        # Create hash of text for consistent filenames
        text_hash = hashlib.md5(text.encode()).hexdigest()
        filename = f"tts_{text_hash}.wav"
        output_path = os.path.join(ASTERISK_SOUNDS, filename)
        
        # Check if file already exists
        if os.path.exists(output_path):
            logger.info(f"TTS file already exists: {filename}")
            return filename.replace('.wav', '')  # ✅ Return without extension
        
        logger.info(f"Generating TTS: {text[:50]}...")
        
        # ✅ FIX 2: Ensure ASTERISK_SOUNDS directory exists
        os.makedirs(ASTERISK_SOUNDS, exist_ok=True)
        
        # Generate TTS using Google
        mp3_path = output_path.replace('.wav', '.mp3')
        tts = gTTS(text=text, lang='en', slow=False)
        tts.save(mp3_path)
        
        logger.info(f"✓ MP3 file created: {mp3_path}")
        
        # ✅ FIX 3: Try sox first, fallback to ffmpeg
        convert_success = False
        
        # Try sox
        try:
            convert_result = subprocess.run(
                ['sox', mp3_path, '-r', '8000', '-c', '1', output_path],
                capture_output=True,
                check=False,
                timeout=10
            )
            
            if convert_result.returncode == 0 and os.path.exists(output_path):
                convert_success = True
                logger.info(f"✓ Converted with sox")
        except Exception as sox_error:
            logger.warning(f"Sox conversion failed: {sox_error}")
        
        # Fallback to ffmpeg if sox failed
        if not convert_success:
            try:
                convert_result = subprocess.run(
                    ['ffmpeg', '-i', mp3_path, '-ar', '8000', '-ac', '1', '-y', output_path],
                    capture_output=True,
                    check=False,
                    timeout=10
                )
                
                if convert_result.returncode == 0 and os.path.exists(output_path):
                    convert_success = True
                    logger.info(f"✓ Converted with ffmpeg")
            except Exception as ffmpeg_error:
                logger.warning(f"Ffmpeg conversion failed: {ffmpeg_error}")
        
        # Clean up MP3
        if os.path.exists(mp3_path):
            os.remove(mp3_path)
        
        # ✅ FIX 4: Return result or fallback
        if convert_success and os.path.exists(output_path):
            # Set permissions
            try:
                subprocess.run(['chown', 'asterisk:asterisk', output_path], check=False)
                subprocess.run(['chmod', '644', output_path], check=False)
            except (OSError, subprocess.SubprocessError):
                pass  # Permissions might fail in some environments
            
            logger.info(f"✓ TTS file created: {filename}")
            return filename.replace('.wav', '')  # Return without extension
        else:
            logger.error(f"❌ Audio conversion failed")
            # ✅ Return a fallback sound
            return "beep"  # Asterisk has a built-in beep sound
            
    except Exception as e:
        logger.error(f"❌ Error generating TTS: {e}")
        import traceback
        logger.error(traceback.format_exc())
        # ✅ Return fallback instead of None
        return "beep"

def create_call_file(phone_number: str, audio_file: str, caller_id: str = None, 
                    call_id: str = None, max_retries: int = 3, 
                    pre_message_delay: int = 1, max_ring_time: int = 45):
    """Create Asterisk call file"""
    if not call_id:
        call_id = uuid.uuid4().hex
    
    config = load_config()
    
    # Use provided caller_id (if not empty), fallback to config, then "Home Assistant"
    if not caller_id or caller_id.strip() == "":
        caller_id = config.get('sip_trunk', {}).get('caller_number') or 'Home Assistant'
    
    # Add logging to debug
    logger.info(f"Caller ID being used: {caller_id}")
    logger.info(f"Config caller_number: {config.get('sip_trunk', {}).get('caller_number')}")
    
    # Check if AMD is enabled
    amd_enabled = is_amd_enabled()

    # Build call file content with caller ID in the channel
    call_file_content = f"""Channel: SIP/trunk_main/{phone_number}
CallerID: {caller_id}
MaxRetries: {max_retries}
RetryTime: 300
WaitTime: {max_ring_time}
Context: outbound-playback
Extension: s
Priority: 1
Setvar: AUDIO_FILE={audio_file}
Setvar: CALL_ID={call_id}
Setvar: PHONE_NUMBER={phone_number}
Setvar: PRE_MESSAGE_DELAY={pre_message_delay}
Setvar: CUSTOM_CALLERID={caller_id}
Setvar: AMD_ENABLED={1 if amd_enabled else 0}
"""
    
    # Log the actual call file content
    logger.info(f"Call file content:\n{call_file_content}")
    
    # Write to temp file first
    temp_file = f"/tmp/call_{call_id}.call"
    with open(temp_file, 'w') as f:
        f.write(call_file_content)
    
    # Move to spool (atomic operation triggers Asterisk)
    spool_file = os.path.join(ASTERISK_SPOOL, f"call_{call_id}.call")
    os.rename(temp_file, spool_file)
    
    logger.info(f"Call file created: {call_id} -> {phone_number}")
    return call_id
def save_call_to_db(call_id: str, phone_number: str, audio_file: str, 
                   caller_id: str = None, group_name: str = None, 
                   broadcast_id: str = None):
    """Save call to database"""
    try:
        logger.info(f"💾 Saving call to DB: {call_id} → {phone_number} (Caller: {caller_id})")  # ✅ ADD THIS
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO call_history 
            (call_id, phone_number, direction, status, audio_file, caller_id, group_name, broadcast_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (call_id, phone_number, 'outbound', 'initiated', audio_file, caller_id, group_name, broadcast_id))
        conn.commit()
        conn.close()
        logger.info(f"✅ Call saved to database")  # ✅ ADD THIS
    except Exception as e:
        logger.error(f"Error saving call to DB: {e}")

async def process_broadcast(broadcast_id: str, request: BroadcastRequest):
    """Process broadcast calls"""
    logger.info(f"Processing broadcast: {broadcast_id}")
    
    conn = get_db()
    cursor = conn.cursor()
    
    # Get phone numbers and group's caller ID if applicable
    phone_numbers = request.phone_numbers or []
    group_caller_id = None

    if request.group_name:
        # Load group's caller_id first
        cursor.execute('''
            SELECT caller_id FROM contact_groups WHERE name = ?
        ''', (request.group_name,))
        group_row = cursor.fetchone()
        if group_row and group_row[0]:
            group_caller_id = group_row[0]
            logger.info(f"📞 Using group '{request.group_name}' caller ID: {group_caller_id}")

        # Load phone numbers from group
        cursor.execute('''
            SELECT gm.phone_number
            FROM group_members gm
            JOIN contact_groups cg ON gm.group_id = cg.id
            WHERE cg.name = ?
        ''', (request.group_name,))
        phone_numbers.extend([row[0] for row in cursor.fetchall()])
    
    if not phone_numbers:
        logger.error(f"No phone numbers for broadcast {broadcast_id}")
        cursor.execute('UPDATE broadcasts SET status = ? WHERE broadcast_id = ?',
                      ('failed', broadcast_id))
        conn.commit()
        conn.close()
        return
    
    # Prepare audio
    if request.tts_text:
        audio_file = await generate_tts(request.tts_text)
    elif request.recording_file:
        audio_file = request.recording_file
    else:
        audio_file = request.message
    
    if not audio_file:
        logger.error(f"No audio file for broadcast {broadcast_id}")
        cursor.execute('UPDATE broadcasts SET status = ? WHERE broadcast_id = ?',
                      ('failed', broadcast_id))
        conn.commit()
        conn.close()
        return

    # Determine effective caller ID with priority:
    # 1. Group's caller_id (if group specified and has one)
    # 2. Request's caller_id
    # 3. Default DID from settings
    effective_caller_id = group_caller_id or request.caller_id or get_default_did()
    if effective_caller_id:
        logger.info(f"📞 Effective caller ID for broadcast: {effective_caller_id}")

    # Update status and store caller_id/audio_file for callback feature
    cursor.execute('''
        UPDATE broadcasts
        SET status = ?, caller_id = ?, audio_file = ?
        WHERE broadcast_id = ?
    ''', ('processing', effective_caller_id, audio_file, broadcast_id))
    conn.commit()

    # Create callback entries for each recipient (for call-back playback feature)
    # Each recipient who receives the broadcast can call back and hear the message
    # Set expiration to 7 days from now
    expires_at = (datetime.now() + timedelta(days=7)).isoformat()
    for recipient_number in phone_numbers:
        # Normalize recipient number - store last 10 digits for matching
        normalized_recipient = ''.join(c for c in recipient_number if c.isdigit())
        if len(normalized_recipient) > 10:
            normalized_recipient = normalized_recipient[-10:]

        cursor.execute('''
            INSERT INTO broadcast_callbacks
            (caller_id, audio_file, broadcast_id, broadcast_name, expires_at, enabled)
            VALUES (?, ?, ?, ?, ?, 1)
        ''', (normalized_recipient, audio_file, broadcast_id, request.name, expires_at))
    conn.commit()
    logger.info(f"📞 Callbacks created for {len(phone_numbers)} recipients -> {audio_file}")
    
    # Process calls with concurrency control
    semaphore = asyncio.Semaphore(request.concurrent_calls)
    
    async def make_call(phone_number: str):
        async with semaphore:
            try:
                call_id = create_call_file(
                    phone_number,
                    audio_file,
                    effective_caller_id,
                    call_id=f"{broadcast_id}_{uuid.uuid4().hex[:8]}",
                    pre_message_delay=request.pre_message_delay,
                    max_ring_time=request.max_ring_time
                )

                save_call_to_db(
                    call_id,
                    phone_number,
                    audio_file,
                    effective_caller_id,
                    request.group_name,
                    broadcast_id
                )
                
                cursor.execute('''
                    UPDATE broadcasts 
                    SET in_progress = in_progress + 1 
                    WHERE broadcast_id = ?
                ''', (broadcast_id,))
                conn.commit()
                
                await asyncio.sleep(2)
                
            except Exception as e:
                logger.error(f"Error calling {phone_number}: {e}")
                cursor.execute('''
                    UPDATE broadcasts 
                    SET failed = failed + 1 
                    WHERE broadcast_id = ?
                ''', (broadcast_id,))
                conn.commit()
    
    # Execute all calls
    tasks = [make_call(number) for number in phone_numbers]
    await asyncio.gather(*tasks)
    
    # Mark broadcast as completed
    cursor.execute('''
        UPDATE broadcasts 
        SET status = ?, completed_at = CURRENT_TIMESTAMP 
        WHERE broadcast_id = ?
    ''', ('completed', broadcast_id))
    conn.commit()
    conn.close()
    
    fire_ha_event("broadcast_completed", {
        "broadcast_id": broadcast_id,
        "name": request.name,
        "total": len(phone_numbers)
    })
    
    logger.info(f"Broadcast completed: {broadcast_id}")

# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


@app.post("/api/call")
async def make_call(request: Request):
    try:
        data = await request.json()
        phone_number = data.get('phone_number', '')
        message = data.get('message') or data.get('tts_text', '')  # Might be empty!
        recording_file = data.get('recording_file', '')  # Pre-recorded audio file
        custom_caller_id = data.get('caller_id', None)

        logger.info(f"📞 Call request: {phone_number}")

        # Detect call type
        direction, formatted_number = detect_call_type(phone_number)

        # Determine audio source: recording file OR TTS
        if recording_file:
            # Use existing recording file
            # Extract just the filename without path and extension for Asterisk
            # e.g. "/var/lib/asterisk/sounds/custom/outbound-123.wav" -> "outbound-123"
            # or "outbound-123.wav" -> "outbound-123"
            base_name = os.path.basename(recording_file)
            # Remove extension
            audio_file = os.path.splitext(base_name)[0]
            logger.info(f"🎙️ Using recording file: {recording_file} -> {audio_file}")
        else:
            # Generate TTS from message
            if not message or message.strip() == '':
                message = "This is a test call from your phone system"
                logger.warning(f"⚠️ No message provided, using default")

            logger.info(f"📝 Message: {message[:100]}")
            audio_file = await generate_tts(message)

        # ✅ Validate audio file
        if not audio_file:
            logger.error("❌ No audio file, using beep")
            audio_file = "beep"

        logger.info(f"🔊 Using audio file: {audio_file}")
        
        call_id = str(uuid.uuid4())
        
        
        # ✅ FLEXIBLE CALLER ID LOGIC
        # Priority: 1. Custom from request, 2. Default DID from DB, 3. Environment, 4. Config file
        if custom_caller_id:
            # Use caller ID from API request
            caller_number = custom_caller_id
            logger.info(f"🆔 Using CUSTOM Caller ID from request: {caller_number}")
        else:
            # Try to get default DID from database (system settings)
            caller_number = get_default_did()
            if caller_number:
                logger.info(f"🆔 Using DEFAULT DID from settings: {caller_number}")
            else:
                # Fallback to environment variable
                caller_number = os.getenv('CALLER_NUMBER', '')

                # If not in env, try to get from add-on config
                if not caller_number:
                    try:
                        with open('/data/options.json', 'r') as f:
                            options = json.load(f)
                            caller_number = options.get('sip_trunk', {}).get('caller_number', '')
                    except (FileNotFoundError, json.JSONDecodeError, KeyError):
                        pass

                if caller_number:
                    logger.info(f"🆔 Using Caller ID from addon config: {caller_number}")
                else:
                    logger.warning(f"⚠️ No default DID or caller ID configured")
        
        # Ensure E.164 format
        if caller_number and not caller_number.startswith('+'):
            caller_number = f'+{caller_number}'
        
        # Build channel based on direction
        channel = get_channel_string(direction, formatted_number)
        
        # Set caller ID
        if direction == CallDirection.INTERNAL:
            callerid_line = f'CallerID: "Internal" <100>'
        else:
            # For external calls, use the determined caller ID
            if not caller_number:
                logger.warning("⚠️ No caller ID configured! Calls may fail or show unknown caller.")
                # Use a generic caller ID - trunk provider may override this
                callerid_line = 'CallerID: "Phone System" <0000000000>'
            else:
                callerid_line = f'CallerID: {caller_number}'
        
        # Check if AMD is enabled
        amd_enabled = is_amd_enabled()

        # Create call file
        call_file_content = f"""Channel: {channel}
{callerid_line}
MaxRetries: 2
RetryTime: 60
WaitTime: 30
Context: outbound-playback
Extension: s
Priority: 1
Setvar: AUDIO_FILE={audio_file}
Setvar: CALL_ID={call_id}
Setvar: PHONE_NUMBER={formatted_number}
Setvar: CALL_DIRECTION={direction}
Setvar: PRE_MESSAGE_DELAY=1
Setvar: CUSTOM_CALLERID={caller_number or ''}
Setvar: AMD_ENABLED={1 if amd_enabled else 0}
"""

        logger.info(f"📡 Channel: {channel}")
        logger.info(f"📍 Direction: {direction}")
        logger.info(f"📞 Final Caller ID: {caller_number or 'Not configured'}")

        # Write call file
        temp_file = f"/tmp/{call_id}.call"
        final_file = f"/var/spool/asterisk/outgoing/{call_id}.call"

        with open(temp_file, 'w') as f:
            f.write(call_file_content)

        # Set permissions - asterisk needs to read this file
        os.chmod(temp_file, 0o644)
        os.rename(temp_file, final_file)
        
        logger.info(f"✅ Call initiated: {call_id}")

        # ✅ ADD THIS - Save call to database
        save_call_to_db(
            call_id=call_id,
            phone_number=formatted_number,  # The number being called
            audio_file=audio_file,
            caller_id=caller_number,        # The caller ID being used
            group_name=None,
            broadcast_id=None
        )

        # Create callback entry so recipient can call back and hear the message
        try:
            conn = get_db()
            cursor = conn.cursor()
            # Normalize recipient number for matching
            normalized_recipient = ''.join(c for c in formatted_number if c.isdigit())
            if len(normalized_recipient) > 10:
                normalized_recipient = normalized_recipient[-10:]
            expires_at = (datetime.now() + timedelta(days=7)).isoformat()

            # Get full audio path for callback
            full_audio_path = f"/var/lib/asterisk/sounds/custom/{audio_file}.wav" if not audio_file.startswith('/') else audio_file

            cursor.execute('''
                INSERT INTO broadcast_callbacks
                (caller_id, audio_file, broadcast_id, broadcast_name, expires_at, enabled)
                VALUES (?, ?, ?, ?, ?, 1)
            ''', (normalized_recipient, full_audio_path, call_id, f"Call to {formatted_number}", expires_at))
            conn.commit()
            conn.close()
            logger.info(f"📞 Callback created for {normalized_recipient}")
        except Exception as cb_err:
            logger.warning(f"Could not create callback: {cb_err}")

        return {
            "status": "success",
            "call_id": call_id,
            "direction": direction,
            "phone_number": formatted_number,
            "caller_id": caller_number
        }
        
    except ValueError as e:
        logger.error(f"❌ Invalid input: {e}")
        return {"status": "error", "message": str(e)}
    except Exception as e:
        logger.error(f"❌ Call error: {e}")
        return {"status": "error", "message": str(e)}
@app.post("/api/broadcast")
async def create_broadcast(request: BroadcastRequest, background_tasks: BackgroundTasks):
    """Create and start a broadcast"""
    try:
        broadcast_id = uuid.uuid4().hex
        logger.info(f"Broadcast request: {broadcast_id} - {request.name}")
        
        # Count total numbers
        total_numbers = len(request.phone_numbers or [])
        if request.group_name:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COUNT(*) FROM group_members gm
                JOIN contact_groups cg ON gm.group_id = cg.id
                WHERE cg.name = ?
            ''', (request.group_name,))
            total_numbers += cursor.fetchone()[0]
            conn.close()
        
        # Save broadcast to database
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO broadcasts 
            (broadcast_id, name, status, total_numbers)
            VALUES (?, ?, ?, ?)
        ''', (broadcast_id, request.name, 'initiated', total_numbers))
        conn.commit()
        conn.close()
        
        # Start broadcast in background
        background_tasks.add_task(process_broadcast, broadcast_id, request)
        
        fire_ha_event("broadcast_started", {
            "broadcast_id": broadcast_id,
            "name": request.name,
            "total_numbers": total_numbers
        })
        
        return {
            "status": "success",
            "broadcast_id": broadcast_id,
            "total_numbers": total_numbers
        }
        
    except Exception as e:
        logger.error(f"Error creating broadcast: {e}")
        raise HTTPException(status_code=500, detail=str(e))




@app.post("/api/call_status")
async def update_call_status(call_id: str, status: str):
    """Update call status - uses duration-based voicemail detection"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        if status == "completed":
            # Get the call duration from CDR to determine if human answered
            cursor.execute('''
                SELECT billsec, duration FROM call_records 
                WHERE uniqueid LIKE ? 
                ORDER BY created_at DESC LIMIT 1
            ''', (f'%{call_id}%',))
            
            cdr_row = cursor.fetchone()
            
            if cdr_row:
                billsec = int(cdr_row[0]) if cdr_row[0] else 0
                duration = int(cdr_row[1]) if cdr_row[1] else 0
                
                # Duration-based detection logic
                if billsec >= 3:
                    # Human answered and stayed on call
                    is_voicemail = 0
                    status = "human_answered"
                elif billsec > 0 and billsec < 3:
                    # Quick hangup - likely declined or brief answer
                    is_voicemail = 2
                    status = "uncertain"
                elif billsec == 0 and duration > 0:
                    # No talk time but call connected - voicemail
                    is_voicemail = 1
                    status = "voicemail"
                else:
                    # Unknown
                    is_voicemail = 2
                    status = "uncertain"
                
                # Update call_history with detection result
                cursor.execute('''
                    UPDATE call_history 
                    SET status = ?, is_voicemail = ?, 
                        ended_at = CURRENT_TIMESTAMP,
                        duration = ?
                    WHERE call_id = ?
                ''', (status, is_voicemail, duration, call_id))
            else:
                # No CDR found yet, just mark as completed
                cursor.execute('''
                    UPDATE call_history 
                    SET status = ?, ended_at = CURRENT_TIMESTAMP,
                        duration = (julianday(CURRENT_TIMESTAMP) - julianday(started_at)) * 86400
                    WHERE call_id = ?
                ''', (status, call_id))
            
            # Update broadcast stats if applicable
            cursor.execute('SELECT broadcast_id FROM call_history WHERE call_id = ?', (call_id,))
            row = cursor.fetchone()
            if row and row[0]:
                broadcast_id = row[0]
                cursor.execute('''
                    UPDATE broadcasts 
                    SET completed = completed + 1, in_progress = in_progress - 1
                    WHERE broadcast_id = ?
                ''', (broadcast_id,))
        
        elif status in ("hangup", "failed"):
            cursor.execute('''
                UPDATE call_history 
                SET status = ?, ended_at = CURRENT_TIMESTAMP
                WHERE call_id = ?
            ''', (status, call_id))
            
            # Update broadcast stats
            cursor.execute('SELECT broadcast_id FROM call_history WHERE call_id = ?', (call_id,))
            row = cursor.fetchone()
            if row and row[0]:
                broadcast_id = row[0]
                cursor.execute('''
                    UPDATE broadcasts 
                    SET failed = failed + 1, in_progress = in_progress - 1
                    WHERE broadcast_id = ?
                ''', (broadcast_id,))
        
        conn.commit()
        conn.close()
        
        return {"status": "ok"}
        
    except Exception as e:
        logger.error(f"Error updating call status: {e}")
        return {"status": "error", "message": str(e)}
@app.post("/api/call_status/ringing")
async def update_call_ringing(call_id: str):
    """Mark call as ringing"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE call_history 
            SET status = 'ringing'
            WHERE call_id = ?
        ''', (call_id,))
        conn.commit()
        conn.close()
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error updating ringing status: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/call_status/answered")
async def update_call_answered(call_id: str):
    """Mark call as answered"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE call_history 
            SET status = 'answered'
            WHERE call_id = ?
        ''', (call_id,))
        conn.commit()
        conn.close()
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error updating answered status: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/api/calls/active")
async def get_active_calls():
    """Get currently active calls"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT call_id, phone_number, caller_id, status, audio_file,
                   strftime('%s', 'now') - strftime('%s', started_at) as duration_seconds,
                   started_at
            FROM call_history
            WHERE status IN ('initiated', 'ringing', 'answered')
            ORDER BY started_at DESC
        ''')
        
        active_calls = []
        for row in cursor.fetchall():
            active_calls.append({
                "call_id": row[0],
                "phone_number": row[1],
                "caller_id": row[2],
                "status": row[3],
                "audio_file": row[4],
                "duration": row[5],
                "started_at": row[6]
            })
        
        conn.close()
        return {"active_calls": active_calls}
        
    except Exception as e:
        logger.error(f"Error getting active calls: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/broadcasts")
async def list_broadcasts():
    """List all broadcasts"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT broadcast_id, name, status, total_numbers, 
                   completed, failed, in_progress, created_at, completed_at
            FROM broadcasts
            ORDER BY created_at DESC
            LIMIT 50
        ''')
        
        broadcasts = []
        for row in cursor.fetchall():
            broadcasts.append({
                "broadcast_id": row[0],
                "name": row[1],
                "status": row[2],
                "total_numbers": row[3],
                "completed": row[4],
                "failed": row[5],
                "in_progress": row[6],
                "created_at": row[7],
                "completed_at": row[8]
            })
        
        conn.close()
        return {"broadcasts": broadcasts}
        
    except Exception as e:
        logger.error(f"Error listing broadcasts: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/call_history")
async def get_call_history(limit: int = 50):
    """Get call history"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT call_id, phone_number, direction, status, 
                   caller_id, started_at, ended_at, duration, group_name
            FROM call_history
            ORDER BY started_at DESC
            LIMIT ?
        ''', (limit,))
        
        calls = []
        for row in cursor.fetchall():
            calls.append({
                "call_id": row[0],
                "phone_number": row[1],
                "direction": row[2],
                "status": row[3],
                "caller_id": row[4],
                "started_at": row[5],
                "ended_at": row[6],
                "duration": row[7],
                "group_name": row[8]
            })
        
        conn.close()
        return {"calls": calls}
        
    except Exception as e:
        logger.error(f"Error getting call history: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/groups")
async def create_group(group: ContactGroup):
    """Create a contact group"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Create group
        cursor.execute('''
            INSERT INTO contact_groups (name, caller_id)
            VALUES (?, ?)
        ''', (group.name, group.caller_id))
        
        group_id = cursor.lastrowid
        
        # Add members with names
        for contact in group.contacts:
            cursor.execute('''
                INSERT INTO group_members (group_id, phone_number, contact_name)
                VALUES (?, ?, ?)
            ''', (group_id, contact.phone_number, contact.name))
        
        conn.commit()
        conn.close()
        
        return {"status": "success", "group_id": group_id, "name": group.name}
        
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Group already exists")
    except Exception as e:
        logger.error(f"Error creating group: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/groups")
async def list_groups():
    """List all contact groups"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT cg.id, cg.name, cg.caller_id, COUNT(gm.id) as member_count
            FROM contact_groups cg
            LEFT JOIN group_members gm ON cg.id = gm.group_id
            GROUP BY cg.id
        ''')
        
        groups = []
        for row in cursor.fetchall():
            groups.append({
                "id": row[0],
                "name": row[1],
                "caller_id": row[2],
                "member_count": row[3]
            })
        
        conn.close()
        return {"groups": groups}
        
    except Exception as e:
        logger.error(f"Error listing groups: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/groups/{group_name}")
async def get_group_details(group_name: str):
    """Get details of a specific contact group"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get group info
        cursor.execute('''
            SELECT id, name, caller_id
            FROM contact_groups
            WHERE name = ?
        ''', (group_name,))
        
        group_row = cursor.fetchone()
        if not group_row:
            raise HTTPException(status_code=404, detail="Group not found")
        
        group_id, name, caller_id = group_row
        
        # Get group members with names
        cursor.execute('''
            SELECT phone_number, contact_name
            FROM group_members
            WHERE group_id = ?
        ''', (group_id,))
        
        contacts = []
        for row in cursor.fetchall():
            contacts.append({
                "phone_number": row[0],
                "name": row[1] or ""  # Default to empty string if NULL
            })
        
        conn.close()
        
        return {
            "id": group_id,
            "name": name,
            "caller_id": caller_id,
            "contacts": contacts
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting group details: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/groups/{group_name}")
async def update_group(group_name: str, group: ContactGroup):
    """Update a contact group"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get group ID
        cursor.execute('SELECT id FROM contact_groups WHERE name = ?', (group_name,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Group not found")
        
        group_id = row[0]
        
        # Update group info
        cursor.execute('''
            UPDATE contact_groups 
            SET name = ?, caller_id = ?
            WHERE id = ?
        ''', (group.name, group.caller_id, group_id))
        
        # Delete old members
        cursor.execute('DELETE FROM group_members WHERE group_id = ?', (group_id,))
        
        # Add new members with names
        for contact in group.contacts:
            cursor.execute('''
                INSERT INTO group_members (group_id, phone_number, contact_name)
                VALUES (?, ?, ?)
            ''', (group_id, contact.phone_number, contact.name))
        
        conn.commit()
        conn.close()
        
        return {"status": "success", "message": "Group updated successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating group: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/groups/{group_name}")
async def delete_group(group_name: str):
    """Delete a contact group"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM contact_groups WHERE name = ?', (group_name,))
        conn.commit()
        conn.close()
        
        return {"status": "success", "message": "Group deleted"}
        
    except Exception as e:
        logger.error(f"Error deleting group: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ... rest of your code including hangup_call stays the same
@app.post("/api/calls/{call_id}/hangup")
async def hangup_call(call_id: str):
    """Hangup an active call"""
    # CRITICAL: Print immediately to bypass any logging buffer issues
    print(f"========== HANGUP ENDPOINT HIT: {call_id} ==========", flush=True)
    
    try:
        logger.info(f"Hangup requested for call_id: {call_id}")
        
        # Step 1: Try to delete the call file
        spool_dir = "/var/spool/asterisk/outgoing"
        deleted_file = False
        
        print(f"Checking spool directory: {spool_dir}", flush=True)
        
        try:
            files = os.listdir(spool_dir)
            print(f"Files in spool: {files}", flush=True)
            
            for filename in files:
                print(f"Checking file: {filename}", flush=True)
                if call_id in filename:
                    file_path = os.path.join(spool_dir, filename)
                    os.remove(file_path)
                    logger.info(f"Deleted call file: {filename}")
                    print(f"DELETED FILE: {filename}", flush=True)
                    deleted_file = True
                    break
        except Exception as e:
            error_msg = f"Could not delete call file: {e}"
            logger.warning(error_msg)
            print(error_msg, flush=True)
        
        # Step 2: Try to hangup active channels - UPDATED to catch ringing
        result = subprocess.run(
            ['asterisk', '-rx', 'core show channels concise'],
            capture_output=True,
            text=True,
            check=False
        )

        logger.info(f"Channels output: {result.stdout}")

        channel = None
        for line in result.stdout.split('\n'):
            # Match ANY trunk_main channel - remove context requirement
            if 'trunk_main' in line:
                parts = line.split('!')
                if parts:
                    channel = parts[0]
                    state = parts[4] if len(parts) > 4 else 'Unknown'
                    context = parts[1] if len(parts) > 1 else 'Unknown'
                    logger.info(f"Found channel: {channel} (Context: {context}, State: {state})")
                    print(f"FOUND CHANNEL: {channel} (Context: {context}, State: {state})", flush=True)
                    break

        if channel:
            hangup_result = subprocess.run(
                ['asterisk', '-rx', f'channel request hangup {channel}'],
                capture_output=True,
                text=True,
                check=False
            )
    
            logger.info(f"Hangup command: channel request hangup {channel}")
            logger.info(f"Hangup result: {hangup_result.stdout}")
            print(f"Hangup executed on {channel}: {hangup_result.stdout}", flush=True)
        
        # Step 3: Update database
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE call_history 
            SET status = 'hangup', ended_at = CURRENT_TIMESTAMP
            WHERE call_id = ?
        ''', (call_id,))
        conn.commit()
        conn.close()
        
        print(f"Database updated for {call_id}", flush=True)
        
        if deleted_file or channel:
            print("========== HANGUP SUCCESS ==========", flush=True)
            return {"status": "success", "message": "Call hangup requested"}
        else:
            print("========== HANGUP: NO ACTIVE CALL FOUND ==========", flush=True)
            return {"status": "error", "message": "Call not found or already ended"}
            
    except Exception as e:
        error_msg = f"Error hanging up call: {e}"
        logger.error(error_msg)
        print(f"========== HANGUP ERROR: {error_msg} ==========", flush=True)
        raise HTTPException(status_code=500, detail=str(e))
# ============================================================================
# RECORDINGS MANAGEMENT ENDPOINTS
# ============================================================================

@app.get("/api/recordings")
async def list_recordings():
    try:
        recordings = []
        recordings_dir = Path(ASTERISK_SOUNDS)
        
        if recordings_dir.exists():
            for file in recordings_dir.glob('*'):
                if file.is_file() and file.suffix.lower() in ['.wav', '.gsm', '.mp3', '.ulaw']:
                    stat = file.stat()
                    recordings.append({
                        "filename": file.name,
                        "display_name": file.stem,
                        "size": stat.st_size,
                        "size_mb": round(stat.st_size / 1024 / 1024, 2),
                        "format": file.suffix[1:].upper(),
                        "created": datetime.fromtimestamp(stat.st_ctime).isoformat()
                    })
        
        # Sort by creation date, newest first
        recordings.sort(key=lambda x: x['created'], reverse=True)
        
        return {"recordings": recordings}
        
    except Exception as e:
        logger.error(f"Error listing recordings: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Call Records API with clear from/to formatting
@app.get("/api/call_records")
async def get_call_records(limit: int = 50, call_type: str = None, direction: str = None):
    """Get call records with optional filtering and clear from/to display"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        query = """
            SELECT id, uniqueid, caller_id, source_number, destination_number,
                   context, channel, extension, call_type, direction,
                   start_time, answer_time, end_time, duration, billsec,
                   disposition, broadcast_id, created_at, amd_status
            FROM call_records
        """
        conditions = []
        params = []

        if call_type:
            conditions.append("call_type = ?")
            params.append(call_type)

        if direction:
            conditions.append("direction = ?")
            params.append(direction)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY start_time DESC LIMIT ?"
        params.append(limit)

        c.execute(query, params)
        rows = c.fetchall()
        conn.close()

        results = []
        for row in rows:
            (id_, uniqueid, caller_id, source_number, destination_number,
             context, channel, extension, call_type_, direction_,
             start_time, answer_time, end_time, duration, billsec,
             disposition, broadcast_id, created_at, amd_status) = row

            # Format the from/to display based on call direction
            if direction_ == "inbound":
                # Inbound: caller → DID (system)
                from_display = source_number or caller_id or "Unknown"
                to_display = destination_number or "System"
                summary = f"📞 {from_display} → {to_display}"
                if extension:
                    summary += f" ({extension})"
            elif direction_ == "outbound":
                # Outbound: system (caller_id) → destination
                from_display = caller_id or "System"
                to_display = destination_number or "Unknown"
                summary = f"📤 {from_display} → {to_display}"
            elif direction_ == "internal":
                # Internal: extension → extension
                from_display = source_number or "Internal"
                to_display = destination_number or extension or "Unknown"
                summary = f"🔄 {from_display} → {to_display}"
            else:
                from_display = source_number or caller_id or "Unknown"
                to_display = destination_number or "Unknown"
                summary = f"{from_display} → {to_display}"

            # Add disposition indicator - enhanced with AMD detection
            if disposition == "ANSWERED":
                if amd_status == "HUMAN":
                    status_icon = "👤"
                    answer_type = "Human Answered"
                elif amd_status == "MACHINE":
                    status_icon = "📠"
                    answer_type = "Voicemail/Machine"
                elif amd_status == "NOTSURE":
                    status_icon = "❓"
                    answer_type = "Uncertain"
                else:
                    status_icon = "✅"
                    answer_type = "Answered"
            elif disposition == "NO ANSWER":
                status_icon = "📵"
                answer_type = "No Answer"
            elif disposition == "BUSY":
                status_icon = "🔴"
                answer_type = "Busy"
            elif disposition == "FAILED":
                status_icon = "❌"
                answer_type = "Failed"
            else:
                status_icon = "❓"
                answer_type = disposition or "Unknown"

            results.append({
                "id": id_,
                "uniqueid": uniqueid,
                "summary": f"{status_icon} {summary}",
                "from": from_display,
                "to": to_display,
                "caller_id": caller_id,
                "direction": direction_,
                "call_type": call_type_,
                "context": context,
                "extension": extension,
                "start_time": start_time,
                "answer_time": answer_time,
                "end_time": end_time,
                "duration": duration,
                "talk_time": billsec,
                "disposition": disposition,
                "status_icon": status_icon,
                "broadcast_id": broadcast_id,
                "amd_status": amd_status,
                "answer_type": answer_type
            })

        return {"calls": results, "total": len(results)}

    except Exception as e:
        logger.error(f"Error fetching call records: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/recordings/upload")
async def upload_recording(file: UploadFile = File(...)):
    try:
        allowed_extensions = ['.wav', '.mp3', '.gsm']
        file_ext = Path(file.filename).suffix.lower()
        
        if file_ext not in allowed_extensions:
            raise HTTPException(
                status_code=400, 
                detail=f"Invalid file type. Allowed: WAV, MP3, GSM"
            )
        
        # Generate safe filename - replace spaces with underscores
        base_name = Path(file.filename).stem.replace(' ', '_')
        safe_filename = f"{base_name}_{uuid.uuid4().hex[:8]}{file_ext}"
        
        # Save to Asterisk sounds directory
        file_path = os.path.join(ASTERISK_SOUNDS, safe_filename)
        
        # Save uploaded file
        async with aiofiles.open(file_path, 'wb') as f:
            content = await file.read()
            await f.write(content)
        
        # Convert to Asterisk-compatible format (8kHz, 16-bit signed WAV)
        if file_ext in ['.mp3', '.wav']:
            output_path = file_path.replace(file_ext, '_temp.wav')
            result = subprocess.run(
                ['sox', file_path, '-r', '8000', '-c', '1', '-b', '16', '-e', 'signed-integer', output_path],
                capture_output=True,
                check=False
            )
            if result.returncode == 0:
                os.remove(file_path)
                os.rename(output_path, file_path.replace(file_ext, '.wav'))
                safe_filename = safe_filename.replace(file_ext, '.wav')
                file_path = file_path.replace(file_ext, '.wav')
            else:
                logger.error(f"Audio conversion failed: {result.stderr}")
                raise HTTPException(status_code=500, detail="Audio conversion failed")
        
        # Set ownership
        subprocess.run(['chown', 'asterisk:asterisk', file_path], check=False)
        
        logger.info(f"✓ Recording uploaded: {safe_filename}")
        
        return {
            "status": "success",
            "filename": safe_filename,
            "message": "Recording uploaded and converted successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading recording: {e}")
        raise HTTPException(status_code=500, detail=str(e))
@app.post("/api/recordings/rename")
async def rename_recording(old_name: str, new_name: str):
    try:
        old_path = os.path.join(ASTERISK_SOUNDS, old_name)
        
        # Replace spaces with underscores
        new_name = new_name.replace(' ', '_')
        
        # Keep the same extension
        extension = Path(old_name).suffix
        if not new_name.endswith(extension):
            new_name += extension
        
        new_path = os.path.join(ASTERISK_SOUNDS, new_name)  # Changed from RECORDINGS_PATH
        
        if not os.path.exists(old_path):
            raise HTTPException(status_code=404, detail="Recording not found")
        
        if os.path.exists(new_path):
            raise HTTPException(status_code=400, detail="A recording with that name already exists")
        
        os.rename(old_path, new_path)
        logger.info(f"Recording renamed: {old_name} -> {new_name}")
        
        return {"status": "success", "new_name": new_name}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error renaming recording: {e}")
        raise HTTPException(status_code=500, detail=str(e))
@app.delete("/api/recordings/{filename}")
async def delete_recording(filename: str):
    try:
        file_path = os.path.join(ASTERISK_SOUNDS, filename)
        
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="Recording not found")
        
        os.remove(file_path)
        logger.info(f"Recording deleted: {filename}")
        
        return {"status": "success", "message": "Recording deleted"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting recording: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/recordings/play/{filename}")
async def play_recording(filename: str, request: Request):
    """Stream a recording file"""
    try:
        file_path = os.path.join(ASTERISK_SOUNDS, filename)
        
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="Recording not found")
        
        # Get file size
        file_size = os.path.getsize(file_path)
        
        if file_size < 100:
            raise HTTPException(status_code=400, detail="Recording file is empty or corrupted")
        
        # Parse range header for streaming
        range_header = request.headers.get("range")
        
        if range_header:
            # Handle range requests for audio streaming
            range_match = range_header.replace("bytes=", "").split("-")
            start = int(range_match[0]) if range_match[0] else 0
            end = int(range_match[1]) if len(range_match) > 1 and range_match[1] else file_size - 1
            
            chunk_size = end - start + 1
            
            def iterfile():
                with open(file_path, "rb") as f:
                    f.seek(start)
                    yield f.read(chunk_size)
            
            headers = {
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(chunk_size),
                "Content-Type": "audio/wav",
            }
            
            return StreamingResponse(iterfile(), status_code=206, headers=headers)
        
        # No range header - send entire file
        return FileResponse(
            file_path,
            media_type="audio/wav",
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size)
            }
        )
        
    except Exception as e:
        logger.error(f"Error playing recording: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/recordings/download/{filename}")
async def download_recording(filename: str):
    """Download a recording file"""
    try:
        file_path = os.path.join(ASTERISK_SOUNDS, filename)

        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="Recording not found")

        file_size = os.path.getsize(file_path)

        if file_size < 100:
            raise HTTPException(status_code=400, detail="Recording file is empty or corrupted")

        # Determine media type based on file extension
        ext = os.path.splitext(filename)[1].lower()
        media_types = {
            '.wav': 'audio/wav',
            '.mp3': 'audio/mpeg',
            '.gsm': 'audio/x-gsm',
            '.ulaw': 'audio/basic'
        }
        media_type = media_types.get(ext, 'application/octet-stream')

        return FileResponse(
            file_path,
            media_type=media_type,
            filename=filename,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(file_size)
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error downloading recording: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/recordings/register")
async def register_recording(filename: str, recording_id: str):
    """Register a recording created via phone system"""
    try:
        file_path = os.path.join(ASTERISK_SOUNDS, filename)
        
        # Wait for file to be written
        import time
        for i in range(10):
            if os.path.exists(file_path):
                break
            time.sleep(0.5)
        
        if not os.path.exists(file_path):
            logger.warning(f"Recording file not found: {file_path}")
            return {"status": "pending", "message": "Recording will be available shortly"}
        
        # Ensure file is not empty
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            logger.error(f"Recording file is empty: {file_path}")
            return {"status": "error", "message": "Recording file is empty"}
        
        # Get file info
        created_time = datetime.fromtimestamp(os.path.getctime(file_path))
        
        logger.info(f"✓ Registered recording: {filename} (ID: {recording_id}, Size: {file_size} bytes)")
        
        return {
            "status": "success",
            "filename": filename,
            "recording_id": recording_id,
            "size": file_size,
            "created": created_time.isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error registering recording: {e}")
        return {"status": "error", "message": str(e)}

# ============================================================================
# SCHEDULED CALLS ENDPOINTS
# ============================================================================

@app.get("/api/scheduled")
async def get_scheduled_calls():
    """Get all scheduled calls"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, name, phone_number, group_name, message, tts_text,
                   recording_file, caller_id, scheduled_time, repeat_type,
                   repeat_days, enabled, last_run, next_run, status, created_at,
                   is_broadcast, concurrent_calls, pre_message_delay, max_ring_time
            FROM scheduled_calls
            ORDER BY scheduled_time ASC
        ''')
        rows = cursor.fetchall()
        conn.close()

        scheduled = []
        for row in rows:
            scheduled.append({
                "id": row[0],
                "name": row[1],
                "phone_number": row[2],
                "group_name": row[3],
                "message": row[4],
                "tts_text": row[5],
                "recording_file": row[6],
                "caller_id": row[7],
                "scheduled_time": row[8],
                "repeat_type": row[9],
                "repeat_days": row[10],
                "enabled": bool(row[11]),
                "last_run": row[12],
                "next_run": row[13],
                "status": row[14],
                "created_at": row[15],
                "is_broadcast": row[16] if len(row) > 16 else 0,
                "concurrent_calls": row[17] if len(row) > 17 else 5,
                "pre_message_delay": row[18] if len(row) > 18 else 1,
                "max_ring_time": row[19] if len(row) > 19 else 45
            })

        return {"scheduled": scheduled}

    except Exception as e:
        logger.error(f"Error getting scheduled calls: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/scheduled")
async def create_scheduled_call(schedule: ScheduledCall):
    """Create a new scheduled call"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO scheduled_calls
            (name, phone_number, group_name, message, tts_text, recording_file,
             caller_id, scheduled_time, repeat_type, repeat_days, enabled, status, next_run,
             is_broadcast, concurrent_calls, pre_message_delay, max_ring_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
        ''', (
            schedule.name,
            schedule.phone_number,
            schedule.group_name,
            schedule.message,
            schedule.tts_text,
            schedule.recording_file,
            schedule.caller_id,
            schedule.scheduled_time,
            schedule.repeat_type,
            schedule.repeat_days,
            1 if schedule.enabled else 0,
            schedule.scheduled_time,
            1 if schedule.is_broadcast else 0,
            schedule.concurrent_calls,
            schedule.pre_message_delay,
            schedule.max_ring_time
        ))

        schedule_id = cursor.lastrowid
        conn.commit()
        conn.close()

        logger.info(f"✅ Scheduled {'broadcast' if schedule.is_broadcast else 'call'} created: {schedule.name} for {schedule.scheduled_time}")
        return {
            "status": "success",
            "id": schedule_id,
            "name": schedule.name,
            "scheduled_time": schedule.scheduled_time,
            "is_broadcast": schedule.is_broadcast
        }

    except Exception as e:
        logger.error(f"Error creating scheduled call: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/scheduled/{schedule_id}")
async def get_scheduled_call(schedule_id: int):
    """Get a specific scheduled call"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, name, phone_number, group_name, message, tts_text,
                   recording_file, caller_id, scheduled_time, repeat_type,
                   repeat_days, enabled, last_run, next_run, status, created_at
            FROM scheduled_calls
            WHERE id = ?
        ''', (schedule_id,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            raise HTTPException(status_code=404, detail="Scheduled call not found")

        return {
            "id": row[0],
            "name": row[1],
            "phone_number": row[2],
            "group_name": row[3],
            "message": row[4],
            "tts_text": row[5],
            "recording_file": row[6],
            "caller_id": row[7],
            "scheduled_time": row[8],
            "repeat_type": row[9],
            "repeat_days": row[10],
            "enabled": bool(row[11]),
            "last_run": row[12],
            "next_run": row[13],
            "status": row[14],
            "created_at": row[15]
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting scheduled call: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/scheduled/{schedule_id}")
async def update_scheduled_call(schedule_id: int, schedule: ScheduledCall):
    """Update a scheduled call"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE scheduled_calls
            SET name = ?, phone_number = ?, group_name = ?, message = ?,
                tts_text = ?, recording_file = ?, caller_id = ?, scheduled_time = ?,
                repeat_type = ?, repeat_days = ?, enabled = ?, next_run = ?,
                status = CASE WHEN status = 'completed' THEN 'pending' ELSE status END
            WHERE id = ?
        ''', (
            schedule.name,
            schedule.phone_number,
            schedule.group_name,
            schedule.message,
            schedule.tts_text,
            schedule.recording_file,
            schedule.caller_id,
            schedule.scheduled_time,
            schedule.repeat_type,
            schedule.repeat_days,
            1 if schedule.enabled else 0,
            schedule.scheduled_time,
            schedule_id
        ))

        if cursor.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail="Scheduled call not found")

        conn.commit()
        conn.close()

        logger.info(f"✅ Scheduled call updated: {schedule.name}")
        return {"status": "success", "id": schedule_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating scheduled call: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/scheduled/{schedule_id}")
async def delete_scheduled_call(schedule_id: int):
    """Delete a scheduled call"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('DELETE FROM scheduled_calls WHERE id = ?', (schedule_id,))

        if cursor.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail="Scheduled call not found")

        conn.commit()
        conn.close()

        logger.info(f"✅ Scheduled call deleted: {schedule_id}")
        return {"status": "success", "id": schedule_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting scheduled call: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/scheduled/{schedule_id}/toggle")
async def toggle_scheduled_call(schedule_id: int):
    """Enable/disable a scheduled call"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE scheduled_calls
            SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END
            WHERE id = ?
        ''', (schedule_id,))

        if cursor.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail="Scheduled call not found")

        # Get new status
        cursor.execute('SELECT enabled FROM scheduled_calls WHERE id = ?', (schedule_id,))
        enabled = cursor.fetchone()[0]

        conn.commit()
        conn.close()

        return {"status": "success", "id": schedule_id, "enabled": bool(enabled)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling scheduled call: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# SETTINGS ENDPOINTS
# ============================================================================

@app.get("/api/settings")
async def get_all_settings():
    """Get all system settings"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT key, value, updated_at FROM settings')
        rows = cursor.fetchall()
        conn.close()

        settings = {row[0]: {"value": row[1], "updated_at": row[2]} for row in rows}
        return {"settings": settings}

    except Exception as e:
        logger.error(f"Error getting settings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/settings/{key}")
async def get_setting(key: str):
    """Get a specific setting"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT value, updated_at FROM settings WHERE key = ?', (key,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            raise HTTPException(status_code=404, detail=f"Setting '{key}' not found")

        return {"key": key, "value": row[0], "updated_at": row[1]}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting setting {key}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/settings/{key}")
async def update_setting(key: str, request: dict):
    """Update a setting"""
    try:
        value = request.get("value")
        if value is None:
            raise HTTPException(status_code=400, detail="Value is required")

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
        ''', (key, str(value)))

        conn.commit()
        conn.close()

        logger.info(f"✅ Setting updated: {key} = {value}")
        return {"status": "success", "key": key, "value": value}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating setting {key}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# CALL SETTINGS ENDPOINTS
# ============================================================================

@app.get("/api/settings/call")
async def get_call_settings():
    """Get call settings including AMD and default caller ID"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get AMD enabled setting
        cursor.execute('SELECT value FROM settings WHERE key = ?', ('amd_enabled',))
        amd_row = cursor.fetchone()
        amd_enabled = amd_row[0].lower() == 'true' if amd_row else True  # Default to True

        # Get default caller ID from DIDs
        cursor.execute('SELECT phone_number FROM dids WHERE is_default = 1 LIMIT 1')
        did_row = cursor.fetchone()
        default_caller_id = did_row[0] if did_row else None

        conn.close()

        return {
            "amd_enabled": amd_enabled,
            "default_caller_id": default_caller_id
        }

    except Exception as e:
        logger.error(f"Error getting call settings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/settings/call")
async def update_call_settings(request: dict):
    """Update call settings"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Update AMD setting if provided
        if 'amd_enabled' in request:
            amd_value = 'true' if request['amd_enabled'] else 'false'
            cursor.execute('''
                INSERT INTO settings (key, value, updated_at)
                VALUES ('amd_enabled', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
            ''', (amd_value,))
            logger.info(f"✅ AMD enabled set to: {amd_value}")

        conn.commit()
        conn.close()

        return {"status": "success"}

    except Exception as e:
        logger.error(f"Error updating call settings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def is_amd_enabled():
    """Check if AMD is enabled in settings"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM settings WHERE key = ?', ('amd_enabled',))
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0].lower() == 'true'
        return True  # Default to enabled
    except:
        return True  # Default to enabled on error


# ============================================================================
# DID MANAGEMENT ENDPOINTS
# ============================================================================

@app.get("/api/dids")
async def list_dids():
    """Get all DIDs"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id, phone_number, name, description, is_default, created_at FROM dids ORDER BY is_default DESC, name ASC')
        rows = cursor.fetchall()
        conn.close()

        dids = []
        for row in rows:
            dids.append({
                "id": row[0],
                "phone_number": row[1],
                "name": row[2],
                "description": row[3],
                "is_default": bool(row[4]),
                "created_at": row[5]
            })

        return {"dids": dids}

    except Exception as e:
        logger.error(f"Error listing DIDs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/dids")
async def add_did(request: dict):
    """Add a new DID"""
    try:
        phone_number = request.get("phone_number", "").strip()
        name = request.get("name", "").strip()
        description = request.get("description", "").strip()
        is_default = request.get("is_default", False)

        if not phone_number:
            raise HTTPException(status_code=400, detail="Phone number is required")

        conn = get_db_connection()
        cursor = conn.cursor()

        # If this is set as default, unset other defaults first
        if is_default:
            cursor.execute('UPDATE dids SET is_default = 0')

        cursor.execute('''
            INSERT INTO dids (phone_number, name, description, is_default)
            VALUES (?, ?, ?, ?)
        ''', (phone_number, name or None, description or None, 1 if is_default else 0))

        did_id = cursor.lastrowid
        conn.commit()
        conn.close()

        logger.info(f"✅ DID added: {phone_number} ({name})")
        return {"status": "success", "id": did_id, "phone_number": phone_number}

    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="This phone number already exists")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding DID: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/dids/{did_id}")
async def update_did(did_id: int, request: dict):
    """Update a DID"""
    try:
        phone_number = request.get("phone_number", "").strip()
        name = request.get("name", "").strip()
        description = request.get("description", "").strip()
        is_default = request.get("is_default", False)

        if not phone_number:
            raise HTTPException(status_code=400, detail="Phone number is required")

        conn = get_db_connection()
        cursor = conn.cursor()

        # If this is set as default, unset other defaults first
        if is_default:
            cursor.execute('UPDATE dids SET is_default = 0')

        cursor.execute('''
            UPDATE dids SET phone_number = ?, name = ?, description = ?, is_default = ?
            WHERE id = ?
        ''', (phone_number, name or None, description or None, 1 if is_default else 0, did_id))

        if cursor.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail="DID not found")

        conn.commit()
        conn.close()

        logger.info(f"✅ DID updated: {phone_number}")
        return {"status": "success", "id": did_id}

    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="This phone number already exists")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating DID: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/dids/{did_id}")
async def delete_did(did_id: int):
    """Delete a DID"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('DELETE FROM dids WHERE id = ?', (did_id,))

        if cursor.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail="DID not found")

        conn.commit()
        conn.close()

        logger.info(f"✅ DID deleted: {did_id}")
        return {"status": "success"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting DID: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/dids/{did_id}/set-default")
async def set_default_did(did_id: int):
    """Set a DID as the default"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Unset all defaults
        cursor.execute('UPDATE dids SET is_default = 0')

        # Set this one as default
        cursor.execute('UPDATE dids SET is_default = 1 WHERE id = ?', (did_id,))

        if cursor.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail="DID not found")

        conn.commit()
        conn.close()

        logger.info(f"✅ Default DID set: {did_id}")
        return {"status": "success"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting default DID: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# SIP TRUNK STATUS
# ============================================================================

@app.get("/api/sip/status")
async def get_sip_status():
    """Get SIP trunk registration status"""
    try:
        # Check SIP registrations using Asterisk CLI
        result = subprocess.run(
            ['asterisk', '-rx', 'sip show registry'],
            capture_output=True,
            text=True,
            timeout=10
        )

        registrations = []
        lines = result.stdout.strip().split('\n')

        for line in lines:
            if 'trunk' in line.lower() or 'sip.' in line.lower() or ':5060' in line:
                parts = line.split()
                if len(parts) >= 5:
                    registrations.append({
                        "host": parts[0] if parts else "Unknown",
                        "username": parts[2] if len(parts) > 2 else "Unknown",
                        "state": parts[-1] if parts else "Unknown"
                    })

        # Check SIP peers
        result_peers = subprocess.run(
            ['asterisk', '-rx', 'sip show peers'],
            capture_output=True,
            text=True,
            timeout=10
        )

        peers = []
        peer_lines = result_peers.stdout.strip().split('\n')
        for line in peer_lines:
            if 'trunk_main' in line.lower():
                parts = line.split()
                if len(parts) >= 3:
                    peers.append({
                        "name": parts[0],
                        "host": parts[1] if len(parts) > 1 else "Unknown",
                        "status": parts[-1] if parts else "Unknown"
                    })

        # Determine overall status
        is_registered = any('Registered' in str(r.get('state', '')) for r in registrations)
        is_reachable = any('OK' in str(p.get('status', '')) for p in peers)

        return {
            "status": "connected" if is_registered else "disconnected",
            "registered": is_registered,
            "reachable": is_reachable,
            "registrations": registrations,
            "peers": peers,
            "raw_registry": result.stdout,
            "raw_peers": result_peers.stdout
        }

    except subprocess.TimeoutExpired:
        logger.error("Asterisk CLI timed out")
        return {"status": "error", "message": "Asterisk not responding"}
    except Exception as e:
        logger.error(f"Error getting SIP status: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/api/sip/reload")
async def reload_sip():
    """Reload SIP configuration to re-register trunks"""
    try:
        # Reload SIP module
        result = subprocess.run(
            ['asterisk', '-rx', 'sip reload'],
            capture_output=True,
            text=True,
            timeout=30
        )

        logger.info("🔄 SIP configuration reloaded")

        # Wait a moment for registration
        await asyncio.sleep(3)

        # Check new status
        status = await get_sip_status()

        return {
            "status": "success",
            "message": "SIP configuration reloaded",
            "sip_status": status
        }

    except Exception as e:
        logger.error(f"Error reloading SIP: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# BACKUP AND RESTORE
# ============================================================================

@app.get("/api/backup")
async def create_backup():
    """Export entire system configuration as a downloadable JSON backup"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        backup_data = {
            "version": "1.0",
            "created_at": datetime.now().isoformat(),
            "tables": {}
        }

        # List of tables to backup
        tables_to_backup = [
            "call_history",
            "broadcasts",
            "contact_groups",
            "group_members",
            "scheduled_calls",
            "dids",
            "settings",
            "broadcast_callbacks"
        ]

        for table in tables_to_backup:
            try:
                cursor.execute(f"SELECT * FROM {table}")
                rows = cursor.fetchall()
                backup_data["tables"][table] = [dict(row) for row in rows]
                logger.info(f"📦 Backed up {len(rows)} rows from {table}")
            except sqlite3.OperationalError as e:
                logger.warning(f"Table {table} not found or error: {e}")
                backup_data["tables"][table] = []

        conn.close()

        # Convert to JSON
        backup_json = json.dumps(backup_data, indent=2, default=str)

        # Create filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"phone_system_backup_{timestamp}.json"

        logger.info(f"✅ Backup created: {filename}")

        return Response(
            content=backup_json,
            media_type="application/json",
            headers={
                "Content-Disposition": f"attachment; filename={filename}"
            }
        )

    except Exception as e:
        logger.error(f"Error creating backup: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/restore")
async def restore_backup(file: UploadFile = File(...)):
    """Restore system configuration from a backup file"""
    try:
        # Read and parse the backup file
        content = await file.read()
        try:
            backup_data = json.loads(content.decode('utf-8'))
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid backup file format")

        # Validate backup structure
        if "version" not in backup_data or "tables" not in backup_data:
            raise HTTPException(status_code=400, detail="Invalid backup file structure")

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        restored_counts = {}

        # Restore each table
        for table_name, rows in backup_data["tables"].items():
            if not rows:
                restored_counts[table_name] = 0
                continue

            try:
                # Get column names from the first row
                columns = list(rows[0].keys())

                # Clear existing data (except for some tables we want to merge)
                if table_name not in ["call_history"]:  # Don't clear call history
                    cursor.execute(f"DELETE FROM {table_name}")

                # Insert rows
                placeholders = ",".join(["?" for _ in columns])
                column_names = ",".join(columns)

                for row in rows:
                    values = [row.get(col) for col in columns]
                    try:
                        cursor.execute(
                            f"INSERT OR REPLACE INTO {table_name} ({column_names}) VALUES ({placeholders})",
                            values
                        )
                    except sqlite3.Error as e:
                        logger.warning(f"Error inserting row into {table_name}: {e}")

                restored_counts[table_name] = len(rows)
                logger.info(f"📥 Restored {len(rows)} rows to {table_name}")

            except sqlite3.OperationalError as e:
                logger.warning(f"Error restoring table {table_name}: {e}")
                restored_counts[table_name] = 0

        conn.commit()
        conn.close()

        logger.info(f"✅ Backup restored successfully from {file.filename}")

        return {
            "status": "success",
            "message": "Backup restored successfully",
            "restored": restored_counts,
            "backup_date": backup_data.get("created_at", "Unknown")
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error restoring backup: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# SECURITY ENDPOINTS
# ============================================================================

@app.get("/api/security/status")
async def get_security_status():
    """Get fail2ban status and banned IPs"""
    try:
        # Check if fail2ban is running
        result = subprocess.run(
            ['pgrep', '-x', 'fail2ban-server'],
            capture_output=True,
            text=True,
            check=False
        )
        fail2ban_running = result.returncode == 0

        banned_ips = []
        if fail2ban_running:
            # Get banned IPs from fail2ban
            result = subprocess.run(
                ['fail2ban-client', 'status', 'asterisk'],
                capture_output=True,
                text=True,
                check=False
            )
            if result.returncode == 0:
                # Parse banned IPs from output
                for line in result.stdout.split('\n'):
                    if 'Banned IP list:' in line:
                        ips = line.split(':')[1].strip()
                        if ips:
                            banned_ips = [ip.strip() for ip in ips.split()]

        # Get iptables blocked IPs
        result = subprocess.run(
            ['iptables', '-L', 'INPUT', '-n'],
            capture_output=True,
            text=True,
            check=False
        )
        iptables_blocked = []
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'DROP' in line and 'all' in line:
                    parts = line.split()
                    for part in parts:
                        if '.' in part and part[0].isdigit():
                            iptables_blocked.append(part)
                            break

        return {
            "fail2ban_running": fail2ban_running,
            "banned_ips": banned_ips,
            "iptables_blocked": iptables_blocked,
            "total_blocked": len(set(banned_ips + iptables_blocked))
        }

    except Exception as e:
        logger.error(f"Error getting security status: {e}")
        return {"fail2ban_running": False, "banned_ips": [], "error": str(e)}


@app.post("/api/security/block/{ip}")
async def block_ip(ip: str):
    """Manually block an IP address"""
    try:
        # Validate IP format (basic check)
        import re
        if not re.match(r'^[\d./]+$', ip):
            raise HTTPException(status_code=400, detail="Invalid IP address format")

        # Add to iptables
        result = subprocess.run(
            ['iptables', '-A', 'INPUT', '-s', ip, '-j', 'DROP'],
            capture_output=True,
            text=True,
            check=False
        )

        if result.returncode == 0:
            logger.info(f"🚫 Blocked IP: {ip}")
            return {"status": "success", "message": f"IP {ip} blocked"}
        else:
            raise HTTPException(status_code=500, detail=f"Failed to block IP: {result.stderr}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error blocking IP: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/security/unblock/{ip}")
async def unblock_ip(ip: str):
    """Unblock an IP address"""
    try:
        # Remove from iptables
        result = subprocess.run(
            ['iptables', '-D', 'INPUT', '-s', ip, '-j', 'DROP'],
            capture_output=True,
            text=True,
            check=False
        )

        # Also try to unban from fail2ban
        subprocess.run(
            ['fail2ban-client', 'set', 'asterisk', 'unbanip', ip],
            capture_output=True,
            check=False
        )

        logger.info(f"✅ Unblocked IP: {ip}")
        return {"status": "success", "message": f"IP {ip} unblocked"}

    except Exception as e:
        logger.error(f"Error unblocking IP: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/security/attacks")
async def get_recent_attacks():
    """Get recent attack attempts from Asterisk logs"""
    try:
        attacks = []
        log_file = "/var/log/asterisk/messages"

        if os.path.exists(log_file):
            result = subprocess.run(
                ['tail', '-n', '500', log_file],
                capture_output=True,
                text=True,
                check=False
            )

            if result.returncode == 0:
                import re
                # Pattern to match failed auth and registration attempts
                patterns = [
                    (r'Registration from .* failed for \'(\d+\.\d+\.\d+\.\d+)', 'Registration Failed'),
                    (r'Failed to authenticate.*\((\d+\.\d+\.\d+\.\d+)', 'Auth Failed'),
                    (r'Call from .* \((\d+\.\d+\.\d+\.\d+):\d+\) to extension .* rejected', 'Call Rejected'),
                ]

                seen = set()
                for line in result.stdout.split('\n'):
                    for pattern, attack_type in patterns:
                        match = re.search(pattern, line)
                        if match:
                            ip = match.group(1)
                            key = f"{ip}:{attack_type}"
                            if key not in seen:
                                seen.add(key)
                                # Extract timestamp if available
                                time_match = re.match(r'\[([^\]]+)\]', line)
                                timestamp = time_match.group(1) if time_match else "Unknown"
                                attacks.append({
                                    "ip": ip,
                                    "type": attack_type,
                                    "timestamp": timestamp,
                                    "raw": line[:200]
                                })

        # Sort by timestamp (most recent first) and limit
        attacks = attacks[-50:]
        attacks.reverse()

        return {"attacks": attacks, "count": len(attacks)}

    except Exception as e:
        logger.error(f"Error getting attacks: {e}")
        return {"attacks": [], "error": str(e)}


# ============================================================================
# START SERVER
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8088, log_level="info")
