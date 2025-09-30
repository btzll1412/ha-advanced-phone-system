#!/usr/bin/env python3
"""
Advanced Phone System - Main API Service
Handles calls, broadcasts, and Home Assistant integration
"""

import asyncio
import os
import json
import uuid
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import aiofiles

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize FastAPI
app = FastAPI(title="Advanced Phone System API", version="1.0.0")

# Configuration paths
CONFIG_FILE = "/data/options.json"
DB_PATH = "/data/database/phone_system.db"
ASTERISK_SPOOL = "/var/spool/asterisk/outgoing"
RECORDINGS_PATH = "/data/recordings"
ASTERISK_SOUNDS = "/var/lib/asterisk/sounds/custom"

# Home Assistant API
HA_URL = os.getenv("SUPERVISOR_TOKEN") and "http://supervisor/core/api" or "http://homeassistant:8123/api"
HA_TOKEN = os.getenv("SUPERVISOR_TOKEN", "")

# ============================================================================
# DATABASE SETUP
# ============================================================================

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
    
    # Group members table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS group_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            phone_number TEXT NOT NULL,
            FOREIGN KEY (group_id) REFERENCES contact_groups(id) ON DELETE CASCADE
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("✓ Database initialized")

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

class BroadcastRequest(BaseModel):
    name: str
    phone_numbers: Optional[List[str]] = None
    group_name: Optional[str] = None
    message: Optional[str] = None
    recording_file: Optional[str] = None
    tts_text: Optional[str] = None
    caller_id: Optional[str] = None
    concurrent_calls: int = 5

class ContactGroup(BaseModel):
    name: str
    phone_numbers: List[str]
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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

async def generate_tts(text: str) -> Optional[str]:
    """Generate TTS audio file using Home Assistant"""
    try:
        import requests
        
        filename = f"tts_{uuid.uuid4().hex}.wav"
        output_path = os.path.join(ASTERISK_SOUNDS, filename)
        
        headers = {
            "Authorization": f"Bearer {HA_TOKEN}",
            "Content-Type": "application/json"
        }
        
        # Call HA TTS service
        data = {
            "message": text,
            "cache": False
        }
        
        # This is simplified - actual TTS integration needs more work
        # For now, we'll just return a filename and let HA handle it
        logger.info(f"TTS requested: {text[:50]}...")
        return filename
        
    except Exception as e:
        logger.error(f"Error generating TTS: {e}")
        return None

def create_call_file(phone_number: str, audio_file: str, caller_id: str = None, 
                    call_id: str = None, max_retries: int = 3):
    """Create Asterisk call file"""
    if not call_id:
        call_id = uuid.uuid4().hex
    
    config = load_config()
    
    # Build call file content
    call_file_content = f"""Channel: SIP/trunk_main/{phone_number}
CallerID: {caller_id or 'Home Assistant'}
MaxRetries: {max_retries}
RetryTime: 300
WaitTime: 45
Context: outbound-playback
Extension: s
Priority: 1
Setvar: AUDIO_FILE={audio_file}
Setvar: CALL_ID={call_id}
Setvar: PHONE_NUMBER={phone_number}
"""
    
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
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO call_history 
            (call_id, phone_number, direction, status, audio_file, caller_id, group_name, broadcast_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (call_id, phone_number, 'outbound', 'initiated', audio_file, caller_id, group_name, broadcast_id))
        conn.commit()
        conn.close()
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
                    call_id=f"{broadcast_id}_{uuid.uuid4().hex[:8]}"
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
                
                await asyncio.sleep(2)  # Delay between calls
                
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
    os.makedirs(RECORDINGS_PATH, exist_ok=True)
    os.makedirs(ASTERISK_SOUNDS, exist_ok=True)
    logger.info("✓ API Service started")

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "Advanced Phone System API",
        "version": "1.0.0",
        "status": "running"
    }

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.post("/api/call")
async def make_call(request: CallRequest, background_tasks: BackgroundTasks):
    """Initiate a phone call"""
    try:
        logger.info(f"Call request: {request.phone_number}")
        
        # Prepare audio
        if request.tts_text:
            audio_file = await generate_tts(request.tts_text)
        elif request.recording_file:
            audio_file = request.recording_file
        else:
            audio_file = request.message
        
        if not audio_file:
            raise HTTPException(status_code=400, detail="No audio source provided")
        
        # Create call
        call_id = create_call_file(
            request.phone_number,
            audio_file,
            request.caller_id,
            max_retries=request.max_retries
        )
        
        # Save to database
        save_call_to_db(call_id, request.phone_number, audio_file, request.caller_id)
        
        # Fire HA event
        fire_ha_event("call_initiated", {
            "call_id": call_id,
            "phone_number": request.phone_number,
            "timestamp": datetime.now().isoformat()
        })
        
        return {
            "status": "success",
            "call_id": call_id,
            "phone_number": request.phone_number
        }
        
    except Exception as e:
        logger.error(f"Error making call: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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
    """Update call status from Asterisk"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        if status == "completed":
            cursor.execute('''
                UPDATE call_history 
                SET status = ?, ended_at = CURRENT_TIMESTAMP,
                    duration = (julianday(CURRENT_TIMESTAMP) - julianday(started_at)) * 86400
                WHERE call_id = ?
            ''', (status, call_id))
            
            # Update broadcast stats
            cursor.execute('SELECT broadcast_id FROM call_history WHERE call_id = ?', (call_id,))
            row = cursor.fetchone()
            if row and row[0]:
                broadcast_id = row[0]
                cursor.execute('''
                    UPDATE broadcasts 
                    SET completed = completed + 1, in_progress = in_progress - 1
                    WHERE broadcast_id = ?
                ''', (broadcast_id,))
        
        elif status == "hangup" or status == "failed":
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
        
        # Add members
        for number in group.phone_numbers:
            cursor.execute('''
                INSERT INTO group_members (group_id, phone_number)
                VALUES (?, ?)
            ''', (group_id, number))
        
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

# ============================================================================
# START SERVER
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8088, log_level="info")
