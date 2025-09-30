"""
Advanced Phone System Integration for Home Assistant
Provides services to make calls and create broadcasts from automations
"""
import logging
import voluptuous as vol
import requests
from datetime import datetime

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.const import CONF_HOST, CONF_PORT

_LOGGER = logging.getLogger(__name__)

DOMAIN = "advanced_phone_system"

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema({
            vol.Optional(CONF_HOST, default="localhost"): cv.string,
            vol.Optional(CONF_PORT, default=8088): cv.port,
        })
    },
    extra=vol.ALLOW_EXTRA,
)

# Service schemas
CALL_SCHEMA = vol.Schema({
    vol.Required("phone_number"): cv.string,
    vol.Optional("message"): cv.string,
    vol.Optional("tts_text"): cv.string,
    vol.Optional("recording_file"): cv.string,
    vol.Optional("caller_id"): cv.string,
    vol.Optional("max_retries", default=3): cv.positive_int,
})

BROADCAST_SCHEMA = vol.Schema({
    vol.Required("name"): cv.string,
    vol.Optional("phone_numbers"): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("group_name"): cv.string,
    vol.Optional("message"): cv.string,
    vol.Optional("tts_text"): cv.string,
    vol.Optional("recording_file"): cv.string,
    vol.Optional("caller_id"): cv.string,
    vol.Optional("concurrent_calls", default=5): cv.positive_int,
})

TTS_CALL_SCHEMA = vol.Schema({
    vol.Required("phone_number"): cv.string,
    vol.Required("message"): cv.template,
    vol.Optional("caller_id"): cv.string,
    vol.Optional("max_retries", default=3): cv.positive_int,
})

GROUP_SCHEMA = vol.Schema({
    vol.Required("name"): cv.string,
    vol.Required("phone_numbers"): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("caller_id"): cv.string,
})

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Advanced Phone System integration."""
    conf = config.get(DOMAIN, {})
    host = conf.get(CONF_HOST, "localhost")
    port = conf.get(CONF_PORT, 8088)
    
    api_url = f"http://{host}:{port}"
    hass.data[DOMAIN] = {"api_url": api_url}
    
    _LOGGER.info(f"Advanced Phone System initialized: {api_url}")
    
    async def make_call(call: ServiceCall) -> None:
        """Make a phone call."""
        phone_number = call.data.get("phone_number")
        message = call.data.get("message")
        tts_text = call.data.get("tts_text")
        recording_file = call.data.get("recording_file")
        caller_id = call.data.get("caller_id")
        max_retries = call.data.get("max_retries", 3)
        
        payload = {
            "phone_number": phone_number,
            "caller_id": caller_id,
            "max_retries": max_retries
        }
        
        if tts_text:
            payload["tts_text"] = tts_text
        elif recording_file:
            payload["recording_file"] = recording_file
        elif message:
            payload["message"] = message
        
        try:
            response = await hass.async_add_executor_job(
                lambda: requests.post(f"{api_url}/api/call", json=payload, timeout=10)
            )
            
            if response.status_code == 200:
                result = response.json()
                _LOGGER.info(f"Call initiated: {result.get('call_id')}")
                
                hass.bus.async_fire(f"{DOMAIN}_call_initiated", {
                    "call_id": result.get("call_id"),
                    "phone_number": phone_number,
                    "timestamp": datetime.now().isoformat()
                })
            else:
                _LOGGER.error(f"Failed to make call: {response.status_code}")
                
        except Exception as e:
            _LOGGER.error(f"Error making call: {e}")
    
    async def make_broadcast(call: ServiceCall) -> None:
        """Create a broadcast."""
        name = call.data.get("name")
        phone_numbers = call.data.get("phone_numbers")
        group_name = call.data.get("group_name")
        message = call.data.get("message")
        tts_text = call.data.get("tts_text")
        recording_file = call.data.get("recording_file")
        caller_id = call.data.get("caller_id")
        concurrent_calls = call.data.get("concurrent_calls", 5)
        
        payload = {
            "name": name,
            "concurrent_calls": concurrent_calls
        }
        
        if phone_numbers:
            payload["phone_numbers"] = phone_numbers
        if group_name:
            payload["group_name"] = group_name
        
        if tts_text:
            payload["tts_text"] = tts_text
        elif recording_file:
            payload["recording_file"] = recording_file
        elif message:
            payload["message"] = message
        
        try:
            response = await hass.async_add_executor_job(
                lambda: requests.post(f"{api_url}/api/broadcast", json=payload, timeout=10)
            )
            
            if response.status_code == 200:
                result = response.json()
                _LOGGER.info(f"Broadcast created: {result.get('broadcast_id')}")
                
                hass.bus.async_fire(f"{DOMAIN}_broadcast_started", {
                    "broadcast_id": result.get("broadcast_id"),
                    "name": name,
                    "total_numbers": result.get("total_numbers"),
                    "timestamp": datetime.now().isoformat()
                })
            else:
                _LOGGER.error(f"Failed to create broadcast: {response.status_code}")
                
        except Exception as e:
            _LOGGER.error(f"Error creating broadcast: {e}")
    
    async def call_with_sensor(call: ServiceCall) -> None:
        """Make a call with sensor data in TTS."""
        phone_number = call.data.get("phone_number")
        message_template = call.data.get("message")
        caller_id = call.data.get("caller_id")
        max_retries = call.data.get("max_retries", 3)
        
        # Render template with sensor data
        message_template.hass = hass
        rendered_message = message_template.async_render()
        
        payload = {
            "phone_number": phone_number,
            "tts_text": rendered_message,
            "caller_id": caller_id,
            "max_retries": max_retries
        }
        
        try:
            response = await hass.async_add_executor_job(
                lambda: requests.post(f"{api_url}/api/call", json=payload, timeout=10)
            )
            
            if response.status_code == 200:
                result = response.json()
                _LOGGER.info(f"TTS call initiated: {result.get('call_id')}")
            else:
                _LOGGER.error(f"Failed to make TTS call: {response.status_code}")
                
        except Exception as e:
            _LOGGER.error(f"Error making TTS call: {e}")
    
    async def create_group(call: ServiceCall) -> None:
        """Create a contact group."""
        name = call.data.get("name")
        phone_numbers = call.data.get("phone_numbers")
        caller_id = call.data.get("caller_id")
        
        payload = {
            "name": name,
            "phone_numbers": phone_numbers,
            "caller_id": caller_id
        }
        
        try:
            response = await hass.async_add_executor_job(
                lambda: requests.post(f"{api_url}/api/groups", json=payload, timeout=10)
            )
            
            if response.status_code == 200:
                result = response.json()
                _LOGGER.info(f"Group created: {result.get('name')}")
            else:
                _LOGGER.error(f"Failed to create group: {response.status_code}")
                
        except Exception as e:
            _LOGGER.error(f"Error creating group: {e}")
    
    # Register services
    hass.services.async_register(
        DOMAIN, "make_call", make_call, schema=CALL_SCHEMA
    )
    
    hass.services.async_register(
        DOMAIN, "create_broadcast", make_broadcast, schema=BROADCAST_SCHEMA
    )
    
    hass.services.async_register(
        DOMAIN, "call_with_sensor", call_with_sensor, schema=TTS_CALL_SCHEMA
    )
    
    hass.services.async_register(
        DOMAIN, "create_group", create_group, schema=GROUP_SCHEMA
    )
    
    _LOGGER.info("Advanced Phone System services registered")
    
    return True
