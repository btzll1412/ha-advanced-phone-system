#!/usr/bin/env python3
"""Add direction column to existing database"""

import sqlite3
import os

DB_PATH = os.getenv('DB_PATH', '/data/phone_system.db')

def migrate():
    if not os.path.exists(DB_PATH):
        print(f"❌ Database not found: {DB_PATH}")
        return
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check if column exists
    cursor.execute("PRAGMA table_info(call_history)")
    columns = [col[1] for col in cursor.fetchall()]
    
    if 'direction' not in columns:
        print("Adding 'direction' column...")
        cursor.execute("ALTER TABLE call_history ADD COLUMN direction TEXT DEFAULT 'unknown'")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_call_direction ON call_history(direction)")
        conn.commit()
        print("✅ Migration complete!")
    else:
        print("✅ Column already exists")
    
    conn.close()

if __name__ == "__main__":
    migrate()
