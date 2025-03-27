from flask import Flask, request, jsonify
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import requests
from bs4 import BeautifulSoup
from openai import OpenAI
import os
import re
import json
from collections import OrderedDict
import pickle

app = Flask(__name__)

# Initialize Slack client
slack_client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))

# Initialize OpenAI client
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Use an OrderedDict with limited size to track recent messages
# This will automatically remove oldest entries when it reaches max size
class LimitedSizeDict(OrderedDict):
    def __init__(self, *args, **kwds):
        self.size_limit = kwds.pop("size_limit", None)
        OrderedDict.__init__(self, *args, **kwds)
        self._check_size_limit()

    def __setitem__(self, key, value):
        OrderedDict.__setitem__(self, key, value)
        self._check_size_limit()

    def _check_size_limit(self):
        if self.size_limit is not None:
            while len(self) > self.size_limit:
                self.popitem(last=False)

# Load processed messages from a file if it exists
def load_processed_messages():
    try:
        with open('processed_messages.pkl', 'rb') as f:
            return pickle.load(f)
    except FileNotFoundError:
        return LimitedSizeDict(size_limit=1000)

# Save processed messages to a file
def save_processed_messages():
    with open('processed_messages.pkl', 'wb') as f:
        pickle.dump(processed_messages, f)

# Track the 1000 most recent processed message IDs
processed_messages = load_processed_messages()

@app.route('/slack/events', methods=['POST'])
def slack_events():
    data = request.json

    if 'challenge' in data:
        return jsonify({'challenge': data['challenge']})

    if 'event' in data:
        event = data['event']
        # print(event)  # Debug: Print the full event
        
        # Skip message_changed events and bot messages
        if event.get('subtype') == 'message_changed' or event.get('bot_id'):
            return jsonify({'status': 'skipped'})
        
        if event.get('type') == 'message':
            message_id = event.get('client_msg_id', event.get('ts'))
            if message_id in processed_messages:
                return jsonify({'status': 'already processed'})
            
            # Add to processed messages and save state
            processed_messages[message_id] = True
            save_processed_messages()
            
            # Check for URLs in text or in blocks (for formatted messages)
            text = event.get('text', '')
            has_url = 'http' in text
            
            # Check for URLs in blocks (Slack's rich text format)
            if not has_url and 'blocks' in event:
                for block in event.get('blocks', []):
                    if block.get('type') == 'rich_text':
                        for element in block.get('elements', []):
                            if element.get('type') == 'rich_text_section':
                                for item in element.get('elements', []):
                                    if item.get('type') == 'link' and 'url' in item:
                                        has_url = True
                                        break
            
            if has_url:
                handle_message(event)

    return jsonify({'status': 'ok'})

def handle_message(event):
    text = event.get('text', '')
    user = event.get('user', '')
    channel = event.get('channel', '')
    thread_ts = event.get('thread_ts', None)  # Get thread timestamp if message is in a thread
    message_ts = event.get('ts', '')  # Get the timestamp of the current message
    # print("----------text----------------------")
    # print(text)
    # print("--------------------------------")
    
    # Check for URLs in Slack's angle bracket format
    url_match = re.search(r'<(https?://[^>]+)>', text)
    if url_match:
        text = text.replace(url_match.group(0), url_match.group(1))

    # Extract URL from the message
    url = extract_url(text)
    if url:
        # Fetch and summarize the content
        summary = fetch_and_summarize(url)
        if summary:
            # If the message is already in a thread, use that thread_ts
            # If not, use the message's own timestamp to create a new thread
            reply_thread_ts = thread_ts if thread_ts else message_ts
            post_summary_to_slack(channel, user, summary, reply_thread_ts)

def extract_url(text):
    # Simple URL extraction logic
    url_match = re.search(r'(https?://\S+)', text)
    return url_match.group(0) if url_match else None

def fetch_and_summarize(url):
    output = f"you have entered the url: {url}"
    # print("-------------------------------- ")
    # print(output)
    # print("--------------------------------")
    # return output
    try:
        response = requests.get(url)
        
        if response.status_code != 200:
            return f"Unable to access the webpage. Status code: {response.status_code}"
            
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Try to get the main content, focusing on article elements
        article = soup.find('article')
        if article:
            content = article.get_text()
        else:
            # Fall back to main content or body
            main = soup.find('main') or soup.find('body')
            content = main.get_text() if main else soup.get_text()
        
        # Limit content length to avoid API limits
        content = content[:15000]

        # Call OpenAI API to summarize
        response = openai_client.responses.create(
            model="gpt-4o-mini",
            instructions="I am a junior PhD student in molecular biology, synthetic biology, bioengineering, and bioinfomatics. I have a basic understanding of all of these fields, but I am not an expert. I want you to summarize the following academic paper webpage. Give me the main findings and the key points, and also why this paper is important in the field. Include necessary background information. Limit the response to 200 words. I am also posting this message to slack, so make sure to format it correctly. It only accepts single asterisks for bold and single underscores for italics. Do not use other formatting options. You need to follow these formmating instructions.",
            input=content,
        )
        return response.output_text.strip()
    except Exception as e:
        print(f"Error fetching or summarizing content: {e}")
        return f"I encountered an error when trying to access or process the content: {str(e)}"

def post_summary_to_slack(channel, user, summary, thread_ts=None):
    try:
        slack_client.chat_postMessage(
            channel=channel,
            text=f"<@{user}> Here's the summary of the linked paper:\n{summary}",
            thread_ts=thread_ts  # This will post in the thread if thread_ts is provided
        )
    except SlackApiError as e:
        print(f"Error posting message to Slack: {e.response['error']}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)