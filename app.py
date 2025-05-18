import time
import dateparser
import os
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, session
from flask_session import Session
from calenderinternal import (
    authenticate_services, correct_schedule_spelling, extract_delete_details, get_event_by_name, is_schedule_intent,
    is_update_intent, is_delete_intent, extract_event_details,
    parse_datetime, send_invitation, wait_for_acceptance, create_event, send_email,
    delete_event,extract_update_details, chat
)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
app.config["SESSION_TYPE"] = "filesystem"
Session(app)

services = authenticate_services()
REQUIRED_FIELDS = ["participant_email", "event_name", "event_date", "event_time"]

@app.route('/')
def index():
    session.clear()
    return render_template('index.html')

def get_missing_field_prompt(current_data):
    missing = [field for field in REQUIRED_FIELDS if field not in current_data]
    questions = {
        "participant_email": "What is the participant's email?",
        "event_name": "What should be the event name?",
        "event_date": "When is the meeting? (e.g. tomorrow or April 8)",
        "event_time": "What time is the meeting? (e.g. 10am to 11am)"
    }
    if missing:
        field = missing[0]
        return field, f"{questions[field]}"
    return None, None

@app.route('/chat', methods=['POST'])
def chat_route():
    user_input = request.json.get("message", "").strip()
    user_input = correct_schedule_spelling(user_input)

    if 'data' not in session:
        session['data'] = {}
    data = session['data']

    if 'messages' not in session:
        session['messages'] = []
        
    # Handle missing field reply
    if 'waiting_for' in session:
        field = session['waiting_for']
        intent = session.get('intent')

        if intent == 'schedule':
            extracted = extract_event_details(user_input)
            if field == "event_name" and user_input.strip():
                data[field] = user_input.strip()
                session['data'] = data
                session.pop('waiting_for')
            elif field in extracted:
                data[field] = extracted[field]
                session['data'] = data
                session.pop('waiting_for')
            else:
                return jsonify({"reply": f"❗ Invalid or missing {field}, please try again."})

        elif intent == 'update':
            if field == "event_name" and user_input.strip():
                session['data'][field] = user_input.strip()
                session.pop('waiting_for')
            elif field == "new_date":
                parsed = dateparser.parse(user_input, settings={'PREFER_DATES_FROM': 'future'})
                if parsed:
                    session['data'][field] = parsed.strftime('%Y-%m-%d')
                    session.pop('waiting_for')
                else:
                    return jsonify({"reply": "❗ Couldn't parse the date. Try again."})
            elif field == "new_time":
                session['data'][field] = user_input
                session.pop('waiting_for')
            else:
                return jsonify({"reply": f"❗ Invalid or missing {field}, please try again."})

        elif intent == 'delete':
            if field == "event_name":
                if user_input.strip():
                    session['data']['event_name'] = user_input.strip()
                    session.pop('waiting_for')
                else:
                    return jsonify({"reply": "❗ Event name cannot be empty. Please enter it."})

    
    # Initial intent detection
    if 'intent' not in session:
        if is_schedule_intent(user_input):
            session['intent'] = 'schedule'
            extracted = extract_event_details(user_input)
            session['data'] = extracted
        elif is_update_intent(user_input):
            session['intent'] = 'update'
            session['update_text'] = user_input
            session['data'] = extract_update_details(user_input)
        elif is_delete_intent(user_input):
            session['intent'] = 'delete'
            session['delete_text'] = user_input
            session['data'] = extract_delete_details(user_input)
        else:
            reply = chat.send_message(f"""Reply to the users Message: "{user_input}" """).text
            print(reply)
            return jsonify({"reply": reply})

    intent = session['intent']

    if intent == 'schedule':
        field, prompt = get_missing_field_prompt(session['data'])
        mail_check = ""
        if field:
            session['waiting_for'] = field
            return jsonify({"reply": prompt})

        details = session['data']
        start_time, end_time = parse_datetime(details['event_date'], details['event_time'])

        msg1 = f"📨 Invitation email sent to {details['participant_email']}."

        sent_time, mail_check = send_invitation(services['gmail'], details['participant_email'], details['event_date'], details['event_time'])
        

        if wait_for_acceptance(services['gmail'], details['participant_email'], sent_time):
            create_event(
                services['calendar'],
                summary=details['event_name'],
                start_time=start_time,
                end_time=end_time,
                participant_email=details['participant_email']
            )
            msg3 = f"✅ Event '{details['event_name']}' scheduled successfully."
        else:
            msg3 = "❌ The attendee has rejected the event."

        session.clear()
        return jsonify({"reply": msg3})

    elif intent == 'update':
        details = session['data']
        print(details)

        # Check for missing fields and prompt
        for field, prompt in [
            ("event_name", "🤖 What is the event name to update?"),
            ("new_date", "📅 New date (e.g. April 21): "),
            ("new_time", "🕐 New time (e.g. 10am to 11am): ")
        ]:
            if field not in details:
                session['waiting_for'] = field
                return jsonify({"reply": prompt})

        new_start, new_end = parse_datetime(details['new_date'], details['new_time'])

        print(new_start,new_end)

        event = get_event_by_name(services['calendar'], details['event_name'])
        if not event:
            session.clear()
            return jsonify({"reply": "❗ Event to update not found."})

        email = event['attendees'][0]['email']
        send_email(
            services['gmail'],
            email,
            f"Reschedule Request: {details['event_name']}",
            f"Hi, would you be okay with rescheduling the meeting '{details['event_name']}' to:\n{new_start} to {new_end}?\n\nPlease reply 'Yes' to confirm."
        )

        sent_time = time.time()
        if wait_for_acceptance(services['gmail'], email, sent_time):
            event['start']['dateTime'] = new_start
            event['end']['dateTime'] = new_end
            updated_event = services['calendar'].events().update(
                calendarId='primary',
                eventId=event['id'],
                body=event,
                sendUpdates='all'
            ).execute()
            session.clear()
            return jsonify({"reply": f"✅ Event called '{details['event_name']}' rescheduled successfully to '{details['new_date']}'"})
        else:
            session.clear()
            return jsonify({"reply": "❌ Reschedule rejected or no response."})

    elif intent == 'delete':
        details = session['data']
        print(details)
        if 'event_name' not in details:
            session['waiting_for'] = 'event_name'
            return jsonify({"reply": "🗑️ What is the name of the event you want to delete?"})

        # Try deleting the event
        deleted = delete_event(services['calendar'], services['gmail'], details['event_name'])
        session.clear()
        if deleted:
            return jsonify({"reply": f"⛔ Event '{details['event_name']}' deleted."})
        else:
            return jsonify({"reply": "❗ Event not found."})


    return jsonify({"reply": "Feature not recognized."})

def check_if_event_accepted(gmail_service, participant_email):
    query = f"from:{participant_email} subject:Accepted"
    result = gmail_service.users().messages().list(userId='me', q=query).execute()

    # Ensure 'messages' key exists before accessing it
    if 'messages' in result and result['messages']:
        return True
    return False

if __name__ == '__main__':
    app.run(debug=True)
