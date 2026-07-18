"""
M.A.R.K. [MODULAR ASSISTANT FOR RESOURCE & KNOWLEDGE]
=====================================================
Theme: Deep Cyber Cyan 3D Mainframe Edition
Database Status: PURGED (Session-Only Memory Profile)
Requirements:
    python -m pip install requests SpeechRecognition edge-tts pygame pyaudio pycaw comtypes python-dotenv

Build notes (this revision):
    - FIXED "AI mainframe connection error": your Groq model list pointed at
      llama-3.3-70b-versatile, which Groq deprecated on 2026-06-17, and
      mixtral-8x7b-32768, which was decommissioned long before that. Every
      request was hitting a dead model ID and failing with an HTTP 400 that
      got swallowed into a generic message. MODEL_FALLBACK_LIST now points at
      currently-supported Groq models, and run_agent() actually walks the
      fallback list on failure instead of only ever trying entry [0]. Errors
      are also surfaced with their real status code / reason instead of a
      single generic string, so future breakage is easy to diagnose.
    - English-only: all Hindi detection, Hindi voices, language-switching
      commands/tool, and the dual-language speech recognition path have been
      removed. M.A.R.K. now always replies and listens in English.
    - Multi-agent reasoning unchanged: the AI is given TOOLS (web_search,
      system_task, send_whatsapp_message) and decides for itself which ones
      to call, in what order, to satisfy a request (Groq function calling).
    - WhatsApp messaging: the agent can DRAFT a WhatsApp message, but it can
      never send it directly. A confirmation popup always appears first,
      showing the contact name, phone number, and message text (all
      editable). Clicking SEND opens WhatsApp Web/App with the message
      pre-filled via a wa.me link — you still tap send inside WhatsApp
      itself, so nothing is auto-sent by simulated keystrokes.

Before using WhatsApp features, fill in the CONTACTS dictionary below with
real names/numbers. Before running at all, put a valid GROQ_API_KEY in a
.env file next to this script.
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import time
import math
import random
import re
import json
import difflib
import urllib.parse
import asyncio
import tempfile
import uuid
import requests
import speech_recognition as sr
import subprocess
import os
import edge_tts
import pygame
import webbrowser
from dotenv import load_dotenv

# ==================== THEME: DEEP CYBER CYAN ====================
COLOR_PRIMARY = "#00E5FF"     # High-voltage Cyber Cyan
COLOR_BG_DARK = "#000508"     # Pure Deep Space Black
COLOR_PANEL_BG = "#03141E"    # Translucent Slate Blue Fill
COLOR_BORDER = "#005F73"      # Dark Cyan Slate Border
COLOR_TEXT_LIGHT = "#E0FAFF"  # Crisp Ice White
COLOR_TEXT_MUTED = "#0A859E"  # Cyan Stream Muted Tech
COLOR_CARD_BG = "#010A10"     # Terminal Box Background
COLOR_GRID_LINE = "#001B24"   # Cybernetic Background Grid Line
COLOR_VECTOR_LINE = "#004052" # Rotational Perspective Vector Edge

pygame.mixer.init()

# ==================== AI CORE CONFIGURATION ====================
load_dotenv()  # This looks for a .env file in your folder
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Current, actively-supported Groq chat/tool-use models (checked 2026-07).
# llama-3.3-70b-versatile and mixtral-8x7b-32768 are dead — do not add them
# back without first checking https://console.groq.com/docs/deprecations
MODEL_FALLBACK_LIST = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.6-27b"]

TTS_VOICES = {
    "Jarvis (UK Male)": "en-GB-RyanNeural",
    "Sonia (UK Female)": "en-GB-SoniaNeural",
    "Aria (US Female)": "en-US-AriaNeural",
    "Guy (US Standard Male)": "en-US-GuyNeural",
}
SELECTED_VOICE = "en-GB-RyanNeural"

MIC_DEVICE_INDEX = None

WAKE_PATTERNS = [r"\bhello mark\b", r"\bwhats up mark\b", r"\bwhat's up mark\b", r"\bmark\b"]
WAKE_REGEX = re.compile("|".join(WAKE_PATTERNS))

STOP_PATTERNS = [r"\bmark stop\b", r"\bstop\b"]
STOP_REGEX = re.compile("|".join(STOP_PATTERNS))

# ==================== WHATSAPP CONTACTS ====================
# Fill this in with real contacts: "name": "+<countrycode><number>"
# The agent will look names up here when you don't give a number explicitly.
CONTACTS = {
    "meet": "+919818902250",
    "uzzaif": "+919911683671",
    "mayank": "+919818693601",
    "harsh": "+919289715645",
    "khushi": "+919818934341",
}

# ==================== AGENT TOOLS SCHEMA ====================
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the live web for current information, facts, news, or answers to research questions the model doesn't already know.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system_task",
            "description": "Perform a local system action on the user's machine: open a website or app, change system volume, play a song on YouTube, or change the TTS voice profile.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The raw natural-language command describing the action, e.g. 'open github' or 'volume up'.",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_whatsapp_message",
            "description": (
                "Draft a WhatsApp text message for a contact. This tool NEVER sends anything by "
                "itself — it always opens an on-screen confirmation popup showing the contact name, "
                "phone number, and message text, all of which the user can edit. Nothing is sent "
                "unless the user explicitly clicks SEND in that popup. Never tell the user a message "
                "was sent unless the tool result explicitly confirms it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_name": {
                        "type": "string",
                        "description": "The name of the contact as the user refers to them, e.g. 'mom' or 'Rahul'.",
                    },
                    "phone_number": {
                        "type": "string",
                        "description": "Phone number in international format if known, e.g. +91XXXXXXXXXX. Leave empty to look it up by contact_name.",
                    },
                    "message": {
                        "type": "string",
                        "description": "The message text to send, drafted on the user's behalf.",
                    },
                },
                "required": ["contact_name", "message"],
            },
        },
    },
]


def quick_web_search(query):
    try:
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=5,
        )
        data = resp.json()
        return data.get("AbstractText") or data.get("Answer") or ""
    except Exception:
        return ""


# ==================== MAIN APPLICATION INTERFACE ====================
class Mark3DMainframe:
    def set_voice(self, voice_name):
        voice_map = {
            "jarvis": "en-GB-RyanNeural",
            "sonia": "en-GB-SoniaNeural",
            "aria": "en-US-AriaNeural",
            "guy": "en-US-GuyNeural",
        }
        name = voice_name.lower().strip()
        if name in voice_map:
            self.selected_voice = voice_map[name]
            self.log_raw(f"[SYSTEM]: Voice profile shifted to {voice_name}.")
            return True
        return False

    def run_task(self, command):
        cmd = command.lower()

        # 1. Open Sites & Software (Added flexibility for "open" or "launch")
        if "open" in cmd or "launch" in cmd:
            target = cmd.replace("open", "").replace("launch", "").strip()

            # 1. Check your predefined list
            sites = {
                "youtube": "https://www.youtube.com",
                "calendar": "https://calendar.google.com",
                "github": "https://github.com",
                "docs": "https://docs.google.com"
            }

            if target in sites:
                webbrowser.open(sites[target])
                return f"Opening {target}."

            # 2. If not in list, check if it's a URL or a System App
            elif "." in target or "www" in target:
                # If it looks like a website, force it to open in a browser
                if not target.startswith("http"):
                    target = "https://" + target
                webbrowser.open(target)
                return f"Navigating to {target}."

            else:
                # 3. Fallback to system application
                try:
                    subprocess.Popen([target], shell=True)
                    return f"Launching {target}."
                except Exception:
                    return f"I couldn't locate '{target}'. It is not a known site or application."

        # 2. Play Music
        elif "play" in cmd:
            song = cmd.replace("play", "").strip()
            # A simple, clean way to open a search result
            url = f"https://www.youtube.com/results?search_query={song.replace(' ', '+')}"
            os.startfile(url)  # This is a Windows native command
            return f"Opening {song}."

        # 3. Change Voice
        elif "change voice to" in cmd:
            target_voice = cmd.replace("change voice to", "").strip()
            if self.set_voice(target_voice):
                return f"Voice profile updated to {target_voice}."
            else:
                return "I don't recognize that voice profile."

    # ==================== WHATSAPP CONFIRMATION FLOW ====================
    def request_whatsapp_confirmation(self, contact_name, phone_number, message):
        """
        Builds a modal confirmation popup on the Tk main thread and blocks
        (safely, from a background thread) until the user clicks SEND or CANCEL.
        Returns a dict describing what the user decided.
        """
        result_holder = {"action": "cancel"}
        done_event = threading.Event()

        def build_popup():
            popup = tk.Toplevel(self.root)
            popup.title("WhatsApp Send Confirmation")
            popup.configure(bg=COLOR_BG_DARK)
            popup.geometry("440x420")
            popup.resizable(False, False)
            popup.transient(self.root)
            popup.grab_set()

            header = tk.Label(
                popup, text=" ⚠ CONFIRM WHATSAPP MESSAGE",
                bg=COLOR_BORDER, fg=COLOR_TEXT_LIGHT,
                font=("Courier New", 10, "bold"), anchor="w"
            )
            header.pack(fill=tk.X)

            body = tk.Frame(popup, bg=COLOR_BG_DARK)
            body.pack(fill=tk.BOTH, expand=True, padx=15, pady=10)

            tk.Label(body, text="Contact name:", bg=COLOR_BG_DARK, fg=COLOR_PRIMARY,
                     font=("Courier New", 9, "bold")).pack(anchor="w")
            name_var = tk.StringVar(value=contact_name)
            name_entry = tk.Entry(body, textvariable=name_var, font=("Courier New", 10),
                                   bg=COLOR_BG_DARK, fg=COLOR_TEXT_LIGHT, insertbackground=COLOR_PRIMARY,
                                   relief=tk.SOLID, bd=1, highlightthickness=1,
                                   highlightbackground=COLOR_BORDER, highlightcolor=COLOR_PRIMARY)
            name_entry.pack(fill=tk.X, pady=(2, 10), ipady=4)

            tk.Label(body, text="Phone number (with country code):", bg=COLOR_BG_DARK, fg=COLOR_PRIMARY,
                     font=("Courier New", 9, "bold")).pack(anchor="w")
            number_var = tk.StringVar(value=phone_number)
            number_entry = tk.Entry(body, textvariable=number_var, font=("Courier New", 10),
                                     bg=COLOR_BG_DARK, fg=COLOR_TEXT_LIGHT, insertbackground=COLOR_PRIMARY,
                                     relief=tk.SOLID, bd=1, highlightthickness=1,
                                     highlightbackground=COLOR_BORDER, highlightcolor=COLOR_PRIMARY)
            number_entry.pack(fill=tk.X, pady=(2, 10), ipady=4)

            tk.Label(body, text="Message:", bg=COLOR_BG_DARK, fg=COLOR_PRIMARY,
                     font=("Courier New", 9, "bold")).pack(anchor="w")
            msg_text = tk.Text(body, height=7, font=("Courier New", 10), bg=COLOR_BG_DARK,
                                fg=COLOR_TEXT_LIGHT, insertbackground=COLOR_PRIMARY, wrap=tk.WORD,
                                relief=tk.SOLID, bd=1, highlightthickness=1,
                                highlightbackground=COLOR_BORDER, highlightcolor=COLOR_PRIMARY)
            msg_text.insert("1.0", message)
            msg_text.pack(fill=tk.BOTH, expand=True, pady=(2, 10))

            warn = tk.Label(
                body, text="Nothing is sent until you click SEND.",
                bg=COLOR_BG_DARK, fg=COLOR_TEXT_MUTED, font=("Courier New", 8, "italic")
            )
            warn.pack(anchor="w", pady=(0, 8))

            btn_bar = tk.Frame(body, bg=COLOR_BG_DARK)
            btn_bar.pack(fill=tk.X)

            def on_send():
                if not number_var.get().strip():
                    messagebox.showwarning("Missing number", "Please enter a phone number before sending.", parent=popup)
                    return
                result_holder["action"] = "send"
                result_holder["name"] = name_var.get().strip()
                result_holder["number"] = number_var.get().strip()
                result_holder["message"] = msg_text.get("1.0", tk.END).strip()
                popup.destroy()
                done_event.set()

            def on_cancel():
                result_holder["action"] = "cancel"
                popup.destroy()
                done_event.set()

            popup.protocol("WM_DELETE_WINDOW", on_cancel)

            cancel_btn = tk.Button(
                btn_bar, text="[CANCEL]", bg=COLOR_BG_DARK, fg=COLOR_PRIMARY,
                activebackground=COLOR_PRIMARY, activeforeground=COLOR_BG_DARK,
                font=("Courier New", 10, "bold"), relief=tk.SOLID, bd=1,
                highlightthickness=1, command=on_cancel
            )
            cancel_btn.pack(side=tk.LEFT, ipady=5, ipadx=10)

            send_btn = tk.Button(
                btn_bar, text="[SEND]", bg=COLOR_BG_DARK, fg="#00FF7F",
                activebackground="#00FF7F", activeforeground=COLOR_BG_DARK,
                font=("Courier New", 10, "bold"), relief=tk.SOLID, bd=1,
                highlightthickness=1, command=on_send
            )
            send_btn.pack(side=tk.RIGHT, ipady=5, ipadx=10)

        self.root.after(0, build_popup)
        finished = done_event.wait(timeout=180)
        if not finished:
            return {"action": "timeout"}
        return result_holder

    def resolve_contact(self, contact_name):
        """
        Looks up a contact_name against CONTACTS. Tries an exact (case-insensitive)
        match first, then falls back to fuzzy matching so minor spelling differences
        still resolve correctly. Returns (resolved_name, phone_number) — phone_number
        is "" if nothing matched.
        """
        key = (contact_name or "").strip().lower()
        if not key or not CONTACTS:
            return contact_name, ""

        if key in CONTACTS:
            return key, CONTACTS[key]

        # Fuzzy fallback: handles things like "uzaif" vs "uzzaif".
        close = difflib.get_close_matches(key, CONTACTS.keys(), n=1, cutoff=0.55)
        if close:
            return close[0], CONTACTS[close[0]]

        return contact_name, ""

    def tool_send_whatsapp(self, contact_name="", phone_number="", message=""):
        if not phone_number:
            resolved_name, resolved_number = self.resolve_contact(contact_name)
            if resolved_number:
                contact_name, phone_number = resolved_name, resolved_number

        result = self.request_whatsapp_confirmation(contact_name, phone_number, message)

        if result.get("action") == "send":
            digits_only = re.sub(r"[^\d+]", "", result["number"]).lstrip("+")
            if not digits_only:
                return "STATUS: NOT_SENT — the phone number was invalid, so nothing was opened."
            encoded_msg = urllib.parse.quote(result["message"])
            wa_url = f"https://wa.me/{digits_only}?text={encoded_msg}"
            try:
                webbrowser.open(wa_url)
                return (
                    f"STATUS: OPENED_FOR_SEND — WhatsApp was opened with a message pre-filled for "
                    f"{result['name']} ({result['number']}). The user still needs to press the send "
                    f"button inside WhatsApp itself to actually deliver it; it has NOT been sent yet."
                )
            except Exception as e:
                return f"STATUS: FAILED — couldn't open WhatsApp: {e}"
        elif result.get("action") == "cancel":
            return "STATUS: CANCELLED — the user reviewed the draft and cancelled it. It was NOT sent."
        else:
            return "STATUS: TIMED_OUT — the confirmation popup got no response. It was NOT sent."

    # ==================== MULTI-AGENT REASONING ====================
    def build_system_prompt(self):
        base = (
            "You are M.A.R.K., a brilliant, exceptionally sharp, and highly informal AI companion. "
            "Drop all stiff, formal, military, or tactical jargon. Never say 'affirmative' or 'operator'. "
            "Speak to your developer like an awesome, supportive peer. Keep answers punchy, clever, "
            "and conversational without the robotic attitude. Do not make assumptions about their personal data. "
            "Always reply in English.\n\n"
            "You have tools available: web_search, system_task, and send_whatsapp_message. For "
            "multi-step requests, break the problem down and call the right tool(s) before giving "
            "your final answer. The send_whatsapp_message tool only ever drafts a message for human "
            "confirmation in an on-screen popup — never tell the user a WhatsApp message was sent "
            "unless the tool result explicitly says so.\n\n"
            "WHATSAPP RULES:\n"
            "- If the user hasn't told you what the message should say, do NOT invent placeholder "
            "text and do NOT call send_whatsapp_message yet — first ask the user what they'd like "
            "the message to say.\n"
            "- The send_whatsapp_message tool result always starts with a STATUS field "
            "(OPENED_FOR_SEND, CANCELLED, TIMED_OUT, NOT_SENT, or FAILED). Relay that outcome to the "
            "user plainly and accurately — do not say 'sent' unless it says OPENED_FOR_SEND, and even "
            "then remind them they still need to press send inside WhatsApp itself."
        )
        if CONTACTS:
            contact_list = ", ".join(sorted(CONTACTS.keys()))
            base += f"\n\nKnown WhatsApp contacts (use these exact spellings as contact_name): {contact_list}."
        return base

    def execute_tool(self, name, args):
        if name == "web_search":
            return quick_web_search(args.get("query", "")) or "No results."
        elif name == "system_task":
            return self.run_task(args.get("command", "")) or "Task executed."
        elif name == "send_whatsapp_message":
            return self.tool_send_whatsapp(
                args.get("contact_name", ""), args.get("phone_number", ""), args.get("message", "")
            )
        return "Unknown tool."

    def _call_groq(self, model, msgs):
        """Single request/response round trip against one model. Raises on
        network failure; returns (message_dict, None) on success or
        (None, error_string) on a non-200 HTTP response."""
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": msgs,
            "temperature": 0.5,
            "tools": TOOLS_SCHEMA,
            "tool_choice": "auto",
        }
        response = requests.post(url, json=payload, headers=headers, timeout=25)
        if response.status_code != 200:
            err = f"HTTP {response.status_code} from model '{model}': {response.text[:300]}"
            print(f"DEBUG GROQ ERROR: {err}")
            return None, err
        return response.json()["choices"][0]["message"], None

    def run_agent(self, prompt_text):
        if not GROQ_API_KEY:
            return "Hey, looks like the Groq API key is missing — add GROQ_API_KEY to your .env file.", "None"

        msgs = [
            {"role": "system", "content": self.build_system_prompt()},
            {"role": "user", "content": prompt_text},
        ]

        # Try each model in the fallback list in order. If one is decommissioned
        # or rate-limited, move on to the next instead of just failing outright.
        active_model = None
        last_error = None
        for candidate in MODEL_FALLBACK_LIST:
            try:
                probe_msgs = msgs.copy()
                message, err = self._call_groq(candidate, probe_msgs)
            except requests.exceptions.RequestException as e:
                last_error = f"network error talking to '{candidate}': {e}"
                continue
            if err:
                last_error = err
                continue
            active_model = candidate
            msgs.append(message)
            break

        if not active_model:
            self.log_raw(f"[SYSTEM]: All models failed — {last_error}")
            return (
                "AI mainframe connection error — every configured model failed. "
                f"Last error: {last_error}"
            ), "None"

        for _ in range(3):  # Max additional turns to resolve tool calls
            message = msgs[-1]

            if message.get("tool_calls"):
                for tc in message["tool_calls"]:
                    fname = tc["function"]["name"]
                    try:
                        fargs = json.loads(tc["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        fargs = {}

                    self.log_raw(f"[AGENT]: Invoking {fname}...")
                    tool_result = self.execute_tool(fname, fargs)

                    msgs.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": fname,
                        "content": str(tool_result),
                    })

                try:
                    next_message, err = self._call_groq(active_model, msgs)
                except requests.exceptions.RequestException as e:
                    return f"AI mainframe connection error — network failure mid-conversation: {e}", active_model
                if err:
                    return f"AI mainframe connection error — {err}", active_model
                msgs.append(next_message)
            else:
                return message.get("content", ""), active_model

        return "Reasoning complete.", active_model

    def __init__(self, root):
        self.root = root
        self.root.title("M.A.R.K. [MODULAR ASSISTANT FOR RESOURCE & KNOWLEDGE] — Cyber Cyan 3D Mainframe")
        self.root.geometry("1300x790")
        self.root.configure(bg=COLOR_BG_DARK)
        self.root.resizable(False, False)

        # Voice Configuration
        self.is_active = False
        self.selected_voice = "en-GB-RyanNeural"  # Default voice initialized here

        self.awaiting_command = False
        self.angle_y = 0.0
        self.angle_x = 0.25

        self.build_3d_vector_nodes()
        self.build_ui_layout()
        self.start_voice_thread()
        self.cycle_matrix_render()

        if not GROQ_API_KEY:
            self.log_raw("[SYSTEM]: WARNING — no GROQ_API_KEY found in .env. AI reasoning will not work.")

    def build_ui_layout(self):
        self.left_panel = tk.Frame(self.root, width=500, height=770, bg=COLOR_BG_DARK, bd=2, relief=tk.SOLID, highlightbackground=COLOR_BORDER, highlightthickness=1)
        self.left_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.left_panel.pack_propagate(False)

        self.right_panel = tk.Frame(self.root, width=780, height=770, bg=COLOR_BG_DARK)
        self.right_panel.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.right_panel.pack_propagate(False)

        self.title_lbl = tk.Label(self.left_panel, text="[ NEURAL JURISDICTION MATRIX ]", bg=COLOR_BG_DARK, fg=COLOR_PRIMARY, font=("Courier New", 12, "bold"))
        self.title_lbl.pack(fill=tk.X, pady=10)

        self.canvas = tk.Canvas(self.left_panel, width=480, height=680, bg=COLOR_BG_DARK, highlightthickness=1, highlightbackground=COLOR_BORDER)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self.console_box = tk.Frame(self.right_panel, bg=COLOR_BG_DARK, bd=2, relief=tk.SOLID, highlightbackground=COLOR_BORDER, highlightthickness=1)
        self.console_box.pack(fill=tk.BOTH, expand=True, padx=5, pady=(5, 10))

        self.console_header = tk.Label(self.console_box, text=" ⚡ AGENT RESPONSE", bg=COLOR_BORDER, fg=COLOR_TEXT_LIGHT, font=("Courier New", 10, "bold"), anchor="w")
        self.console_header.pack(fill=tk.X)

        self.chat_display = scrolledtext.ScrolledText(
            self.console_box, wrap=tk.WORD, state='disabled',
            font=("Courier New", 10), bg=COLOR_BG_DARK, fg=COLOR_PRIMARY,
            insertbackground=COLOR_PRIMARY, relief=tk.FLAT, highlightthickness=0
        )
        self.chat_display.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.chat_display.tag_config("normal_style", foreground=COLOR_PRIMARY)
        self.chat_display.tag_config("user_style", foreground="#FFFFFF", font=("Courier New", 10, "bold"))
        self.chat_display.tag_config("bot_header_style", foreground=COLOR_PRIMARY, font=("Courier New", 10, "bold"))
        self.chat_display.tag_config("bold_style", foreground="#FFFFFF", font=("Courier New", 10, "bold"))
        self.chat_display.tag_config("heading_style", foreground="#FFFFFF", font=("Courier New", 11, "bold"))
        self.chat_display.tag_config("divider_style", foreground=COLOR_BORDER)

        self.input_box = tk.Frame(self.right_panel, bg=COLOR_BG_DARK, bd=2, relief=tk.SOLID, highlightbackground=COLOR_BORDER, highlightthickness=1)
        self.input_box.pack(fill=tk.X, padx=5, pady=(0, 5))

        self.input_header = tk.Label(self.input_box, text=" 💾 INPUT ", bg=COLOR_BORDER, fg=COLOR_TEXT_LIGHT, font=("Courier New", 10, "bold"), anchor="w")
        self.input_header.pack(fill=tk.X)

        self.action_bar = tk.Frame(self.input_box, bg=COLOR_BG_DARK)
        self.action_bar.pack(fill=tk.X, padx=10, pady=15)

        self.search_var = tk.BooleanVar(value=False)
        self.search_check = tk.Checkbutton(
            self.action_bar, text="[LIVE_SEARCH]", variable=self.search_var,
            bg=COLOR_BG_DARK, fg=COLOR_PRIMARY, selectcolor=COLOR_BG_DARK,
            activebackground=COLOR_BG_DARK, activeforeground=COLOR_PRIMARY,
            font=("Courier New", 9, "bold")
        )
        self.search_check.pack(side=tk.LEFT, padx=(0, 15))

        self.entry_box = tk.Entry(
            self.action_bar, font=("Courier New", 11), bg=COLOR_BG_DARK, fg=COLOR_PRIMARY, insertbackground=COLOR_PRIMARY,
            relief=tk.SOLID, bd=1, highlightthickness=1, highlightcolor=COLOR_PRIMARY, highlightbackground=COLOR_BORDER
        )
        self.entry_box.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=8, padx=5)
        self.entry_box.bind("<Return>", self.handle_submit)

        self.send_button = tk.Button(
            self.action_bar, text="[RUN ANALYSIS]", bg=COLOR_BG_DARK, fg=COLOR_PRIMARY,
            activebackground=COLOR_PRIMARY, activeforeground=COLOR_BG_DARK,
            font=("Courier New", 10, "bold"), relief=tk.SOLID, bd=1,
            highlightthickness=1, command=self.handle_submit
        )
        self.send_button.pack(side=tk.RIGHT, padx=5, ipady=5, ipadx=15)

        welcome_text = """ACCESS GRANTED // M.A.R.K [MODULAR ASSISTANT FOR RESEARCH AND KNOWLEDGE]
[SYSTEM_STATUS] : ACTIVE // 3D NEURAL PERSPECTIVE LOADED // MULTI-AGENT TOOLS ONLINE

Ready to roll. Drop whatever you're working on below, and let's build something cool.
Try: "search the web for X and draft a whatsapp to mom about it" or "open github"."""
        self.chat_display.configure(state='normal')
        self.chat_display.insert(tk.END, "[SYSTEM_NOTIFICATION]:\n", "bot_header_style")
        self.chat_display.configure(state='disabled')
        self.insert_markdown_text(self.chat_display, welcome_text)
        self.chat_display.configure(state='normal')
        self.chat_display.insert(tk.END, "\n" + "="*70 + "\n", "divider_style")
        self.chat_display.configure(state='disabled')

    def insert_markdown_text(self, widget, text):
        widget.configure(state='normal')
        lines = text.split('\n')
        for line in lines:
            if line.strip() in ['***', '---', '___']:
                widget.insert(tk.END, "\n" + "="*70 + "\n\n", "divider_style")
                continue
            heading_match = re.match(r'^(#{1,6})\s+(.*)$', line)
            if heading_match:
                widget.insert(tk.END, f"\n>>> {heading_match.group(2).upper()} <<<\n", "heading_style")
                continue
            parts = re.split(r'(\*\*.*?\*\*)', line)
            for part in parts:
                if part.startswith('**') and part.endswith('**'):
                    widget.insert(tk.END, part[2:-2], "bold_style")
                else:
                    widget.insert(tk.END, part, "normal_style")
            widget.insert(tk.END, "\n")
        widget.configure(state='disabled')
        widget.see(tk.END)

    def build_3d_vector_nodes(self):
        self.nodes_3d = []
        labels = [
            "SYS.CORE_M_01", "SYS.NLU_PARSER", "SYS.RESOURCE_ALLOC",
            "SYS.LIVE_INTEL", "SYS.EXEC_SANDBOX", "SYS.CRYPTO_SEC",
            "SYS.K_GRAPH", "SYS.TTS_ENGINE", "SYS.API_BRIDGE", "SYS.HEURISTIC_GEN",
            "SYS.LOG_STREAM"
        ]

        for i, name in enumerate(labels):
            self.nodes_3d.append({
                "x": int(150 * math.cos(i * (2 * math.pi / len(labels)))),
                "y": int(150 * math.sin(i * (2 * math.pi / len(labels)))),
                "z": random.randint(-40, 40),
                "label": name,
                "is_core": True
            })

        random.seed(101)
        for _ in range(35):
            self.nodes_3d.append({
                "x": random.randint(-200, 200),
                "y": random.randint(-240, 240),
                "z": random.randint(-80, 80),
                "label": "",
                "is_core": False
            })

    def cycle_matrix_render(self):
        self.canvas.delete("all")
        for i in range(0, 500, 30):
            self.canvas.create_line(i, 0, i, 680, fill=COLOR_GRID_LINE, width=1)
        for j in range(0, 680, 30):
            self.canvas.create_line(0, j, 500, j, fill=COLOR_GRID_LINE, width=1)

        c_x, c_y = 250, 340
        projected = []

        cos_y, sin_y = math.cos(self.angle_y), math.sin(self.angle_y)
        cos_x, sin_x = math.cos(self.angle_x), math.sin(self.angle_x)

        for node in self.nodes_3d:
            x1 = node["x"] * cos_y - node["z"] * sin_y
            z1 = node["x"] * sin_y + node["z"] * cos_y
            y2 = node["y"] * cos_x - z1 * sin_x
            z2 = node["y"] * sin_x + z1 * cos_x

            dist = 400
            scale = dist / (dist + z2)

            s_x = int(c_x + x1 * scale)
            s_y = int(c_y + y2 * scale)
            projected.append((s_x, s_y, scale, node["is_core"], node["label"]))

        for i, p1 in enumerate(projected):
            for j, p2 in enumerate(projected):
                if i < j:
                    dx = p1[0] - p2[0]
                    dy = p1[1] - p2[1]
                    if dx*dx + dy*dy < (7000 if (p1[3] or p2[3]) else 2500):
                        line_color = COLOR_VECTOR_LINE if not (p1[3] and p2[3]) else COLOR_PRIMARY
                        self.canvas.create_line(p1[0], p1[1], p2[0], p2[1], fill=line_color, width=1)

        for p in projected:
            if p[3]:
                r = max(3, int(5 * p[2]))
                self.canvas.create_oval(p[0]-r-2, p[1]-r-2, p[0]+r+2, p[1]+r+2, fill="", outline=COLOR_PRIMARY, width=1)
                self.canvas.create_oval(p[0]-r, p[1]-r, p[0]+r, p[1]+r, fill="#FFFFFF", outline=COLOR_PRIMARY)
                self.canvas.create_text(p[0], p[1]-r-10, text=p[4], fill=COLOR_PRIMARY, font=("Courier New", 8, "bold"))
            else:
                r = max(1, int(2 * p[2]))
                self.canvas.create_oval(p[0]-r, p[1]-r, p[0]+r, p[1]+r, fill=COLOR_BORDER, outline="")

        self.angle_y += 0.008
        self.root.after(35, self.cycle_matrix_render)

    def handle_submit(self, event=None):
        user_input_text = self.entry_box.get().strip()
        if not user_input_text:
            return

        self.entry_box.delete(0, tk.END)
        self.chat_display.configure(state='normal')
        self.chat_display.insert(tk.END, f"\n[OPERATOR]: {user_input_text}\n", "user_style")
        self.chat_display.insert(tk.END, "[SYSTEM]: COMPILING TARGET VECTORS...\n", "bold_style")
        self.chat_display.configure(state='disabled')
        self.chat_display.see(tk.END)

        self.execute_command(user_input_text)

    def execute_command(self, cmd_text):
        if STOP_REGEX.search(cmd_text.lower()):
            self.stop_voice_playback()
            return

        # Attempt to run as a system task first (instant, no AI round-trip)
        task_response = self.run_task(cmd_text)

        if task_response:
            # If a task was found, execute it and update UI
            self.update_chat_ui(task_response)
            self.speak(task_response)
        else:
            # If no direct task match, hand off to the multi-agent reasoning loop
            def ai_worker():
                prompt = cmd_text
                # Handle Live Search if enabled
                if self.search_var.get():
                    snippet = quick_web_search(cmd_text)
                    if snippet:
                        prompt = f"Background Scraped Data: {snippet}\n\nClient command parameters: {cmd_text}"
                        self.root.after(0, lambda: self.log_raw("[RETRIEVAL]: Live scrap verified."))

                # Fetch AI Response via the agent (may call tools along the way)
                reply, model = self.run_agent(prompt)

                # Update UI and Speak
                self.root.after(0, lambda: self.update_chat_ui(reply))
                self.speak(reply)

            # Start the thread only here, inside the 'else' block
            threading.Thread(target=ai_worker, daemon=True).start()

    def update_chat_ui(self, response_text):
        self.chat_display.configure(state='normal')
        self.chat_display.insert(tk.END, f"\n[M.A.R.K]:\n", "bot_header_style")
        self.chat_display.configure(state='disabled')
        self.insert_markdown_text(self.chat_display, response_text)
        self.chat_display.configure(state='normal')
        self.chat_display.insert(tk.END, "\n" + "="*70 + "\n", "divider_style")
        self.chat_display.configure(state='disabled')
        self.chat_display.see(tk.END)

    def log_raw(self, msg):
        self.chat_display.configure(state='normal')
        self.chat_display.insert(tk.END, f"{msg}\n", "normal_style")
        self.chat_display.configure(state='disabled')
        self.chat_display.see(tk.END)

    def stop_voice_playback(self):
        if pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()
            self.root.after(0, lambda: self.log_raw("[SYSTEM]: Playback stopped."))

    def speak(self, text):
        def tts_thread():
            audio_path = os.path.join(tempfile.gettempdir(), f"mark_{uuid.uuid4().hex}.mp3")
            try:
                clean_text = re.sub(r'\*+', '', text)
                clean_text = re.sub(r'#+\s+', '', clean_text)
                clean_text = re.sub(r'[-_=\s]{3,}', '', clean_text)

                asyncio.run(self._synthesize(clean_text, audio_path, self.selected_voice))

                if pygame.mixer.music.get_busy():
                    pygame.mixer.music.stop()

                pygame.mixer.music.load(audio_path)
                pygame.mixer.music.play()

                def cleanup():
                    while pygame.mixer.music.get_busy():
                        time.sleep(0.2)
                    try:
                        os.remove(audio_path)
                    except OSError:
                        pass
                threading.Thread(target=cleanup, daemon=True).start()
            except Exception as e:
                print(f"Voice pipeline alert: {e}")
        threading.Thread(target=tts_thread, daemon=True).start()

    async def _synthesize(self, text, path, voice=None):
        communicate = edge_tts.Communicate(text, voice or self.selected_voice)
        await communicate.save(path)

    def start_voice_thread(self):
        threading.Thread(target=self._init_and_listen, daemon=True).start()

    def _init_and_listen(self):
        try:
            self.recognizer = sr.Recognizer()

            # These settings make it feel more "human" and adaptive
            self.recognizer.dynamic_energy_threshold = True
            self.recognizer.energy_threshold = 400
            self.recognizer.pause_threshold = 0.8

            # Using your original device index to ensure stability
            mic = sr.Microphone(device_index=MIC_DEVICE_INDEX)

            # Background thread - now much more responsive
            self.stop_listening = self.recognizer.listen_in_background(
                mic, self._on_audio
            )
            self.root.after(0, lambda: self.log_raw("[SYSTEM]: Voice channel sync established."))
        except Exception as e:
            self.root.after(0, lambda: self.log_raw(f"[SYSTEM]: Voice error: {e}"))

    def recognize_speech(self, audio):
        """English-only recognition via Google's speech API."""
        try:
            return self.recognizer.recognize_google(audio, language="en-IN").lower()
        except Exception:
            return None

    def _on_audio(self, recognizer, audio):
        phrase = self.recognize_speech(audio)
        if not phrase:
            return
        self.root.after(0, lambda p=phrase: self._handle_heard_phrase(p))

    def _handle_heard_phrase(self, phrase):
        p_lower = phrase.lower()

        # 1. Activation Toggles
        if "mark activate" in p_lower:
            self.is_active = True
            self.speak("Voice channel active. I am listening.")
            self.log_raw("[SYSTEM]: Persistent listening ENABLED.")
            return

        if "mark deactivate" in p_lower:
            self.is_active = False
            self.speak("Deactivating voice channel.")
            self.log_raw("[SYSTEM]: Persistent listening DISABLED.")
            return

        # 2. Logic: If active OR if wake word used, process command
        if self.is_active or WAKE_REGEX.search(p_lower):
            # Clean the command (remove the wake word if it was present)
            cmd = WAKE_REGEX.sub("", phrase).strip()

            # If the user said "mark activate" or just "mark", avoid empty commands
            if cmd and not ("activate" in cmd or "deactivate" in cmd):
                self.execute_command(cmd)
            elif not cmd:
                self.speak("I'm listening.")
            return

        # 3. Stop command always works
        if STOP_REGEX.search(p_lower):
            self.stop_voice_playback()


if __name__ == "__main__":
    root = tk.Tk()
    app = Mark3DMainframe(root)
    root.mainloop()
