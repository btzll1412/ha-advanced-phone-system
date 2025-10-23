#!/usr/bin/env python3
"""
Advanced Phone System - Main API Service
Handles calls, broadcasts, and Home Assistant integration
"""
import csv
from datetime import datetime
from typing import List, Dict
import asyncio
import os
import json
import uuid
import logging
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import aiofiles
import pwd
import grp
import hashlib
from gtts import gTTS

from enum import Enum

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
    """Generate Asterisk channel string"""
    if direction == CallDirection.INTERNAL:
        return f"PJSIP/{number}"
    else:
        # Remove + for trunk routing
        clean = number.lstrip('+')
        return f"PJSIP/{clean}@trunk_main"

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI
app = FastAPI(title="Advanced Phone System API", version="1.1.0")

@app.get("/", response_class=HTMLResponse)
async def serve_web_ui():
    """Serve the web UI"""
    return FileResponse("/app/web/index.html")

@app.get("/api")
async def api_info():
    """API information endpoint"""
    return {
        "service": "Advanced Phone System API",
        "version": "1.1.0",
        "status": "running"
    }

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
            completed_at TIMESTAMP
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
        lastapp = row[7]
        lastdata = row[8]
        start = row[9]
        answer = row[10]
        end = row[11]
        duration = row[12]
        billsec = row[13]
        disposition = row[14]
        uniqueid = row[16]
        
        # Skip if uniqueid is empty
        if not uniqueid or uniqueid == '':
            return
        
        # Extract caller_id from clid field
        caller_id = ""
        if "<" in clid and ">" in clid:
            caller_id = clid.split("<")[1].split(">")[0]
        
        # Determine call type and direction
        call_type = "unknown"
        direction = "unknown"
        extension = None
        
        # Determine based on context
        if context == "outbound-playback":
            call_type = "outbound"
            direction = "outbound"
            # Extract actual destination from channel: SIP/trunk_main/18455021412
            if "trunk_main/" in channel:
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
            call_type = "inbound"
            direction = "inbound"
        elif context == "broadcast":
            call_type = "broadcast"
            direction = "outbound"
        
        # Parse extension from dst_channel
        if "SIP/" in dst_channel:
            try:
                extension = dst_channel.split("/")[1].split("-")[0]
            except:
                pass
        
        # Parse extension from channel if not found
        if not extension and "SIP/" in channel:
            try:
                extension = channel.split("/")[1].split("-")[0]
                # Check if it looks like an extension (3 digits starting with 1)
                if not (extension.startswith("1") and len(extension) == 3):
                    if extension == "trunk_main":
                        extension = None  # This is the trunk, not an extension
            except:
                pass
        
        # Convert to integers
        try:
            duration_int = int(float(duration))
            billsec_int = int(float(billsec))
        except:
            duration_int = 0
            billsec_int = 0
        
        # Insert into database
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        c.execute('''
            INSERT OR IGNORE INTO call_records 
            (uniqueid, caller_id, source_number, destination_number, context, 
             channel, destination_channel, extension, call_type, direction,
             start_time, answer_time, end_time, duration, billsec, disposition,
             broadcast_id, recording_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (uniqueid, caller_id, source, dest, context, channel, dst_channel,
              extension, call_type, direction, start, answer, end, 
              duration_int, billsec_int, disposition, None, None))
        
        conn.commit()
        conn.close()
        
        logger.info(f"📞 Recorded call: {source} → {dest} ({call_type}) - Duration: {duration_int}s, Talk: {billsec_int}s, Status: {disposition}")
        
    except Exception as e:
        logger.error(f"Error processing CDR: {e}")
        logger.error(f"Row data: {row}")

async def record_call_to_db(call_data: dict):
    """Record call with direction tracking - FIXED to match actual schema"""
    try:
        call_id = call_data.get('UniqueID', str(uuid.uuid4()))
        source = call_data.get('Source', 'Unknown')
        destination = call_data.get('Destination', 'Unknown')
        duration = call_data.get('Duration', 0)
        billsec = call_data.get('BillableSeconds', 0)
        disposition = call_data.get('Disposition', 'UNKNOWN')
        direction = call_data.get('Direction', 'unknown')
        
        # Auto-detect direction if not provided
        if direction == 'unknown':
            if len(destination.replace('+', '')) <= 4:
                direction = 'internal'
            elif source.startswith('PJSIP/trunk') or source.startswith('SIP/trunk'):
                direction = 'inbound'
            else:
                direction = 'outbound'
        
        logger.info(f"📞 CDR Data - Source: {source}, Destination: {destination}, Direction: {direction}, Duration: {duration}s")
        
        # ✅ Use correct column names that match the actual database schema
        async with aiosqlite.connect(DB_PATH) as db:
            # Check if call already exists (from initial save_call_to_db)
            cursor = await db.execute(
                "SELECT id FROM call_history WHERE call_id = ?", 
                (call_id,)
            )
            existing = await cursor.fetchone()
            
            if existing:
                # Update existing call with CDR data (duration and final status)
                await db.execute("""
                    UPDATE call_history 
                    SET duration = ?, status = ?, ended_at = CURRENT_TIMESTAMP
                    WHERE call_id = ?
                """, (duration, disposition.lower(), call_id))
                logger.info(f"✅ Updated call {call_id}: Duration={duration}s, Status={disposition}")
            else:
                # Call doesn't exist yet - shouldn't happen, but handle it
                logger.warning(f"⚠️ Call {call_id} not in DB yet, inserting from CDR")
                await db.execute("""
                    INSERT INTO call_history 
                    (call_id, phone_number, direction, status, duration)
                    VALUES (?, ?, ?, ?, ?)
                """, (call_id, destination, direction, disposition.lower(), duration))
                logger.info(f"📞 Inserted call {call_id} from CDR")
            
            await db.commit()
        
    except Exception as e:
        logger.error(f"Failed to record call: {e}")
        import traceback
        logger.error(traceback.format_exc())


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
    
    migrate_database_v2()  # ← ADD THIS LINE
    logger.info("✓ Database migrated v2")
    
    asyncio.create_task(monitor_cdr())
    logger.info("✓ CDR monitoring task started")
    
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
            except:
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
    
    # Get phone numbers
    phone_numbers = request.phone_numbers or []
    
    if request.group_name:
        # Load from group
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
    
    # Update status
    cursor.execute('UPDATE broadcasts SET status = ? WHERE broadcast_id = ?',
                  ('processing', broadcast_id))
    conn.commit()
    
    # Process calls with concurrency control
    semaphore = asyncio.Semaphore(request.concurrent_calls)
    
    async def make_call(phone_number: str):
        async with semaphore:
            try:
                call_id = create_call_file(
                    phone_number, 
                    audio_file, 
                    request.caller_id,
                    call_id=f"{broadcast_id}_{uuid.uuid4().hex[:8]}",
                    pre_message_delay=request.pre_message_delay,
                    max_ring_time=request.max_ring_time
                )
                
                save_call_to_db(
                    call_id, 
                    phone_number, 
                    audio_file, 
                    request.caller_id,
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

@app.on_event("startup")
async def startup_event():
    """Initialize on startup"""
    init_database()
    logger.info("✓ Database initialized")
    
    migrate_database()
    logger.info("✓ Database migrated")
    
    asyncio.create_task(monitor_cdr())
    logger.info("✓ CDR monitoring task started")
    
    os.makedirs(RECORDINGS_PATH, exist_ok=True)
    os.makedirs(ASTERISK_SOUNDS, exist_ok=True)
    logger.info("✓ API Service started")


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

import re
from enum import Enum

class CallDirection(str, Enum):
    INTERNAL = "internal"
    OUTBOUND = "outbound"
    INBOUND = "inbound"


def detect_call_type(number: str) -> tuple:
    """Detect if number is internal or external"""
    digits = ''.join(filter(str.isdigit, number))
    
    # Internal: 3-4 digits
    if len(digits) <= 4:
        logger.info(f"🏠 INTERNAL call to extension: {digits}")
        return (CallDirection.INTERNAL, digits)
    
    # External: 10+ digits
    elif len(digits) >= 10:
        if len(digits) == 10:
            formatted = f'+1{digits}'
        elif len(digits) == 11:
            formatted = f'+{digits}'
        else:
            formatted = f'+{digits}'
        
        logger.info(f"📤 OUTBOUND call to: {formatted}")
        return (CallDirection.OUTBOUND, formatted)
    
    else:
        raise ValueError(f"Invalid number: {number}")


def get_channel_string(direction: CallDirection, number: str) -> str:
    """Generate Asterisk channel string for chan_sip"""
    if direction == CallDirection.INTERNAL:
        # Internal: SIP/extension
        return f"SIP/{number}"
    else:
        # External: SIP/trunk_main/number
        clean = number.lstrip('+')
        return f"SIP/trunk_main/{clean}"


# Update your /api/call endpoint:
@app.post("/api/call")
async def make_call(request: Request):
    try:
        data = await request.json()
        phone_number = data.get('phone_number', '')
        message = data.get('message', '')  # Might be empty!
        custom_caller_id = data.get('caller_id', None)
        
        logger.info(f"📞 Call request: {phone_number}")
        
        # ✅ Validate and provide default message
        if not message or message.strip() == '':
            message = "This is a test call from your phone system"
            logger.warning(f"⚠️ No message provided, using default")
        
        logger.info(f"📝 Message: {message[:100]}")
        
        # Detect call type
        direction, formatted_number = detect_call_type(phone_number)
        
        # Generate TTS
        audio_file = await generate_tts(message)
        
        # ✅ Validate audio file
        if not audio_file:
            logger.error("❌ TTS returned None, using beep")
            audio_file = "beep"
        
        logger.info(f"🔊 Using audio file: {audio_file}")
        
        call_id = str(uuid.uuid4())
        
        
        # ✅ FLEXIBLE CALLER ID LOGIC
        # Priority: 1. Custom from request, 2. Environment, 3. Config file, 4. Fallback
        if custom_caller_id:
            # Use caller ID from API request
            caller_number = custom_caller_id
            logger.info(f"🆔 Using CUSTOM Caller ID from request: {caller_number}")
        else:
            # Use default from config
            caller_number = os.getenv('CALLER_NUMBER', '')
            
            # If not in env, try to get from add-on config
            if not caller_number:
                try:
                    import json
                    with open('/data/options.json', 'r') as f:
                        options = json.load(f)
                        caller_number = options.get('sip_trunk', {}).get('caller_number', '')
                except:
                    pass
            
            logger.info(f"🆔 Using DEFAULT Caller ID from config: {caller_number}")
        
        # Ensure E.164 format
        if caller_number and not caller_number.startswith('+'):
            caller_number = f'+{caller_number}'
        
        # Build channel based on direction
        channel = get_channel_string(direction, formatted_number)
        
        # ✅ Set caller ID
        if direction == CallDirection.INTERNAL:
            callerid_line = f'CallerID: "Internal" <100>'
        else:
            # For external calls, use the determined caller ID
            if not caller_number:
                logger.warning("⚠️ No caller ID configured! Using fallback")
                caller_number = '+18455021412'  # Fallback - replace with your DID
            
            callerid_line = f'CallerID: {caller_number}'
        
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
Setvar: CUSTOM_CALLERID={caller_number}
"""
        
        logger.info(f"📡 Channel: {channel}")
        logger.info(f"📍 Direction: {direction}")
        logger.info(f"📞 Final Caller ID: {caller_number}")
        
        # Write call file
        temp_file = f"/tmp/{call_id}.call"
        final_file = f"/var/spool/asterisk/outgoing/{call_id}.call"
        
        with open(temp_file, 'w') as f:
            f.write(call_file_content)
        
        os.chmod(temp_file, 0o777)
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

class ContactMember(BaseModel):
    name: str
    phone_number: str

class ContactGroup(BaseModel):
    name: str
    contacts: List[ContactMember]  # Changed from phone_numbers
    caller_id: Optional[str] = None

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

# ADD THE NEW ENDPOINT HERE (right after the recordings endpoint)
@app.get("/api/call_records")
async def get_call_records(limit: int = 50, call_type: str = None):
    """Get call records with optional filtering"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        query = "SELECT * FROM call_records"
        params = []
        
        if call_type:
            query += " WHERE call_type = ?"
            params.append(call_type)
        
        query += " ORDER BY start_time DESC LIMIT ?"
        params.append(limit)
        
        c.execute(query, params)
        columns = [description[0] for description in c.description]
        results = [dict(zip(columns, row)) for row in c.fetchall()]
        
        conn.close()
        return results
        
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
# START SERVER
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8088, log_level="info")
