#!/usr/bin/env python3
"""
Communication Manager Bot - MVP for Railway Deployment
=====================================================

Minimal viable product that captures operational issues and follow-ups,
stores them directly in Notion with photo attachments.

Features:
- Follow-ups about people
- Kitchen issues  
- Facility issues
- Direct photo upload to Notion
- Weekly automated follow-ups
- Railway-ready deployment

Author: Based on K2 system patterns
Version: 1.0.0-MVP
"""

import asyncio
import json
import logging
import os
import sys
import threading
import time
import uuid
import signal
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from enum import Enum

import requests

# Handle Pydantic v1 vs v2 imports
try:
    from pydantic import BaseSettings, Field, field_validator
    PYDANTIC_V2 = True
except ImportError:
    try:
        from pydantic_settings import BaseSettings
        from pydantic import Field, field_validator
        PYDANTIC_V2 = True
    except ImportError:
        from pydantic import BaseSettings, Field, validator
        PYDANTIC_V2 = False

# System info
SYSTEM_VERSION = "1.0.0-MVP"
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(message)s"

# Load environment variables
def load_env_file():
    """Load .env file if it exists (for local development)"""
    if os.path.exists('.env'):
        with open('.env', 'r') as f:
            for line in f:
                if line.strip() and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key.strip()] = value.strip().strip('"').strip("'")

load_env_file()

# ===== CONFIGURATION =====

class Settings(BaseSettings):
    """Simplified configuration for MVP"""
    
    # Required
    telegram_bot_token: str = Field(..., env='TELEGRAM_BOT_TOKEN')
    notion_token: str = Field(..., env='NOTION_TOKEN')
    employees_db_id: str = Field(..., env='EMPLOYEES_DB_ID')  
    communication_db_id: str = Field(..., env='COMMUNICATION_DB_ID')  # Renamed from ops_items
    
    # Optional
    log_chat_id: Optional[int] = Field(None, env='LOG_CHAT_ID')
    shoutout_chat_id: Optional[int] = Field(None, env='SHOUTOUT_CHAT_ID')  # New chat ID for shoutouts
    port: int = Field(8000, env='PORT')  # Railway needs this
    default_timezone: str = Field('America/Chicago', env='DEFAULT_TIMEZONE')  # Add default timezone
    
    if PYDANTIC_V2:
        @field_validator('log_chat_id', mode='before')
        @classmethod
        def parse_log_chat_id(cls, v):
            if v == '' or v is None:
                return None
            try:
                return int(v)
            except (ValueError, TypeError):
                return None
    else:
        @validator('log_chat_id', pre=True)
        def parse_log_chat_id(cls, v):
            if v == '' or v is None:
                return None
            try:
                return int(v)
            except (ValueError, TypeError):
                return None
    
    class Config:
        env_file = '.env'

# ===== LOGGING =====

def setup_logging():
    """Simple logging for Railway"""
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    return logging.getLogger('system')

logger = setup_logging()

# ===== UTILITY FUNCTIONS =====

def get_local_time(timezone_name: str = None) -> datetime:
    """Get current time in specified timezone, fallback to system time"""
    if not timezone_name:
        return datetime.now()
    
    try:
        import pytz
        tz = pytz.timezone(timezone_name)
        utc_now = datetime.now(datetime.UTC if hasattr(datetime, 'UTC') else pytz.UTC)
        if not hasattr(datetime, 'UTC'):
            utc_now = utc_now.replace(tzinfo=pytz.UTC)
        local_time = utc_now.astimezone(tz)
        return local_time.replace(tzinfo=None)  # Remove timezone info for consistency
    except (ImportError, Exception):
        # Fallback to system time if pytz not available or timezone invalid
        return datetime.now()

# ===== DATA MODELS =====

class ItemType(Enum):
    FOLLOWUP = "Follow-up"
    KITCHEN_ISSUE = "Kitchen Issue"
    FACILITY_ISSUE = "Facility Issue"
    SHOUTOUT = "Shout-out"

@dataclass
class Employee:
    id: str
    name: str
    telegram_handle: Optional[str] = None
    active: bool = True

@dataclass  
class ConversationState:
    user_id: int
    chat_id: int
    command: str
    step: str
    data: Dict[str, Any] = field(default_factory=dict)
    photos: List[str] = field(default_factory=list)  # Telegram file_ids
    started_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    
    def is_expired(self) -> bool:
        return (datetime.now() - self.last_activity).total_seconds() > 1800  # 30 min
    
    def update_activity(self):
        self.last_activity = datetime.now()

# ===== NOTION CLIENT =====

class NotionClient:
    """Simplified Notion client for MVP"""
    
    def __init__(self, token: str, employees_db_id: str, communication_db_id: str):
        self.token = token
        self.employees_db_id = employees_db_id
        self.communication_db_id = communication_db_id
        
        self.headers = {
            'Authorization': f'Bearer {token}',
            'Notion-Version': '2022-06-28'
        }
        self.base_url = "https://api.notion.com/v1"
        self.logger = logging.getLogger('notion')
    
    def _make_request(self, method: str, path: str, data: Dict = None, files: Dict = None) -> Optional[Dict]:
        """Make request to Notion API"""
        url = f"{self.base_url}{path}"
        
        try:
            if files:
                # For file uploads, don't set Content-Type header
                headers = {k: v for k, v in self.headers.items() if k != 'Content-Type'}
                resp = requests.request(method, url, data=data, files=files, headers=headers, timeout=30)
            else:
                resp = requests.request(method, url, json=data, headers=self.headers, timeout=30)
            
            if 200 <= resp.status_code < 300:
                return resp.json()
            else:
                self.logger.error(f"Notion API error: {resp.status_code} - {resp.text}")
                return None
                
        except Exception as e:
            self.logger.error(f"Notion request failed: {e}")
            return None
    
    def get_employees(self) -> List[Employee]:
        """Get active employees"""
        query = {
            'filter': {'property': 'active', 'checkbox': {'equals': True}},
            'sorts': [{'property': 'Name', 'direction': 'ascending'}]
        }
        
        response = self._make_request('POST', f'/databases/{self.employees_db_id}/query', query)
        if not response:
            return []
        
        employees = []
        for page in response.get('results', []):
            props = page.get('properties', {})
            try:
                name_prop = props.get('Name', {}).get('title', [])
                name = name_prop[0]['plain_text'] if name_prop else 'Unknown'
                
                telegram_prop = props.get('telegram_handle', {}).get('rich_text', [])
                telegram = telegram_prop[0]['plain_text'] if telegram_prop else None
                
                active = props.get('active', {}).get('checkbox', True)
                
                employees.append(Employee(
                    id=page['id'],
                    name=name,
                    telegram_handle=telegram,
                    active=active
                ))
            except Exception as e:
                self.logger.warning(f"Failed to parse employee: {e}")
                continue
        
        return employees
    
    def upload_photo_to_notion(self, telegram_file_id: str, bot_token: str) -> Optional[str]:
        """Download photo from Telegram and get it ready for Notion"""
        try:
            # Get file info from Telegram
            file_response = requests.get(
                f"https://api.telegram.org/bot{bot_token}/getFile",
                params={'file_id': telegram_file_id}
            )
            
            if not file_response.ok:
                return None
            
            file_info = file_response.json()
            if not file_info.get('ok'):
                return None
            
            file_path = file_info['result']['file_path']
            
            # Download the actual image
            download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
            image_response = requests.get(download_url)
            
            if not image_response.ok:
                return None
            
            # For MVP: We'll return the download URL temporarily
            # Notion will cache it when we create the page
            return download_url
            
        except Exception as e:
            self.logger.error(f"Failed to process photo: {e}")
            return None
    
    def create_communication_item(self, item_type: ItemType, narrative: str, 
                                person_id: str = None, area: str = None,
                                occurred_at: datetime = None, photo_urls: List[str] = None,
                                anonymous: bool = False, reporter_name: str = None) -> Optional[str]:
        """Create communication item in Notion"""
        try:
            # Build title
            title = f"{item_type.value} - {(occurred_at or datetime.now()).strftime('%Y-%m-%d')}"
            
            # Build properties - using rich_text based on earlier error fix
            properties = {
                'item_title': {
                    'title': [{'text': {'content': title}}]
                },
                'item_type': {
                    'rich_text': [{'text': {'content': item_type.value}}]
                },
                'narrative': {
                    'rich_text': [{'text': {'content': narrative}}]
                },
                'occurred_at': {
                    'date': {'start': (occurred_at or datetime.now()).isoformat()}
                },
                'submitted_anonymously': {
                    'checkbox': anonymous
                },
                'source': {
                    'rich_text': [{'text': {'content': 'Telegram'}}]
                }
            }
            
            # Add reporter name if not anonymous and provided
            if not anonymous and reporter_name:
                properties['reporter'] = {
                    'rich_text': [{'text': {'content': reporter_name}}]
                }
            
            # Add person as rich_text for follow-ups and shout-outs
            if person_id and item_type in [ItemType.FOLLOWUP, ItemType.SHOUTOUT]:
                # Get person name for rich_text field
                employees = self.get_employees()
                person_name = next((emp.name for emp in employees if emp.id == person_id), 'Unknown')
                properties['person_of_focus'] = {
                    'rich_text': [{'text': {'content': person_name}}]
                }
            
            # Add area for issues
            if area and item_type in [ItemType.KITCHEN_ISSUE, ItemType.FACILITY_ISSUE]:
                properties['area_or_equipment'] = {
                    'rich_text': [{'text': {'content': area}}]
                }
            
            # Add photos as external files (Notion will cache them)
            if photo_urls:
                properties['images'] = {
                    'files': [
                        {
                            'type': 'external',
                            'name': f'Photo {i+1}',
                            'external': {'url': url}
                        }
                        for i, url in enumerate(photo_urls)
                    ]
                }
            
            # Create the page
            page_data = {
                'parent': {'database_id': self.communication_db_id},
                'properties': properties
            }
            
            response = self._make_request('POST', '/pages', page_data)
            
            if response:
                page_id = response['id']
                self.logger.info(f"Created communication item: {page_id}")
                return page_id
            
            return None
            
        except Exception as e:
            self.logger.error(f"Failed to create communication item: {e}")
            return None

# ===== TELEGRAM BOT =====

class TelegramBot:
    """Simplified Telegram bot for MVP"""
    
    def __init__(self, settings: Settings, notion: NotionClient):
        self.settings = settings
        self.notion = notion
        self.base_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        self.logger = logging.getLogger('telegram')
        
        # State management
        self.conversations: Dict[int, ConversationState] = {}
        self.running = False
        self.last_update_id = 0
    
    def start_polling(self):
        """Start polling for messages"""
        self.running = True
        self.logger.info("Starting Telegram bot")
        
        while self.running:
            try:
                updates = self._get_updates()
                if not self.running:  # Check if we should stop
                    break
                    
                for update in updates:
                    if not self.running:  # Check again during processing
                        break
                    self._process_update(update)
                    
            except KeyboardInterrupt:
                self.logger.info("KeyboardInterrupt in polling loop")
                break
            except Exception as e:
                if self.running:  # Only log errors if we're still supposed to be running
                    self.logger.error(f"Polling error: {e}")
                    time.sleep(5)
                else:
                    break
        
        self.logger.info("Telegram bot polling stopped")
    
    def stop(self):
        self.running = False
    
    def _get_updates(self) -> List[Dict]:
        """Get updates from Telegram"""
        data = {"timeout": 25}
        if self.last_update_id:
            data["offset"] = self.last_update_id + 1
        
        try:
            resp = requests.post(f"{self.base_url}/getUpdates", json=data, timeout=30)
            if resp.ok:
                result = resp.json()
                if result.get("ok"):
                    updates = result.get("result", [])
                    if updates:
                        self.last_update_id = updates[-1]["update_id"]
                    return updates
        except:
            pass
        
        return []
    
    def _process_update(self, update: Dict):
        """Process incoming update"""
        try:
            if "callback_query" in update:
                self._handle_callback(update["callback_query"])
            elif "message" in update:
                self._handle_message(update["message"])
        except Exception as e:
            self.logger.error(f"Error processing update: {e}")
    
    def _handle_message(self, message: Dict):
        """Handle incoming message"""
        user_id = message.get("from", {}).get("id")
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text", "")
        
        if not user_id or not chat_id:
            return
        
        # Handle commands
        if text.startswith("/"):
            command = text.split()[0].lower()
            
            if command == "/report":
                self._send_start_menu(chat_id)
            elif command == "/followup":
                self._start_followup(chat_id, user_id)
            elif command == "/kitchen":
                self._start_kitchen_issue(chat_id, user_id)
            elif command == "/facility":
                self._start_facility_issue(chat_id, user_id)
            elif command == "/shoutout":
                self._start_shoutout(chat_id, user_id)
            elif command == "/cancel_comm":
                self._cancel_conversation(chat_id, user_id)
            elif command == "/comm_help":
                self._send_help(chat_id)
            elif command == "/comm_status":
                self._send_status(chat_id)
            else:
                self.send_message(chat_id, "Unknown command. Type /comm_help for available commands.")
            return
        
        # Handle photos
        if "photo" in message:
            self._handle_photo(message)
            return
        
        # Handle conversation input
        if user_id in self.conversations:
            self._handle_conversation_input(message)
            return
        
        # Default response
        self.send_message(chat_id, 
                        "Type /comm_help to see available commands or /report to start a new report:")
        self._send_start_menu(chat_id)
    
    def _handle_callback(self, callback: Dict):
        """Handle callback queries"""
        data = callback.get("data", "")
        user_id = callback.get("from", {}).get("id")
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        
        # Acknowledge callback
        requests.post(f"{self.base_url}/answerCallbackQuery", 
                     json={"callback_query_id": callback.get("id")})
        
        if data == "start_followup":
            self._start_followup(chat_id, user_id)
        elif data == "start_kitchen":
            self._start_kitchen_issue(chat_id, user_id)
        elif data == "start_facility":
            self._start_facility_issue(chat_id, user_id)
        elif data == "start_shoutout":
            self._start_shoutout(chat_id, user_id)
        elif data.startswith("person_"):
            self._select_person(chat_id, user_id, data.split("_", 1)[1])
        elif data.startswith("area_"):
            self._select_area(chat_id, user_id, data.split("_", 1)[1])
        elif data == "occurred_now":
            self._set_occurred_now(chat_id, user_id)
        elif data == "occurred_custom":
            self._set_occurred_custom(chat_id, user_id)
        elif data == "go_back":
            self._go_back(chat_id, user_id)
        elif data == "skip_photos":
            self._skip_photos(chat_id, user_id)
        elif data == "anonymous_yes":
            self._set_anonymous(chat_id, user_id, True)
        elif data == "anonymous_no":
            self._set_anonymous(chat_id, user_id, False)
        elif data == "submit":
            self._submit_report(chat_id, user_id)
        elif data == "cancel":
            self._cancel_conversation(chat_id, user_id)
    
    def send_message(self, chat_id: int, text: str, reply_markup: Dict = None) -> bool:
        """Send message to chat"""
        data = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        
        if reply_markup:
            data["reply_markup"] = reply_markup
        
        try:
            resp = requests.post(f"{self.base_url}/sendMessage", json=data)
            return resp.ok
        except:
            return False
    
    def _send_start_menu(self, chat_id: int):
        """Send start menu"""
        text = (
            f"<b>Communication Manager Bot v{SYSTEM_VERSION}</b>\n\n"
            "Report operational issues and feedback:\n\n"
            "• Follow-ups about team members\n"
            "• Kitchen operational issues\n" 
            "• Facility issues (safety, cleanliness, etc.)\n"
            "• Shout-outs for great work\n\n"
            "What would you like to report?"
        )
        
        keyboard = {
            "inline_keyboard": [
                [{"text": "👥 Follow-up", "callback_data": "start_followup"}],
                [{"text": "🍳 Kitchen Issue", "callback_data": "start_kitchen"}],
                [{"text": "🏢 Facility Issue", "callback_data": "start_facility"}],
                [{"text": "⭐ Shout-out", "callback_data": "start_shoutout"}]
            ]
        }
        
        self.send_message(chat_id, text, keyboard)
    
    def _send_help(self, chat_id: int):
        """Send help message"""
        text = (
            "<b>Communication Manager Commands:</b>\n\n"
            "/report - Show main report menu\n"
            "/followup - Report about team member\n"
            "/kitchen - Report kitchen issue\n"
            "/facility - Report facility issue\n"
            "/shoutout - Give someone recognition\n"
            "/cancel_comm - Cancel current report\n"
            "/comm_help - Show this help\n"
            "/comm_status - System diagnostics\n\n"
            "<b>During Reports:</b>\n"
            "• Send photos to attach them\n"
            "• Type your description when asked\n"
            "• Use buttons to navigate\n"
            "• Type 'done' to finish photos\n"
            "• Type 'back' to go to previous step"
        )
        
        self.send_message(chat_id, text)
    
    def _send_status(self, chat_id: int):
        """Send system status"""
        try:
            # Test Notion connectivity
            employees = self.notion.get_employees()
            notion_status = "✅ Connected" if employees else "❌ Error"
            
            text = (
                f"<b>Communication Manager Status</b>\n\n"
                f"• Version: {SYSTEM_VERSION}\n"
                f"• Notion: {notion_status}\n"
                f"• Employees: {len(employees)} active\n"
                f"• Bot: ✅ Running\n\n"
                f"All systems operational"
            )
            
        except Exception as e:
            text = (
                f"<b>Communication Manager Status</b>\n\n"
                f"• Version: {SYSTEM_VERSION}\n"
                f"• Error: {str(e)}\n"
                f"• Status: ❌ Check connection"
            )
        
        self.send_message(chat_id, text)
    
    def _start_followup(self, chat_id: int, user_id: int):
        """Start follow-up flow"""
        self.conversations[user_id] = ConversationState(
            user_id=user_id,
            chat_id=chat_id, 
            command="followup",
            step="select_person"
        )
        
        employees = self.notion.get_employees()
        if not employees:
            self.send_message(chat_id, "No employees found. Contact support.")
            return
        
        keyboard = {"inline_keyboard": []}
        for emp in employees[:20]:  # Limit for keyboard
            keyboard["inline_keyboard"].append([{
                "text": emp.name,
                "callback_data": f"person_{emp.id}"
            }])
        
        keyboard["inline_keyboard"].append([{"text": "❌ Cancel", "callback_data": "cancel"}])
        
        self.send_message(chat_id, "<b>Follow-up Report</b>\n\nWho is this about?", keyboard)
    
    def _start_kitchen_issue(self, chat_id: int, user_id: int):
        """Start kitchen issue flow"""
        self.conversations[user_id] = ConversationState(
            user_id=user_id,
            chat_id=chat_id,
            command="kitchen_issue", 
            step="select_area"
        )
        
        areas = ["Prep Station", "Grill", "Fryer", "Oven", "Dishwasher", "Refrigerator", "Other"]
        keyboard = {"inline_keyboard": []}
        
        for area in areas:
            keyboard["inline_keyboard"].append([{
                "text": area,
                "callback_data": f"area_{area}"
            }])
        
        keyboard["inline_keyboard"].append([{"text": "❌ Cancel", "callback_data": "cancel"}])
        
        self.send_message(chat_id, "<b>Kitchen Issue</b>\n\nWhat area/equipment?", keyboard)
    
    def _start_facility_issue(self, chat_id: int, user_id: int):
        """Start facility issue flow"""
        self.conversations[user_id] = ConversationState(
            user_id=user_id,
            chat_id=chat_id,
            command="facility_issue",
            step="select_area"
        )
        
        areas = ["Dining Room", "Restrooms", "HVAC", "Lighting", "Plumbing", "Other"]
        keyboard = {"inline_keyboard": []}
        
        for area in areas:
            keyboard["inline_keyboard"].append([{
                "text": area, 
                "callback_data": f"area_{area}"
            }])
        
        keyboard["inline_keyboard"].append([{"text": "❌ Cancel", "callback_data": "cancel"}])
        
    def _start_shoutout(self, chat_id: int, user_id: int):
        """Start shout-out flow"""
        self.conversations[user_id] = ConversationState(
            user_id=user_id,
            chat_id=chat_id,
            command="shoutout",
            step="select_person"
        )
        
        employees = self.notion.get_employees()
        if not employees:
            self.send_message(chat_id, "No employees found. Contact support.")
            return
        
        keyboard = {"inline_keyboard": []}
        for emp in employees[:20]:  # Limit for keyboard
            keyboard["inline_keyboard"].append([{
                "text": emp.name,
                "callback_data": f"person_{emp.id}"
            }])
        
        keyboard["inline_keyboard"].append([{"text": "❌ Cancel", "callback_data": "cancel"}])
        
        self.send_message(chat_id, "<b>⭐ Shout-out</b>\n\nWho deserves recognition?", keyboard)
    
    def _select_person(self, chat_id: int, user_id: int, person_id: str):
        """Handle person selection"""
        if user_id not in self.conversations:
            return
            
        state = self.conversations[user_id]
        state.data["person_id"] = person_id
        state.step = "set_date"
        
        keyboard = {
            "inline_keyboard": [
                [{"text": "📅 Now", "callback_data": "occurred_now"}],
                [{"text": "✏️ Custom Date/Time", "callback_data": "occurred_custom"}],
                [{"text": "◀️ Back", "callback_data": "go_back"}],
                [{"text": "❌ Cancel", "callback_data": "cancel"}]
            ]
        }
        
        self.send_message(chat_id, "When did this occur?", keyboard)
    
    def _select_area(self, chat_id: int, user_id: int, area: str):
        """Handle area selection"""
        if user_id not in self.conversations:
            return
            
        state = self.conversations[user_id]
        state.data["area"] = area
        state.step = "set_date"
        
        keyboard = {
            "inline_keyboard": [
                [{"text": "📅 Now", "callback_data": "occurred_now"}],
                [{"text": "✏️ Custom Date/Time", "callback_data": "occurred_custom"}],
                [{"text": "◀️ Back", "callback_data": "go_back"}],
                [{"text": "❌ Cancel", "callback_data": "cancel"}]
            ]
        }
        
        self.send_message(chat_id, "When did this occur?", keyboard)
    
    def _set_occurred_now(self, chat_id: int, user_id: int):
        """Set occurred time to now (using configured timezone)"""
        if user_id not in self.conversations:
            return
            
        state = self.conversations[user_id]
        # Use timezone-aware time
        state.data["occurred_at"] = get_local_time(self.settings.default_timezone)
        state.step = "enter_narrative"
        
        # Show the time that will be recorded
        time_str = state.data["occurred_at"].strftime('%Y-%m-%d %H:%M')
        
        self.send_message(chat_id, 
                        f"✅ Time set to: {time_str}\n\n"
                        f"Please describe what happened (up to 500 characters):")
    
    def _set_occurred_custom(self, chat_id: int, user_id: int):
        """Set custom date/time"""
        if user_id not in self.conversations:
            return
            
        state = self.conversations[user_id]
        state.step = "enter_custom_date"
        
        self.send_message(chat_id, 
                        "Enter date and time when this occurred.\n\n"
                        "<b>Format examples:</b>\n"
                        "• 2024-12-25 (date only, assumes 12:00 PM)\n"
                        "• 2024-12-25 14:30 (date and time)\n" 
                        "• yesterday\n"
                        "• this morning\n\n"
                        "Or type 'back' to go back:")
    
    def _handle_conversation_input(self, message: Dict):
        """Handle text input during conversations"""
        user_id = message.get("from", {}).get("id")
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text", "").strip()
        
        if user_id not in self.conversations:
            return
        
        state = self.conversations[user_id]
        state.update_activity()
        
        # Handle "done" command during photo collection
        if state.step == "collect_photos" and text.lower() == "done":
            self._proceed_to_anonymous(chat_id, user_id)
            return
        
        # Handle back command
        if text.lower() == "back":
            self._go_back(chat_id, user_id)
            return
        
        if state.step == "enter_narrative":
            if len(text) > 500:
                self.send_message(chat_id, "Please keep description under 500 characters.")
                return
            
            state.data["narrative"] = text
            state.step = "collect_photos"
            
            keyboard = {
                "inline_keyboard": [
                    [{"text": "⏭️ Skip Photos", "callback_data": "skip_photos"}],
                    [{"text": "◀️ Back", "callback_data": "go_back"}],
                    [{"text": "❌ Cancel", "callback_data": "cancel"}]
                ]
            }
            
            self.send_message(chat_id, 
                            "Send photos now, or skip.\n\nType 'done' when finished with photos.", 
                            keyboard)
        
        elif state.step == "enter_custom_date":
            self._parse_custom_date(chat_id, user_id, text)
        
        elif state.step == "enter_name":
            # Handle name entry for non-anonymous reports
            if len(text.strip()) == 0:
                self.send_message(chat_id, "Please enter a valid name:")
                return
            
            # Store the reporter's name
            state.data["reporter_name"] = text.strip()
            self._show_review(chat_id, user_id)
    
    def _parse_custom_date(self, chat_id: int, user_id: int, text: str):
        """Parse custom date input"""
        if user_id not in self.conversations:
            return
            
        state = self.conversations[user_id]
        
        try:
            parsed_date = None
            
            # Handle common phrases
            if text.lower() in ["yesterday"]:
                parsed_date = datetime.now() - timedelta(days=1)
            elif text.lower() in ["this morning", "morning"]:
                parsed_date = datetime.now().replace(hour=9, minute=0)
            elif text.lower() in ["this afternoon", "afternoon"]:
                parsed_date = datetime.now().replace(hour=14, minute=0)
            elif text.lower() in ["today"]:
                parsed_date = datetime.now()
            else:
                # Try parsing date formats
                for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d"]:
                    try:
                        parsed_date = datetime.strptime(text, fmt)
                        break
                    except ValueError:
                        continue
            
            if parsed_date and parsed_date <= datetime.now():
                state.data["occurred_at"] = parsed_date
                state.step = "enter_narrative"
                self.send_message(chat_id, 
                                f"✅ Date set to: {parsed_date.strftime('%Y-%m-%d %H:%M')}\n\n"
                                f"Please describe what happened (up to 500 characters):")
            else:
                self.send_message(chat_id, 
                                "❌ Invalid date or future date not allowed.\n"
                                "Please try again or type 'back':")
                
        except Exception:
            self.send_message(chat_id, 
                            "❌ Could not understand that date format.\n"
                            "Please try again or type 'back':")
    
    def _go_back(self, chat_id: int, user_id: int):
        """Handle going back to previous step"""
        if user_id not in self.conversations:
            return
            
        state = self.conversations[user_id]
        
        # Determine where to go back based on current step
        if state.step in ["enter_custom_date", "enter_narrative"]:
            # Go back to date selection
            keyboard = {
                "inline_keyboard": [
                    [{"text": "📅 Now", "callback_data": "occurred_now"}],
                    [{"text": "✏️ Custom Date/Time", "callback_data": "occurred_custom"}],
                    [{"text": "◀️ Back", "callback_data": "go_back"}],
                    [{"text": "❌ Cancel", "callback_data": "cancel"}]
                ]
            }
            self.send_message(chat_id, "When did this occur?", keyboard)
            state.step = "set_date"
            
        elif state.step == "collect_photos":
            # Go back to narrative
            state.step = "enter_narrative"
            self.send_message(chat_id, "Please describe what happened (up to 500 characters):")
            
        elif state.step == "select_anonymous":
            # Go back to photos
            state.step = "collect_photos"
            keyboard = {
                "inline_keyboard": [
                    [{"text": "⏭️ Skip Photos", "callback_data": "skip_photos"}],
                    [{"text": "◀️ Back", "callback_data": "go_back"}],
                    [{"text": "❌ Cancel", "callback_data": "cancel"}]
                ]
            }
            self.send_message(chat_id, 
                            "Send photos now, or skip.\n\nType 'done' when finished with photos.", 
                            keyboard)
        else:
            # Default: restart the flow
            self._send_start_menu(chat_id)
    
    def _handle_photo(self, message: Dict):
        """Handle photo uploads"""
        user_id = message.get("from", {}).get("id")
        chat_id = message.get("chat", {}).get("id")
        
        if user_id not in self.conversations:
            self.send_message(chat_id, "Start a report first before adding photos.")
            return
        
        state = self.conversations[user_id]
        if state.step != "collect_photos":
            return
        
        photos = message.get("photo", [])
        if photos:
            # Get largest photo
            largest = max(photos, key=lambda p: p.get("file_size", 0))
            state.photos.append(largest["file_id"])
            
            self.send_message(chat_id, 
                            f"✅ Photo added ({len(state.photos)} total). Send more or type 'done'.")
    
    def _skip_photos(self, chat_id: int, user_id: int):
        """Skip photo collection"""
        if user_id not in self.conversations:
            return
            
        self._proceed_to_anonymous(chat_id, user_id)
    
    def _proceed_to_anonymous(self, chat_id: int, user_id: int):
        """Move to anonymous selection"""
        if user_id not in self.conversations:
            return
            
        state = self.conversations[user_id]
        state.step = "select_anonymous"
        
        keyboard = {
            "inline_keyboard": [
                [{"text": "👤 Submit Anonymously", "callback_data": "anonymous_yes"}],
                [{"text": "📝 Include My Name", "callback_data": "anonymous_no"}]
            ]
        }
        
        self.send_message(chat_id, "Submit this report anonymously?", keyboard)
    
    def _set_anonymous(self, chat_id: int, user_id: int, anonymous: bool):
        """Set anonymous flag and proceed accordingly"""
        if user_id not in self.conversations:
            return
            
        state = self.conversations[user_id]
        state.data["anonymous"] = anonymous
        
        if anonymous:
            # Skip name collection, go straight to review
            self._show_review(chat_id, user_id)
        else:
            # Collect user's name first
            state.step = "enter_name"
            self.send_message(chat_id, "Please enter your name:")
    
    def _show_review(self, chat_id: int, user_id: int):
        """Show review screen"""
        if user_id not in self.conversations:
            return
            
        state = self.conversations[user_id]
        state.step = "review"
        
        # Build review text
        item_type = {
            "followup": "Follow-up",
            "kitchen_issue": "Kitchen Issue", 
            "facility_issue": "Facility Issue",
            "shoutout": "Shout-out"
        }.get(state.command, "Report")
        
        narrative = state.data.get("narrative", "")
        photo_count = len(state.photos)
        anonymous = state.data.get("anonymous", False)
        anon_text = "Yes" if anonymous else f"No - {state.data.get('reporter_name', 'Unknown')}"
        
        review = (
            f"<b>📋 Review {item_type}</b>\n\n"
            f"<b>When:</b> {state.data.get('occurred_at', datetime.now()).strftime('%Y-%m-%d %H:%M')}\n"
            f"<b>Description:</b>\n{narrative}\n\n"
            f"<b>Photos:</b> {photo_count}\n"
            f"<b>Anonymous:</b> {anon_text}\n\n"
            f"<b>Ready to submit to Notion?</b>"
        )
        
        keyboard = {
            "inline_keyboard": [
                [{"text": "✅ Submit to Notion", "callback_data": "submit"}],
                [{"text": "◀️ Back to Edit", "callback_data": "go_back"}],
                [{"text": "❌ Cancel Report", "callback_data": "cancel"}]
            ]
        }
        
        self.send_message(chat_id, review, keyboard)
    
    def _submit_report(self, chat_id: int, user_id: int):
        """Submit the report"""
        if user_id not in self.conversations:
            return
        
        state = self.conversations[user_id]
        
        try:
            # Process photos
            photo_urls = []
            for file_id in state.photos:
                url = self.notion.upload_photo_to_notion(file_id, self.settings.telegram_bot_token)
                if url:
                    photo_urls.append(url)
            
            # Determine item type
            item_type_map = {
                "followup": ItemType.FOLLOWUP,
                "kitchen_issue": ItemType.KITCHEN_ISSUE,
                "facility_issue": ItemType.FACILITY_ISSUE,
                "shoutout": ItemType.SHOUTOUT
            }
            item_type = item_type_map[state.command]
            
            # Create in Notion
            page_id = self.notion.create_communication_item(
                item_type=item_type,
                narrative=state.data["narrative"],
                person_id=state.data.get("person_id"),
                area=state.data.get("area"),
                occurred_at=state.data.get("occurred_at"),
                photo_urls=photo_urls,
                anonymous=state.data.get("anonymous", False),
                reporter_name=state.data.get("reporter_name")  # Pass reporter name
            )
            
            if page_id:
                # Send confirmation to user
                self.send_message(chat_id, 
                                f"✅ <b>Report Submitted!</b>\n\n"
                                f"Your {item_type.value.lower()} has been recorded.\n"
                                f"Report ID: {page_id[:8]}...\n\n"
                                f"Thank you for your feedback!")
                
                # Special handling for shout-outs - send to shoutout chat
                if item_type == ItemType.SHOUTOUT and self.settings.shoutout_chat_id:
                    self._send_shoutout_notification(state, photo_urls)
                    
            else:
                self.send_message(chat_id, "❌ Failed to submit. Please try again.")
            
        except Exception as e:
            self.logger.error(f"Submit error: {e}")
            self.send_message(chat_id, "❌ Error submitting report. Please try again.")
        
        # Clean up conversation
        if user_id in self.conversations:
            del self.conversations[user_id]
    
    def _send_shoutout_notification(self, state: ConversationState, photo_urls: List[str]):
        """Send shout-out to the designated chat with images"""
        try:
            # Check if shoutout chat is configured
            if not self.settings.shoutout_chat_id:
                self.logger.warning("Shoutout chat ID not configured - skipping notification")
                return
            
            # Get person name
            employees = self.notion.get_employees()
            person_name = next((emp.name for emp in employees if emp.id == state.data.get("person_id")), "Unknown")
            
            # Build shout-out message
            occurred_date = state.data.get("occurred_at", datetime.now()).strftime('%B %d, %Y')
            narrative = state.data["narrative"]
            reporter_name = state.data.get("reporter_name", "")
            is_anonymous = state.data.get("anonymous", False)
            
            # Build the shout-out message with optional reporter attribution
            shoutout_message = (
                f"⭐ <b>SHOUT-OUT!</b> ⭐\n\n"
                f"👏 <b>{person_name}</b> deserves recognition!\n\n"
                f"📅 Date: {occurred_date}\n\n"
                f"💬 <b>What they did:</b>\n{narrative}\n\n"
            )
            
            # Add reporter attribution if not anonymous
            if not is_anonymous and reporter_name:
                shoutout_message += f"📝 <b>Recognized by:</b> {reporter_name}\n\n"
            
            shoutout_message += f"🎉 Keep up the amazing work!"
            
            self.logger.info(f"Sending shoutout to chat {self.settings.shoutout_chat_id}")
            
            if photo_urls:
                # Send first photo with the caption message using sendPhoto
                photo_data = {
                    "chat_id": self.settings.shoutout_chat_id,
                    "photo": photo_urls[0],
                    "caption": shoutout_message,
                    "parse_mode": "HTML"
                }
                
                self.logger.info(f"Attempting to send photo with shoutout message")
                
                # Use requests directly to send photo
                response = requests.post(
                    f"{self.base_url}/sendPhoto",
                    json=photo_data,
                    timeout=30
                )
                
                if response.ok:
                    self.logger.info("Photo with shoutout sent successfully")
                    success = True
                else:
                    self.logger.error(f"Failed to send photo: HTTP {response.status_code} - {response.text}")
                    success = False
                
                # Send additional photos if there are more than one
                if success and len(photo_urls) > 1:
                    for i, photo_url in enumerate(photo_urls[1:], 2):
                        try:
                            additional_photo_data = {
                                "chat_id": self.settings.shoutout_chat_id,
                                "photo": photo_url,
                                "caption": f"📸 Photo {i} from the shout-out"
                            }
                            additional_response = requests.post(
                                f"{self.base_url}/sendPhoto",
                                json=additional_photo_data,
                                timeout=30
                            )
                            if not additional_response.ok:
                                self.logger.error(f"Failed to send additional photo {i}: {additional_response.text}")
                        except Exception as photo_error:
                            self.logger.error(f"Error sending additional shout-out photo: {photo_error}")
            else:
                # Send text message only if no photos
                self.logger.info("Sending text-only shoutout message")
                success = self.send_message(self.settings.shoutout_chat_id, shoutout_message)
                if not success:
                    self.logger.error("Failed to send text-only shoutout message")
            
            if success:
                self.logger.info(f"Shout-out notification sent successfully to chat {self.settings.shoutout_chat_id}")
            else:
                self.logger.error("Failed to send shout-out notification")
                
        except Exception as e:
            self.logger.error(f"Error sending shout-out notification: {e}", exc_info=True)
    
    def _cancel_conversation(self, chat_id: int, user_id: int):
        """Cancel active conversation"""
        if user_id in self.conversations:
            del self.conversations[user_id]
        
        self.send_message(chat_id, 
                        "❌ Report cancelled.\n\n"
                        "Use /report to start a new report.")
        
        # Show the main menu after cancelling
        self._send_start_menu(chat_id)

# ===== MAIN APPLICATION =====

class CommunicationApp:
    """Main application for Railway deployment"""
    
    def __init__(self):
        self.logger = logging.getLogger('app')
        self.settings = Settings()
        self.running = False
        self.server = None
        
        # Initialize components
        self.notion = NotionClient(
            self.settings.notion_token,
            self.settings.employees_db_id, 
            self.settings.communication_db_id
        )
        
        self.bot = TelegramBot(self.settings, self.notion)
        
        self.logger.info(f"Communication Manager v{SYSTEM_VERSION} initialized")
        
        # Set up signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.stop()
        sys.exit(0)
    
    def stop(self):
        """Stop all components"""
        self.running = False
        
        if self.bot:
            self.bot.stop()
        
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        
        self.logger.info("Application stopped")
    
    def run(self):
        """Run the application"""
        self.logger.info("Starting Communication Manager Bot")
        self.running = True
        
        # Start webhook server for Railway health checks
        from http.server import HTTPServer, BaseHTTPRequestHandler
        
        class HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/health':
                    self.send_response(200)
                    self.send_header('Content-type', 'text/plain')
                    self.end_headers()
                    self.wfile.write(b'OK')
                else:
                    self.send_response(404)
                    self.end_headers()
            
            def log_message(self, format, *args):
                pass  # Suppress HTTP logs
        
        # Start health check server in background
        try:
            self.server = HTTPServer(('0.0.0.0', self.settings.port), HealthHandler)
            server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            server_thread.start()
            
            self.logger.info(f"Health check server running on port {self.settings.port}")
            
            # Start bot polling (this will block until interrupted)
            self.bot.start_polling()
            
        except KeyboardInterrupt:
            self.logger.info("KeyboardInterrupt received")
        finally:
            self.stop()

# ===== ENTRY POINT =====

def health_check():
    """Simple health check for testing"""
    try:
        settings = Settings()
        
        # Test Notion connection
        notion = NotionClient(
            settings.notion_token,
            settings.employees_db_id,
            settings.communication_db_id
        )
        
        employees = notion.get_employees()
        print(f"✅ Health check passed - Found {len(employees)} employees")
        return True
        
    except Exception as e:
        print(f"❌ Health check failed: {e}")
        return False

def main():
    """Entry point for Railway deployment"""
    if len(sys.argv) > 1 and sys.argv[1] == '--health-check':
        success = health_check()
        sys.exit(0 if success else 1)
    
    app = CommunicationApp()
    
    try:
        app.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
    finally:
        app.stop()

if __name__ == "__main__":
    main()