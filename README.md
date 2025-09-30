# Advanced Phone System for Home Assistant

A professional VoIP phone system add-on for Home Assistant with Flowroute integration, enabling automated calls and broadcast messaging.

## 🎯 Features

- **Automated Calls** - Trigger calls from any Home Assistant automation
- **Text-to-Speech** - Dynamic TTS with sensor values
- **Broadcast System** - Send messages to multiple numbers simultaneously
- **Contact Groups** - Organize and manage phone number lists
- **Web Interface** - User-friendly dashboard for management
- **Call History** - Track all calls with detailed logs
- **Flowroute Support** - Pre-configured for Flowroute (works with any SIP provider)

## 📋 Requirements

- Home Assistant OS, Supervised, or Container
- SIP trunk provider (Flowroute, Twilio, VoIP.ms, etc.)
- Active phone number from your provider

## 🚀 Installation

### Step 1: Add Repository

1. Go to **Settings** → **Add-ons** → **Add-on Store**
2. Click **⋮** (three dots) → **Repositories**
3. Add: `https://github.com/btzll1412/ha-advanced-phone-system`
4. Click **Add** → **Close**

### Step 2: Install Add-on

1. Find "Advanced Phone System" in the Add-on Store
2. Click **Install**
3. Wait for installation to complete

### Step 3: Configure SIP Provider

For **Flowroute**:
```yaml
sip_trunk:
  enabled: true
  provider: "flowroute"
  host: "sip.flowroute.com"
  port: 5060
  username: "your_access_key"
  password: "your_secret_key"
  from_domain: ""

extensions:
  - number: "100"
    name: "Home Assistant"
    secret: "your_strong_password_here"

contact_groups:
  - name: "Family"
    numbers: []
    caller_id: "Home Alert"

web_auth:
  enabled: false
  username: "admin"
  password: "admin"

tts_engine: "google_translate"
recordings_path: "/share/phone_recordings"
max_concurrent_calls: 5
log_level: "info"
