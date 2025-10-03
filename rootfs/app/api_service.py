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
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import aiofiles

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI
app = FastAPI(title="Advanced Phone System API", version="1.0.0")

@app.get("/", response_class=HTMLResponse)
async def serve_web_ui():
    """Serve the web UI"""
    return FileResponse("/app/web/index.html")

@app.get("/api")
async def api_info():
    """API information endpoint"""
    return {
        "service": "Advanced Phone System API",
        "version": "1.0.0",
        "status": "running"
    }

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
    """Generate TTS audio file using espeak"""
    try:
        filename = f"tts_{uuid.uuid4().hex}.wav"
        temp_path = f"/tmp/tts_{uuid.uuid4().hex}.wav"
        output_path = os.path.join(ASTERISK_SOUNDS, filename)
        
        logger.info(f"TTS requested: {text[:50]}...")
        
        # Generate with espeak
        result = subprocess.run(
            ['espeak', '-w', temp_path, '-v', 'en+m3', '-s', '160', '-p', '50', text],
            capture_output=True,
            check=False
        )
        
        if result.returncode == 0 and os.path.exists(temp_path):
            # Convert to 8kHz WAV (telephony standard) with 16-bit quality
            convert_result = subprocess.run(
                ['sox', temp_path, '-r', '8000', '-c', '1', '-b', '16', output_path],
                capture_output=True,
                check=False
            )
            
            subprocess.run(['chown', 'asterisk:asterisk', output_path], check=False)
            os.remove(temp_path)
            
            if convert_result.returncode == 0 and os.path.exists(output_path):
                logger.info(f"✓ TTS file created: {filename}")
                return filename.replace('.wav', '')
            else:
                logger.error(f"Audio conversion failed")
                return None
        else:
            logger.error(f"TTS generation failed")
            return None
            
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
# RECORDINGS MANAGEMENT ENDPOINTS
# ============================================================================

@app.get("/api/recordings")
async def list_recordings():
    """List all available recordings"""
    try:
        recordings = []
        recordings_dir = Path(RECORDINGS_PATH)
        
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
        
        # CHANGE THIS: Save to Asterisk sounds directory, not recordings path
        file_path = os.path.join(ASTERISK_SOUNDS, safe_filename)  # Changed from RECORDINGS_PATH
        
        # Save uploaded file
        async with aiofiles.open(file_path, 'wb') as f:
            content = await file.read()
            await f.write(content)
        
        # Convert to Asterisk-compatible format (8kHz WAV)
        if file_ext in ['.mp3', '.wav']:
            output_path = file_path.replace(file_ext, '.wav')
            result = subprocess.run(
                ['sox', file_path, '-r', '8000', '-c', '1', '-b', '16', output_path],
                capture_output=True,
                check=False
            )
            if result.returncode == 0:
                if file_ext == '.mp3':
                    os.remove(file_path)
                    file_path = output_path
                    safe_filename = safe_filename.replace('.mp3', '.wav')
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
        old_path = os.path.join(RECORDINGS_PATH, old_name)
        
        # Replace spaces with underscores
        new_name = new_name.replace(' ', '_')
        
        # Keep the same extension
        extension = Path(old_name).suffix
        if not new_name.endswith(extension):
            new_name += extension
        
        new_path = os.path.join(RECORDINGS_PATH, new_name)
        
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
    """Delete a recording"""
    try:
        file_path = os.path.join(RECORDINGS_PATH, filename)
        
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
async def play_recording(filename: str):
    """Stream a recording for preview"""
    try:
        file_path = os.path.join(RECORDINGS_PATH, filename)
        
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="Recording not found")
        
        return FileResponse(
            file_path,
            media_type='audio/wav',
            filename=filename
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error playing recording: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================================
# START SERVER
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8088, log_level="info")
