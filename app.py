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
import logging

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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
    try:
        data = request.json
        logger.info(f"Received event: {json.dumps(data)[:200]}...")  # Log truncated event data

        if 'challenge' in data:
            return jsonify({'challenge': data['challenge']})

        if 'event' in data:
            event = data['event']
            logger.info(f"Processing event type: {event.get('type')}")
            
            # Skip message_changed events and bot messages
            if event.get('subtype') == 'message_changed' or event.get('bot_id'):
                logger.info(f"Skipping message: subtype={event.get('subtype')}, bot_id={event.get('bot_id')}")
                return jsonify({'status': 'skipped'})
            
            if event.get('type') == 'message':
                # Ensure we have a valid message ID
                message_id = event.get('client_msg_id')
                if not message_id:
                    message_id = event.get('ts')
                    logger.info(f"Using ts as message_id: {message_id}")
                
                if message_id in processed_messages:
                    logger.info(f"Message already processed: {message_id}")
                    return jsonify({'status': 'already processed'})
                
                # Add to processed messages and save state
                processed_messages[message_id] = True
                save_processed_messages()
                
                # Check for URLs in text or in blocks (for formatted messages)
                text = event.get('text', '')
                has_url = False
                
                # First check simple text
                if 'http' in text:
                    has_url = True
                    logger.info(f"Found URL in text: {text[:50]}...")
                
                # Then check for URLs in blocks (Slack's rich text format)
                if not has_url and 'blocks' in event:
                    logger.info("Checking blocks for URLs")
                    blocks = event.get('blocks', [])
                    
                    # Handle different block structures that might come from mobile
                    for block in blocks:
                        block_type = block.get('type')
                        
                        # Handle rich_text blocks
                        if block_type == 'rich_text':
                            for element in block.get('elements', []):
                                if element.get('type') == 'rich_text_section':
                                    for item in element.get('elements', []):
                                        if item.get('type') == 'link' and 'url' in item:
                                            has_url = True
                                            logger.info(f"Found URL in rich_text block: {item.get('url')}")
                                            break
                        
                        # Handle section blocks with text
                        elif block_type == 'section':
                            text_obj = block.get('text', {})
                            if isinstance(text_obj, dict) and 'http' in text_obj.get('text', ''):
                                has_url = True
                                logger.info(f"Found URL in section block: {text_obj.get('text')[:50]}...")
                
                if has_url:
                    logger.info("URL found, handling message")
                    handle_message(event)
                else:
                    logger.info("No URL found in message")

        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"Error in slack_events: {str(e)}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 200  # Return 200 to acknowledge receipt

def handle_message(event):
    try:
        text = event.get('text', '')
        user = event.get('user', '')
        channel = event.get('channel', '')
        thread_ts = event.get('thread_ts', None)  # Get thread timestamp if message is in a thread
        message_ts = event.get('ts', '')  # Get the timestamp of the current message
        logger.info(f"Handling message from user {user} in channel {channel}")
        
        # Check for URLs in Slack's angle bracket format
        url_match = re.search(r'<(https?://[^>|]+)(?:\|[^>]+)?>', text)
        if url_match:
            extracted_url = url_match.group(1)
            logger.info(f"Extracted URL from angle brackets: {extracted_url}")
            text = text.replace(url_match.group(0), extracted_url)

        # Extract URL from the message
        url = extract_url(text)
        
        # If no URL found in text, try to find it in blocks
        if not url and 'blocks' in event:
            logger.info("No URL in text, checking blocks")
            for block in event.get('blocks', []):
                if block.get('type') == 'rich_text':
                    for element in block.get('elements', []):
                        if element.get('type') == 'rich_text_section':
                            for item in element.get('elements', []):
                                if item.get('type') == 'link' and 'url' in item:
                                    url = item.get('url')
                                    logger.info(f"Found URL in blocks: {url}")
                                    break
        
        if url:
            logger.info(f"Processing URL: {url}")
            # Fetch and summarize the content
            summary = fetch_and_summarize(url)
            if summary:
                # If the message is already in a thread, use that thread_ts
                # If not, use the message's own timestamp to create a new thread
                reply_thread_ts = thread_ts if thread_ts else message_ts
                post_summary_to_slack(channel, user, summary, reply_thread_ts)
        else:
            logger.warning("No URL found to process")
    except Exception as e:
        logger.error(f"Error in handle_message: {str(e)}", exc_info=True)

def extract_url(text):
    try:
        # Simple URL extraction logic
        url_match = re.search(r'(https?://\S+)', text)
        if url_match:
            url = url_match.group(0)
            # Remove trailing punctuation that might be part of the match but not the URL
            url = re.sub(r'[.,;:!?)]+$', '', url)
            logger.info(f"Extracted URL: {url}")
            return url
        return None
    except Exception as e:
        logger.error(f"Error extracting URL: {str(e)}", exc_info=True)
        return None

def fetch_and_summarize(url):
    logger.info(f"Fetching and summarizing URL: {url}")
    try:
        # Add user-agent header to mimic a browser request
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'en-US,en;q=0.9',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        logger.info(f"URL response status code: {response.status_code}")
        
        if response.status_code != 200:
            if response.status_code == 403 and 'www.cell.com' in url:
                return f"Unfortunately this bot doesn't work for Cell Press articles. They have a very good bot blocker :cry:"
            else:
                return f"Unable to access the webpage. Status code: {response.status_code}"
            
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Try to get the main content, focusing on article elements
        article = soup.find('article')
        if article:
            content = article.get_text()
            logger.info("Found article content")
        else:
            # Fall back to main content or body
            main = soup.find('main') or soup.find('body')
            content = main.get_text() if main else soup.get_text()
            logger.info("Using fallback content")
        
        # Limit content length to avoid API limits
        content = content[:15000]
        logger.info(f"Content length: {len(content)} characters")

        # Call OpenAI API to summarize
        logger.info("Calling OpenAI API for summarization")
        response = openai_client.responses.create(
            model="gpt-4o-mini",
            instructions="I am a PhD student in molecular biology, synthetic biology, bioengineering, and bioinfomatics. I have a basic understanding of all of these fields, but I am not an expert. I want you to summarize the following academic paper webpage, at a level suitable for a PhD student with undergrad knowledge in physics, chemistry, and biology. Give me the main findings and the key points. Also include 2 strengths of the paper, 2 weaknesses, and 2 followup experiments or hypthesis that they should explore. Also include the paper title and where the authors are from (if this information is available). Limit the response to 200 words. I am also posting this message to slack, so make sure to format it correctly. It only accepts single asterisks for bold and single underscores for italics. Do not use other formatting options. You need to follow these formmating instructions.",
            input=content,
        )
        summary = response.output_text.strip()
        logger.info("Successfully generated summary")
        return summary
    except Exception as e:
        logger.error(f"Error fetching or summarizing content: {str(e)}", exc_info=True)
        return f"I encountered an error when trying to access or process the content: {str(e)}"

def post_summary_to_slack(channel, user, summary, thread_ts=None):
    try:
        logger.info(f"Posting summary to channel {channel} for user {user}")
        response = slack_client.chat_postMessage(
            channel=channel,
            text=f"<@{user}> Here's the summary of the linked paper:\n{summary}",
            thread_ts=thread_ts  # This will post in the thread if thread_ts is provided
        )
        logger.info(f"Successfully posted message: {response.get('ts')}")
    except SlackApiError as e:
        error_message = e.response['error'] if hasattr(e, 'response') and 'error' in e.response else str(e)
        logger.error(f"Error posting message to Slack: {error_message}", exc_info=True)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    logger.info(f"Starting Flask app on port {port}")
    app.run(host='0.0.0.0', port=port)