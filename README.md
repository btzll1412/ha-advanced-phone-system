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

### Prerequisites
- Home Assistant OS, Supervised, or Container
- SSH access to Home Assistant
- Flowroute account (or any SIP provider)

### Step 1: Add Custom Repository (30 seconds)

1. In Home Assistant, go to **Settings** → **Add-ons** → **Add-on Store**
2. Click the **⋮** menu (three dots, top right)
3. Select **Repositories**
4. Add this URL: `https://github.com/btzll1412/ha-advanced-phone-system`
5. Click **Add** → **Close**

### Step 2: Install the Add-on (2 minutes)

1. Refresh the Add-on Store page
2. Scroll down to find **"Advanced Phone System"**
3. Click on it
4. Click **Install**
5. Wait for installation to complete

### Step 3: Install Custom Integration (2 minutes)

### The add-on includes a Home Assistant integration for automation services. Install it via SSH:
```bash
### SSH into Home Assistant:
ssh root@homeassistant.local

# Create directory and copy integration:
mkdir -p /config/custom_components
cp -r /addons/ha-advanced-phone-system/custom_components/advanced_phone_system /config/custom_components/

# Verify it was copied
ls /config/custom_components/advanced_phone_system/

You should see: __init__.py, manifest.json, services.yaml
Step 4: Configure Home Assistant (1 minute)
Add to your /config/configuration.yaml:
yamladvanced_phone_system:
  host: "localhost"
  port: 8088
### Step 5: Restart Home Assistant
bashha core restart
Or use: Settings → System → Restart
Step 6: Configure the Add-on

Go to Settings → Add-ons → Advanced Phone System
Click the Configuration tab
Enter your Flowroute credentials:

sip_trunk:
  enabled: true
  provider: "flowroute"
  host: "sip.flowroute.com"
  port: 5060
  username: "YOUR_ACCESS_KEY"
  password: "YOUR_SECRET_KEY"
  from_domain: ""

extensions:
  - number: "100"
    name: "Home Assistant"
    secret: "CHANGE_THIS_PASSWORD"

web_auth:
  enabled: false

tts_engine: "google_translate"
max_concurrent_calls: 5
log_level: "info"

Click Save

Step 7: Start the Add-on

Go to the Info tab
Click Start
Watch the Log tab
Wait for: "Advanced Phone System is READY!"

Step 8: Verify Installation
Check services are available:

Go to Developer Tools → Services
Search for advanced_phone_system
You should see 4 services:

advanced_phone_system.make_call
advanced_phone_system.create_broadcast
advanced_phone_system.call_with_sensor
advanced_phone_system.create_group



Access Web UI:

Open: http://homeassistant.local:8088
You should see the dashboard

Step 9: Make Your First Test Call
service: advanced_phone_system.make_call
data:
  phone_number: "+1234567890"  # YOUR phone number
  tts_text: "Hello! This is a test call from Home Assistant."
  caller_id: "Home Assistant"
✅ Installation Complete!
If the call goes through, everything is working. Check the Logs tab if you encounter issues.
