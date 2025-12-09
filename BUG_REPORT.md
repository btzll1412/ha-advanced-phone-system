# Bug Report - Home Assistant Advanced Phone System

**Date:** 2025-12-08
**Reviewed By:** Code Analysis
**Branch:** claude/review-and-identify-bugs-01PpzLX2BXcdqsMEr8qQ7wEH

---

## Critical Security Issues

### 1. SQL Injection Vulnerability in Backup/Restore Functions
**File:** `rootfs/app/api_service.py`
**Lines:** 3405, 3469, 3479
**Severity:** CRITICAL

The backup and restore functions use f-strings to build SQL queries with table names directly from the data:

```python
cursor.execute(f"SELECT * FROM {table}")  # Line 3405
cursor.execute(f"DELETE FROM {table_name}")  # Line 3469
cursor.execute(f"INSERT OR REPLACE INTO {table_name} ...")  # Line 3479
```

**Risk:** If a malicious backup file contains a crafted table name, it could execute arbitrary SQL.

**Fix:** Validate table names against a whitelist before executing queries.

---

### 2. Path Traversal Vulnerability in Recording Endpoints
**File:** `rootfs/app/api_service.py`
**Lines:** 2579-2596, 2598-2650, 2549-2578
**Severity:** HIGH

The recording endpoints (`delete_recording`, `play_recording`, `rename_recording`) don't validate that filenames don't contain path traversal characters like `../`:

```python
file_path = os.path.join(ASTERISK_SOUNDS, filename)  # No validation
```

**Risk:** An attacker could read/delete files outside the recordings directory.

**Fix:** Add path validation:
```python
import os
safe_path = os.path.realpath(os.path.join(ASTERISK_SOUNDS, filename))
if not safe_path.startswith(os.path.realpath(ASTERISK_SOUNDS)):
    raise HTTPException(status_code=400, detail="Invalid filename")
```

---

### 3. Hardcoded Default Admin PIN
**Files:**
- `rootfs/app/api_service.py` (lines 366-367)
- `rootfs/var/lib/asterisk/agi-bin/verify_admin_pin.py` (lines 63-64)
**Severity:** MEDIUM

The default admin PIN "1234" is hardcoded and weak:

```python
cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_pin', '1234')")
```

**Risk:** Users who don't change the PIN are vulnerable.

**Fix:** Force PIN change on first use or generate a random PIN.

---

## Logic Bugs

### 4. Race Condition in CDR Processing
**File:** `rootfs/app/api_service.py`
**Lines:** 690-707
**Severity:** MEDIUM

When processing CDR records for outbound calls, the code queries the most recent call from `call_history` without any locking:

```python
c_temp.execute('''
    SELECT phone_number, caller_id
    FROM call_history
    ORDER BY started_at DESC
    LIMIT 1
''')
```

**Risk:** During concurrent broadcasts, this could match the wrong call, leading to incorrect call history entries.

**Fix:** Use the call_id from the CDR userfield or channel name to match the correct record.

---

### 5. IVR Phone Number Validation Too Restrictive
**File:** `rootfs/etc/asterisk/extensions.conf`
**Line:** 444
**Severity:** LOW

The IVR add-contact feature only accepts exactly 10 digits:

```
exten => s,n,GotoIf($[${LEN(${CONTACT_PHONE})} = 10]?valid:invalid)
```

**Risk:** Users cannot add international numbers or 11-digit US numbers (with country code).

**Fix:** Change to `>= 10` for more flexibility:
```
exten => s,n,GotoIf($[${LEN(${CONTACT_PHONE})} >= 10]?valid:invalid)
```

---

### 6. Schedule Datetime Announcement Shows Wrong Time
**File:** `rootfs/etc/asterisk/extensions.conf`
**Line:** 331
**Severity:** LOW

The schedule confirmation announces the current time instead of the scheduled time:

```
exten => s,n,SayUnixTime(${STRFTIME(${EPOCH},,%s)},,ABdY IM p)
```

**Risk:** Users hear the wrong confirmation time.

**Fix:** Should convert `IVR_SCHEDULE_TIME` to Unix timestamp and say that instead.

---

### 7. Callback Audio File Path Inconsistency
**File:** `rootfs/app/api_service.py`
**Lines:** 1759, 1531
**Severity:** MEDIUM

When creating callback entries, the audio file path format is inconsistent:

- In `/api/call`: Uses full path: `/var/lib/asterisk/sounds/custom/{audio_file}.wav`
- In `process_broadcast`: Uses just the filename: `{audio_file}`

The `callback_lookup.py` script then checks if files exist, but the inconsistent paths may cause lookups to fail.

**Fix:** Standardize the audio file path format in both locations.

---

## Resource Management Issues

### 8. Database Connection Leaks
**File:** `rootfs/app/api_service.py`
**Multiple locations**
**Severity:** MEDIUM

Several functions open database connections but may not close them if an exception occurs:

Example in `process_broadcast` (line 1484):
```python
if not phone_numbers:
    logger.error(f"No phone numbers for broadcast {broadcast_id}")
    cursor.execute('UPDATE broadcasts SET status = ? WHERE broadcast_id = ?', ('failed', broadcast_id))
    conn.commit()
    conn.close()
    return  # Connection closed here, but...
```

The `conn` variable is used later in the function for concurrent calls, but if it was closed earlier, subsequent operations will fail.

**Fix:** Use context managers (`with get_db() as conn:`) or ensure proper cleanup in finally blocks.

---

### 9. TTS Cache Never Cleaned Up
**Files:**
- `rootfs/app/api_service.py` (generate_tts function)
- `rootfs/var/lib/asterisk/agi-bin/ivr_handler.py` (generate_tts_audio function)
**Severity:** LOW

Generated TTS audio files accumulate indefinitely:
- `/var/lib/asterisk/sounds/custom/tts_*.wav`
- `/var/lib/asterisk/sounds/tts_cache/`

**Risk:** Disk space exhaustion over time.

**Fix:** Add periodic cleanup of old TTS files (e.g., older than 7 days).

---

### 10. Session Cleanup Only Runs When Count > 100
**File:** `rootfs/app/api_service.py`
**Lines:** 267-268
**Severity:** LOW

```python
if len(active_sessions) > 100:
    cleanup_expired_sessions()
```

**Risk:** Expired sessions linger in memory until 100+ sessions exist.

**Fix:** Run cleanup periodically regardless of session count, or use a TTL cache.

---

## Code Quality Issues

### 11. Bare Except Clause
**File:** `rootfs/app/api_service.py`
**Line:** 3096
**Severity:** LOW

```python
def is_amd_enabled():
    try:
        ...
    except:  # Bare except catches everything including SystemExit, KeyboardInterrupt
        return True
```

**Fix:** Use `except Exception:` instead.

---

### 12. Debug Logging in Production
**File:** `rootfs/app/api_service.py`
**Line:** 522
**Severity:** LOW

```python
logger.info(f"DEBUG - channel: {channel}, context: {context}")
```

**Risk:** Clutters logs and may expose sensitive info.

**Fix:** Remove or change to `logger.debug()`.

---

### 13. Inconsistent Phone Number Normalization
**Files:**
- `rootfs/app/api_service.py` (normalize_phone_number)
- `rootfs/var/lib/asterisk/agi-bin/callback_lookup.py` (normalize_phone_number)
**Severity:** LOW

Two different implementations of phone number normalization exist, which could lead to matching issues.

**Fix:** Consolidate into a single, consistent implementation.

---

## Minor Issues

### 14. Missing ulaw Format in TTS Generation
**File:** `rootfs/app/api_service.py`
**Lines:** 1284-1376
**Severity:** LOW

The main `generate_tts` function creates WAV files but not ulaw format, while the IVR handler's `generate_tts_audio` creates both. Some SIP trunks may prefer ulaw.

**Fix:** Add ulaw conversion to the main generate_tts function.

---

### 15. Callback Expiration Not Enforced Server-Side
**File:** `rootfs/app/api_service.py`
**Severity:** LOW

Broadcast callbacks have an `expires_at` field set to 7 days, but there's no scheduled job to disable or clean up expired callbacks in the API service.

The check is only in `callback_lookup.py` during AGI execution.

**Fix:** Add a periodic cleanup task for expired callbacks.

---

## Recommendations

1. **Immediate:** Fix SQL injection and path traversal vulnerabilities
2. **High Priority:** Address race condition in CDR processing
3. **Medium Priority:** Fix database connection handling and resource leaks
4. **Low Priority:** Clean up code quality issues and add missing features

---

## Files Reviewed

- `rootfs/app/api_service.py` (3,517 lines)
- `rootfs/etc/asterisk/extensions.conf` (649 lines)
- `rootfs/etc/services.d/phone-system/run` (399 lines)
- `rootfs/var/lib/asterisk/agi-bin/ivr_handler.py` (416 lines)
- `rootfs/var/lib/asterisk/agi-bin/callback_lookup.py` (212 lines)
- `rootfs/var/lib/asterisk/agi-bin/verify_admin_pin.py` (87 lines)
- `rootfs/app/generate_ivr_prompts.py` (181 lines)
