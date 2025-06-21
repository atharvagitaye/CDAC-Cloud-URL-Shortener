import functions_framework
from flask import jsonify, request, redirect
from google.cloud import firestore
import string
import random
import re
from datetime import datetime
import logging

# Initialize Firestore client
db = firestore.Client()
COLLECTION_NAME = 'YOUR_COLLECTION_NAME'

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
            # Check if short code already exists
            doc_ref = db.collection(COLLECTION_NAME).document(short_code)
            doc = doc_ref.get()
            
            if not doc.exists:
                # Create new document with short code as document ID
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
            # Handle URL shortening requests
            try:
                data = request.get_json()
                if not data or 'long_url' not in data:
                    return jsonify({
                        'error': 'Missing long_url in request body'
                    }), 400, headers
                
                long_url = data['long_url'].strip()
                
                # Validate URL
                if not long_url:
                    return jsonify({
                        'error': 'long_url cannot be empty'
                    }), 400, headers
                
                if not is_valid_url(long_url):
                    return jsonify({
                        'error': 'Invalid URL format'
                    }), 400, headers
                
                # Check if URL already exists
                existing_code = check_existing_url(long_url)
                if existing_code:
                    # Get the existing document data
                    doc = db.collection(COLLECTION_NAME).document(existing_code).get()
                    doc_data = doc.to_dict()
                    
                    return jsonify({
                        'success': True,
                        'short_code': existing_code,
                        'short_url': f"{request.host_url.rstrip('/')}/{existing_code}",
                        'long_url': long_url,
                        'created_at': doc_data.get('created_at').isoformat() if doc_data.get('created_at') else None,
                        'message': 'URL already exists'
                    }), 200, headers
                
                # Create new short URL
                short_code = create_short_url(long_url)
                
                # Get the created document to return the timestamp
                doc = db.collection(COLLECTION_NAME).document(short_code).get()
                doc_data = doc.to_dict()
                
                return jsonify({
                    'success': True,
                    'short_code': short_code,
                    'short_url': f"{request.host_url.rstrip('/')}/{short_code}",
                    'long_url': long_url,
                    'created_at': doc_data.get('created_at').isoformat() if doc_data.get('created_at') else None
                }), 201, headers
                
            except Exception as e:
                logger.error(f"Error processing POST request: {str(e)}")
                return jsonify({
                    'error': 'Internal server error while creating short URL'
                }), 500, headers
        
        elif request.method == 'GET':
            # Handle URL redirection
            path = request.path.strip('/')
            
            if not path:
                return jsonify({
                    'error': 'No short code provided'
                }), 400, headers
            
            try:
                # Look up the short code in Firestore
                doc_ref = db.collection(COLLECTION_NAME).document(path)
                doc = doc_ref.get()
                
                if not doc.exists:
                    return jsonify({
                        'error': 'Short URL not found'
                    }), 404, headers
                
                doc_data = doc.to_dict()
                long_url = doc_data.get('long_url')
                
                if not long_url:
                    return jsonify({
                        'error': 'Invalid short URL data'
                    }), 500, headers
                
                # Redirect to the original URL
                return redirect(long_url, code=302)
                
            except Exception as e:
                logger.error(f"Error processing GET request: {str(e)}")
                return jsonify({
                    'error': 'Internal server error while retrieving URL'
                }), 500, headers
        
        else:
            return jsonify({
                'error': 'Method not allowed'
            }), 405, headers
            
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return jsonify({
            'error': 'Internal server error'
        }), 500, headers