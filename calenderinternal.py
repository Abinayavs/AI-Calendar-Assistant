import os
import re
import time
import pickle
import base64
import dateparser
import unicodedata
from datetime import datetime, timedelta
from dotenv import load_dotenv
from email.mime.text import MIMEText
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import google.generativeai as genai

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events"
]
CREDENTIALS_PATH = os.environ.get("CREDENTIALS_FILE_PATH")
TOKEN_PATH = "token.pickle"

REQUIRED_FIELDS = ["participant_email", "event_name", "event_date", "event_time"]
model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    generation_config={
        "temperature": 1,
        "top_p": 0.95,
        "top_k": 40,
        "max_output_tokens": 8192
    }
)

chat = model.start_chat(history=[])

# Authenticate Google APIs
def authenticate_services():
    creds = None
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "rb") as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "wb") as token:
            pickle.dump(creds, token)
    return {
        "gmail": build("gmail", "v1", credentials=creds),
        "calendar": build("calendar", "v3", credentials=creds)
    }

def send_invitation(gmail_service, recipient_email,meet_date,meet_time):
    subject = "Meeting Invitation - Accept to Proceed"
    body = f"Hi, please reply with 'Yes' if you accept the meeting invite on '{meet_date}' at '{meet_time}'."
    message = MIMEText(body)
    message['to'] = recipient_email
    message['from'] = "me"
    message['subject'] = subject
    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    message_body = {'raw': raw_message}
    gmail_service.users().messages().send(userId="me", body=message_body).execute()
    print(f"\n📨 Invitation email sent to {recipient_email}.")
    #return True
    return time.time(),"yes"

def wait_for_acceptance(gmail_service, expected_email, since_timestamp):
    print("⏳ Waiting for response...")
    for _ in range(300):
        response = gmail_service.users().messages().list(
            userId="me",
            q=f"from:{expected_email} newer_than:1d",
            maxResults=5
        ).execute()
        messages = response.get('messages', [])
        for msg in messages:
            full_msg = gmail_service.users().messages().get(userId="me", id=msg['id'], format='full').execute()
            internal_date = int(full_msg.get("internalDate", 0)) / 1000
            if internal_date > since_timestamp:
                snippet = full_msg.get("snippet", "").lower()
                reply_only = re.split(r"\s*on\s.+?wrote:", snippet)[0].strip()
               
                if any(word in reply_only for word in ["yes", "accepted", "i accept"]):
                    print("✅ The Attendee has accepted the event")
                    return True
                else:
                    print("❌ The attendee has rejected the event.")
                    return False
        time.sleep(6)
    print("❌ No response received in time.")
    return False

def create_event(calendar_service, summary, start_time, end_time, participant_email):
    event = {
        "summary": summary,
        "start": {"dateTime": start_time, "timeZone": "Asia/Kolkata"},
        "end": {"dateTime": end_time, "timeZone": "Asia/Kolkata"},
        "attendees": [{"email": participant_email}],
        "conferenceData": {
            "createRequest": {
                "requestId": "meet-" + str(datetime.now().timestamp()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"}
            }
        }
    }
    created_event = calendar_service.events().insert(
        calendarId="primary",
        body=event,
        sendUpdates="all",
        conferenceDataVersion=1
    ).execute()
    print(f"\n✅ Event created: {created_event.get('htmlLink')}")
    print(f"🗓️ Meet Link: {created_event.get('conferenceData', {}).get('entryPoints', [{}])[0].get('uri', 'N/A')}")



def normalize(text):
    return unicodedata.normalize('NFKD', text).strip().lower()

def get_event_by_name(calendar_service, event_name):
    now = datetime.utcnow().isoformat() + 'Z'
    events = calendar_service.events().list(
        calendarId='primary',
        timeMin=now,
        maxResults=10,
        singleEvents=True,
        orderBy='startTime'
    ).execute().get('items', [])

    for event in events:
        print(event)
        if event.get('summary', '').lower() == event_name.lower():
            return event
    return None


def update_event(calendar_service, gmail_service, text):
    details = extract_update_details(text)

    # Ask for missing info
    if "event_name" not in details:
        details['event_name'] = input("🤖 Gemini: What is the event name to update?\nYou: ").strip()
    if "new_date" not in details:
        new_date_input = input("📅 New date (e.g. April 21): ")
        parsed = dateparser.parse(new_date_input, settings={'PREFER_DATES_FROM': 'future'})
        if parsed:
            details['new_date'] = parsed.strftime('%Y-%m-%d')
        else:
            print("❗ Couldn't parse the date. Try again.")
            return
    if "new_time" not in details:
        details['new_time'] = input("🕐 New time (e.g. 10am to 11am): ")

    # Parse datetime range
    new_start, new_end = parse_datetime(details['new_date'], details['new_time'])

    print(f"🪪 Extracted Event Name: {details['event_name']}")

    # Search for the event
    event = get_event_by_name(calendar_service, details['event_name'])
    print(event)
    if not event:
        print("❗ Event to update not found.")
        return

    email = event['attendees'][0]['email']

    send_email(
        gmail_service,
        email,
        f"Reschedule Request: {details['event_name']}",
        f"Hi, would you be okay with rescheduling the meeting '{details['event_name']}' to:\n{new_start} to {new_end}?\n\nPlease reply 'Yes' to confirm."
    )

    sent_time = time.time()
    if wait_for_acceptance(gmail_service, email, sent_time):
        event['start']['dateTime'] = new_start
        event['end']['dateTime'] = new_end
        updated_event = calendar_service.events().update(
            calendarId='primary',
            eventId=event['id'],
            body=event,
            sendUpdates='all'
        ).execute()
        print(f"✅ Event rescheduled: {updated_event.get('htmlLink')}")
    else:
        print("❌ Reschedule rejected or no response.")


def send_email(gmail_service, recipient, subject, body):
    message = MIMEText(body)
    message['to'] = recipient
    message['from'] = "me"
    message['subject'] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()

def delete_event(calendar_service, gmail_service, event_name):
    now = datetime.utcnow().isoformat() + 'Z'
    events = calendar_service.events().list(
        calendarId='primary',
        timeMin=now,
        maxResults=10,
        singleEvents=True,
        orderBy='startTime'
    ).execute().get('items', [])

    for event in events:
        if event.get('summary', '').lower() == event_name.lower():
            attendees = event.get('attendees', [])
            calendar_service.events().delete(calendarId='primary', eventId=event['id']).execute()
            print(f" ⛔ Deleted: {event_name}")
            for attendee in attendees:
                send_email(gmail_service, attendee['email'], f"Event Cancelled: {event_name}",
                           f"The scheduled event '{event_name}' has been cancelled.")
            return True
    print("❗ Event not found")
    return False


def extract_update_details(text):
    details = {}
    text_lower = text.lower()

    prompt = f""" you are an expert in identifying the event date from the given user input , it can be even with spelling error but 
    you are an expert model so you must autocorrect the spelling errors and extract the date correctly and return it in the format which
    will be accepted by the google calendar api 
    Message: "{text_lower}" """
    eve_date = chat.send_message(prompt)
    event_date_text = eve_date.candidates[0].content.parts[0].text.strip()

    prompt = f""" you are an expert in identifying whether there is any date in the message or not, if you found 
    any information that it says there is no date specified in this message, or any message that is not related to date format then 
    return "no" else return "yes"
    Message: "{event_date_text}" """
    is_event_date = chat.send_message(prompt)
    is_event_date = is_event_date.candidates[0].content.parts[0].text.strip()
   
    if is_event_date == "yes":
        details['new_date'] = event_date_text

    print(event_date_text)


    
    # 1. Extract new date first (in case event name appears before/after)
    '''month_pattern = r"(jan|feb|mar|apr|aprl|apl|may|jun|jul|aug|sep|sept|oct|nov|dec|" \
                    r"january|february|march|april|may|june|july|august|" \
                    r"september|october|november|december)"
    date_match = re.search(rf"\b{month_pattern}\s+\d{{1,2}}\b", text_lower)
    if date_match:
        parsed_date = dateparser.parse(date_match.group(0), settings={'PREFER_DATES_FROM': 'future'})
        if parsed_date:
            details['new_date'] = parsed_date.strftime('%Y-%m-%d')'''
    
    
    prompt = f""" you are an expert in identifying the event timing from the given user input, it can be even with spelling error but 
    you are an expert model so you must auto correct the spelling errors and extract the timing correctly and make sure there is the start
    and end timing and return it in this format example "3pm to 4pm"
    Message: "{text_lower}" """
    eve_time = chat.send_message(prompt)
    event_time_text = eve_time.candidates[0].content.parts[0].text.strip()

    prompt = f""" you are an expert in identifying whether there is any timing format in the message or not, if you found 
    any information that it says there is no timing specified in this message, or any message that is not related to timing format then 
    return "no" else return "yes"
    Message: "{event_time_text}" """
    is_event_time = chat.send_message(prompt)
    is_event_time = is_event_time.candidates[0].content.parts[0].text.strip()
    print(is_event_time)
   
    if is_event_time == "yes":
        details['new_time'] = event_time_text

    print(event_time_text)
       

    # 2. Extract new time
    '''time_match = re.search(r"(\d{1,2}\s*(am|pm))\s*(to|till|until)\s*(\d{1,2}\s*(am|pm))", text_lower)
    print(time_match)
    if time_match:
        details['new_time'] = time_match.group(0)
        print(details['new_time'])'''

    # 3. Extract event name using smart patterns
    '''name_patterns = [
        r"(?:update|reschedule|move|change|shift)\s+(?:the\s+)?(?:event|meeting)?\s*(?:named|called)?\s*['\"]?([a-zA-Z0-9 _-]+?)['\"]?(?:\s+(?:to|on|for))?",
        r"(?:the\s+)?(?:event|meeting)?\s*['\"]?([a-zA-Z0-9 _-]+?)['\"]?\s+(?:needs to be|should be|must be|to be)?\s*(?:updated|moved|rescheduled)",
        r"(?:reschedule|move|shift|update)\s+([a-zA-Z0-9 _-]+)\s+(?:to|on|for)",  # "reschedule heyy to april 20"
        r"(?:update|reschedule|move|change|shift)\s+(?:the\s+)?(?:event|meeting)?\s*(?:named|called)?\s*['\"]?([a-zA-Z0-9 _-]+?)",
    ]'''
    
    prompt = f""" you are an expert in identifying the event name from the user input.Now your task is to extract the
    event name from the user input correctly
    Message: "{text_lower}" """
    eve_name = chat.send_message(prompt)
    event_name_text = eve_name.candidates[0].content.parts[0].text.strip()

    prompt = f""" you are an expert in identifying whether there is any event name in the message or not, if you found 
    any information that it says there is no event name specified in this, it's being generic, then return "no" else return "yes"
    Message: "{event_name_text}" """
    is_event_name = chat.send_message(prompt)
    is_event_name = is_event_name.candidates[0].content.parts[0].text.strip()
   
    if is_event_name == "yes":
        details['event_name'] = event_name_text

    '''for pattern in name_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip()
            # Avoid falsely capturing the date as a name
            if candidate.lower() not in text_lower[text_lower.find(candidate.lower()):text_lower.find(candidate.lower())+15]:
                details['event_name'] = candidate
                break'''

    print(details)
    return details

def extract_delete_details(text):
    details= {}
    text_lower = text.lower()

    prompt = f""" you are an expert in identifying the event name from the user input.Now your task is to extract the
    event name from the user input correctly
    Message: "{text_lower}" """
    eve_name = chat.send_message(prompt)
    event_name_text = eve_name.candidates[0].content.parts[0].text.strip()
    
    prompt = f""" you are an expert in identifying whether there is any event name in the message or not, if you found 
    any information that it says there is no event name specified in this, it's being generic, then return "no" else return "yes"
    Message: "{event_name_text}" """
    is_event_name = chat.send_message(prompt)
    is_event_name = is_event_name.candidates[0].content.parts[0].text.strip()
   
    if is_event_name == "yes":
        details['event_name'] = event_name_text
    
   
    print(details)
    return details






def extract_event_details(text):
    details = {}

    # Extract participant email
    email_match = re.search(r"[\w\.-]+@[\w\.-]+", text)
    if email_match:
        details['participant_email'] = email_match.group(0)
    
    text_lower = text.lower()

    # Extract event name only if "called" is mentioned
    '''name_match = re.search(r"called\s+([^\.,\n]+)", text, re.IGNORECASE)
    if name_match:
        details['event_name'] = name_match.group(1).strip()'''
    
    prompt = f""" you are an expert in identifying the event name from the user input.Now your task is to extract the
    event name from the user input correctly
    Message: "{text_lower}" """
    eve_name = chat.send_message(prompt)
    event_name_text = eve_name.candidates[0].content.parts[0].text.strip()

    prompt = f""" you are an expert in identifying whether there is any event name in the message or not, if you found 
    any information that it says there is no event name specified in this, it's being generic, then return "no" else return "yes"
    Message: "{event_name_text}" """
    is_event_name = chat.send_message(prompt)
    is_event_name = is_event_name.candidates[0].content.parts[0].text.strip()
   
    if is_event_name == "yes":
        details['event_name'] = event_name_text
    print(is_event_name)
    print(event_name_text)


    # Extract event date - look for "tomorrow" or flexible formats
    if "tomorrow" in text_lower:
        tomorrow = datetime.now() + timedelta(days=1)
        details['event_date'] = tomorrow.strftime('%Y-%m-%d')
    else:
        month_patterns = r"(jan|feb|mar|apr|aprl|apl|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|may|june|july|august|september|october|november|december)"
        date_match = re.search(rf"\b{month_patterns}\s+\d{{1,2}}\b", text_lower)
        if date_match:
            date_text = date_match.group(0)
            parsed_date = dateparser.parse(date_text, settings={'PREFER_DATES_FROM': 'future'})
            if parsed_date:
                details['event_date'] = parsed_date.strftime('%Y-%m-%d')

    # Extract event time
    time_match = re.search(r"(\d{1,2}\s*(am|pm))\s*(to|too|till|til|until)\s*(\d{1,2}\s*(am|pm))", text_lower)
    if time_match:
        details['event_time'] = time_match.group(0)

    print(f"[Debug] Extracted details: {details}")  # Add this for debugging
    return details

def parse_datetime(date_str, time_range):
    start_time, end_time = time_range.lower().split(" to ")
    start = dateparser.parse(f"{date_str} {start_time}")
    end = dateparser.parse(f"{date_str} {end_time}")
    return start.isoformat(), end.isoformat()



def prompt_missing_fields(current_data):
    missing = [field for field in REQUIRED_FIELDS if field not in current_data]
    questions = {
        "participant_email": "What is the participant's email?",
        "event_name": "What should be the event name?",
        "event_date": "When is the meeting? (e.g. tomorrow or April 8)",
        "event_time": "What time is the meeting? (e.g. 10am to 11am)"
    }
    for field in missing:
        while True:
            reply = input(f"🤖 Gemini: {questions[field]}\nYou: ")
            if field == "event_name":
                if reply.strip():
                    current_data["event_name"] = reply.strip()
                    break
                else:
                    print("❗ Event name cannot be empty.")
            else:
                extracted = extract_event_details(reply)
                if field in extracted:
                    current_data[field] = extracted[field]
                    break
                else:
                    print("❗ Invalid or missing information, please try again.")
    return current_data

def is_schedule_intent(message):
        prompt = f""" Is this message related to scheduling an event or meet (make sure it is not about deletion or cancellation or updation or rescheduling the event or meet) only then Reply with "yes" else "no".
    Message: "{message}" """
        intent = chat.send_message(prompt)
        return "yes" in intent.text.lower()

def is_update_intent(message):
    prompt = f"""Is this message about updating or rescheduling an existing event? Only reply "yes" or "no".

Message: "{message}"
"""
    intent = chat.send_message(prompt)
    return "yes" in intent.text.lower()


def is_delete_intent(message):
    prompt = f"""Is this message about deleting or canceling a calendar event? Reply only with "yes" or "no".
Message: "{message}" """
    intent = chat.send_message(prompt)
    return "yes" in intent.text.lower()


def correct_schedule_spelling(message):

    prompt = f"""You are an expert system, so if there is any spelling errors and grammar errors in the user input just correct
    only it avoid space mistakes and aligning mistakes all and make sure always timing must be in this format example "10am to 11am" 
    if you find timing in some other format correct them to this format alone and return the error free sentence, incase there is no 
    error in the user input just return the same sentence.
    Message: "{message}" """
    crt = chat.send_message(prompt)
    corrected_message = crt.candidates[0].content.parts[0].text.strip()
    print(corrected_message)

    '''corrections = {
        # Months
        r"\b(aprl|apr|apl|aprill|aplr)\b": "April",
        r"\b(my|maay|mee)\b": "May",
        r"\b(jn|junee|juin)\b": "June",
        r"\b(jly|jul|jull|juuly)\b": "July",
        r"\b(augst|agu|agust)\b": "August",
        r"\b(sep|septmbr|sept|setember)\b": "September",
        r"\b(octr|octbr|octb|ocober)\b": "October",
        r"\b(novmbr|novbr|novembr)\b": "November",
        r"\b(decmbr|decbr|decembr)\b": "December",
        r"\b(jan|janury|januar)\b": "January",
        r"\b(feb|febuary|feburary)\b": "February",
        r"\b(mar|mrch|marchh)\b": "March",

    
        # Intent-related
        r"\b(schdule|scehdule|schedul|scdule|scdhule)\b": "schedule",
        r"\b(meetng|meting|metin|metting)\b": "meeting",
        r"\b(emial|maill|emaill|maiil)\b": "email",

        # Date words
        r"\b(tmrw|tmorrow|tmorow|tmrow)\b": "tomorrow",
        r"\btodayy\b": "today",
        r"\bystrdy|yesterdayy\b": "yesterday",
    }

    corrected_message = message
    for pattern, replacement in corrections.items():
        corrected_message = re.sub(pattern, replacement, corrected_message, flags=re.IGNORECASE)'''

    return corrected_message

def prompt_for_deletion_details(text):
    text = text.strip().lower()
    event_name = ""

    # Try multiple patterns to extract event name
    '''patterns = [
        r"(?:named|called)\s+\"?([^\"]+?)\"?(?=\s|$)",                       
        r"(?:event|meeting|meet)?\s*\"([^\"]+)\"",                           
        r"(?:cancel|delete|remove)\s+(?:the\s+)?(?:event|meeting|meet)?\s*called\s+([^\.,\n]+)",  
        r"(?:cancel|delete|remove)\s+(?:the\s+)?(?:event|meeting|meet)?\s*named\s+([^\.,\n]+)",   
        r"(?:cancel|delete|remove)\s+(?:the\s+)?(?:event|meeting|meet)?\s+([^\.,\n]+)",           
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        print(match)
        if match:
            event_name = match.group(1).strip()
            print(event_name)
            break'''
    
    details = extract_delete_details(text)

    if details:
        event_name = details['event_name']


    # If not found, ask for event name
    while not event_name:
        reply = input("🤖 Gemini: What is the name of the event you want to delete?\nYou: ")
        if reply.strip():
            event_name = reply.strip()
        else:
            print("❗ Event name cannot be empty.")

    return event_name



if __name__ == "__main__":
    print("🧠 Gemini Assistant: Ready to schedule your meetings. Type 'exit' anytime to quit.")
    services = authenticate_services()
    while True:
        user_input = input("You: ")
        user_input = correct_schedule_spelling(user_input)
        print(user_input)
        if user_input.lower() in ["exit", "quit"]:
            print("🤖 Gemini: Goodbye! 👋")
            break

        if is_schedule_intent(user_input):
            details = extract_event_details(user_input)
            details = prompt_missing_fields(details)
            start_time, end_time = parse_datetime(details['event_date'], details['event_time'])
            sent_time,mail_check = send_invitation(services['gmail'], details['participant_email'],details["event_date"],details["event_time"])
            if wait_for_acceptance(services['gmail'], details['participant_email'], sent_time):
                create_event(
                    services['calendar'],
                    summary=details['event_name'],
                    start_time=start_time,
                    end_time=end_time,
                    participant_email=details['participant_email']
                )
            else:
                print("🤖 Gemini: Event not scheduled as no confirmation was received.")

        elif is_update_intent(user_input):
            update_event(services['calendar'], services['gmail'], user_input)

        elif is_delete_intent(user_input):
            event_name = prompt_for_deletion_details(user_input)
            deleted = delete_event(services['calendar'], services['gmail'], event_name)
            if not deleted:
                print("🤖 Gemini: I couldn't find that event in your calendar.")
        else:
            response = chat.send_message(f"""Reply to the users Message: "{user_input}" """)
            print(response)
            print("🤖 Gemini:", response.text)
