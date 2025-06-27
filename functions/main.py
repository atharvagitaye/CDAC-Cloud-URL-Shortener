import functions_framework
from flask import jsonify, request, redirect
from google.cloud import firestore
from google.cloud import pubsub_v1
import string
import random
import re
import json
from datetime import datetime
import logging
import os

# Initialize clients
db = firestore.Client()
publisher = pubsub_v1.PublisherClient()

# Configuration
COLLECTION_NAME = 'shortened_urls'
PROJECT_ID = os.getenv('GCP_PROJECT_ID', 'your-project-id')
TOPIC_NAME = os.getenv('PUBSUB_TOPIC_NAME', 'your-topic-name')
TOPIC_PATH = publisher.topic_path(PROJECT_ID, TOPIC_NAME)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def publish_event(event_type, data):
    """Publish event to Pub/Sub topic"""
    try:
        message_data = {
            'event_type': event_type,
            'timestamp': datetime.utcnow().isoformat(),
            'data': data
        }

        message_json = json.dumps(message_data)
        message_bytes = message_json.encode('utf-8')

        # Publish message
        future = publisher.publish(TOPIC_PATH, message_bytes)
        message_id = future.result()

        logger.info(f"Published message {message_id} to {TOPIC_PATH}")
        return message_id

    except Exception as e:
        logger.error(f"Error publishing to Pub/Sub: {str(e)}")
        return None

def generate_short_code(length=6):
    """Generate a random short code using alphanumeric characters"""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(length))

def is_valid_url(url):
    """Basic URL validation"""
    url_pattern = re.compile(
        r'^https?://'  # http:// or https://
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'  # domain...
        r'localhost|'  # localhost...
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
        r'(?::\d+)?'  # optional port
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    return url_pattern.match(url) is not None

def check_existing_url(long_url):
    """Check if URL already exists in database"""
    try:
        docs = db.collection(COLLECTION_NAME).where('long_url', '==', long_url).limit(1).get()
        if docs:
            return docs[0].id
        return None
    except Exception as e:
        logger.error(f"Error checking existing URL: {str(e)}")
        return None

def create_short_url(long_url):
    """Create a new short URL entry in Firestore"""
    max_attempts = 10

    for attempt in range(max_attempts):
        short_code = generate_short_code()

        try:
            doc_ref = db.collection(COLLECTION_NAME).document(short_code)
            doc = doc_ref.get()

            if not doc.exists:
                doc_ref.set({
                    'long_url': long_url,
                    'created_at': firestore.SERVER_TIMESTAMP
                })

                logger.info(f"Created short URL: {short_code} -> {long_url}")
                return short_code

        except Exception as e:
            logger.error(f"Error creating short URL (attempt {attempt + 1}): {str(e)}")
            continue

    raise Exception("Failed to generate unique short code after maximum attempts")

@functions_framework.http
def url_shortener(request):
    """Main Cloud Function entry point"""

    # Handle CORS
    if request.method == 'OPTIONS':
        headers = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Access-Control-Max-Age': '3600'
        }
        return ('', 204, headers)

    # Set CORS headers for actual requests
    headers = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type',
    }

    try:
        if request.method == 'POST':
            try:
                data = request.get_json()
                if not data or 'long_url' not in data:
                    return jsonify({'error': 'Missing long_url in request body'}), 400, headers

                long_url = data['long_url'].strip()

                if not long_url:
                    return jsonify({'error': 'long_url cannot be empty'}), 400, headers

                if not is_valid_url(long_url):
                    return jsonify({'error': 'Invalid URL format'}), 400, headers

                existing_code = check_existing_url(long_url)
                if existing_code:
                    doc = db.collection(COLLECTION_NAME).document(existing_code).get()
                    doc_data = doc.to_dict()

                    publish_event('url_request_existing', {
                        'short_code': existing_code,
                        'long_url': long_url,
                        'user_agent': request.headers.get('User-Agent', ''),
                        'ip_address': request.remote_addr,
                        'referer': request.headers.get('Referer', ''),
                        'created_at': doc_data.get('created_at').isoformat() if doc_data.get('created_at') else None
                    })

                    return jsonify({
                        'success': True,
                        'short_code': existing_code,
                        'short_url': f"{request.host_url.rstrip('/')}/{existing_code}",
                        'long_url': long_url,
                        'created_at': doc_data.get('created_at').isoformat() if doc_data.get('created_at') else None,
                        'message': 'URL already exists'
                    }), 200, headers

                short_code = create_short_url(long_url)
                doc = db.collection(COLLECTION_NAME).document(short_code).get()
                doc_data = doc.to_dict()

                publish_event('url_created', {
                    'short_code': short_code,
                    'long_url': long_url,
                    'user_agent': request.headers.get('User-Agent', ''),
                    'ip_address': request.remote_addr,
                    'referer': request.headers.get('Referer', ''),
                    'created_at': doc_data.get('created_at').isoformat() if doc_data.get('created_at') else None
                })

                return jsonify({
                    'success': True,
                    'short_code': short_code,
                    'short_url': f"{request.host_url.rstrip('/')}/{short_code}",
                    'long_url': long_url,
                    'created_at': doc_data.get('created_at').isoformat() if doc_data.get('created_at') else None
                }), 201, headers

            except Exception as e:
                logger.error(f"Error processing POST request: {str(e)}")
                return jsonify({'error': 'Internal server error while creating short URL'}), 500, headers

        elif request.method == 'GET':
            path = request.path.strip('/')

            if not path:
                return jsonify({'error': 'No short code provided'}), 400, headers

            try:
                doc_ref = db.collection(COLLECTION_NAME).document(path)
                doc = doc_ref.get()

                if not doc.exists:
                    return jsonify({'error': 'Short URL not found'}), 404, headers

                doc_data = doc.to_dict()
                long_url = doc_data.get('long_url')

                if not long_url:
                    return jsonify({'error': 'Invalid short URL data'}), 500, headers

                publish_event('url_accessed', {
                    'short_code': path,
                    'long_url': long_url,
                    'user_agent': request.headers.get('User-Agent', ''),
                    'ip_address': request.remote_addr,
                    'referer': request.headers.get('Referer', ''),
                    'access_time': datetime.utcnow().isoformat()
                })

                return redirect(long_url, code=302)

            except Exception as e:
                logger.error(f"Error processing GET request: {str(e)}")
                return jsonify({'error': 'Internal server error while retrieving URL'}), 500, headers

        else:
            return jsonify({'error': 'Method not allowed'}), 405, headers

    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500, headers
