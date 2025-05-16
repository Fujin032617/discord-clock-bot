from flask import Flask, make_response, request
from threading import Thread
from functools import wraps
import time
import os

app = Flask('')

request_count = {}
RATE_LIMIT = 10
RATE_TIME = 60

def rate_limit(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        now = time.time()
        ip = request.remote_addr
        if ip in request_count:
            if now - request_count[ip]['time'] >= RATE_TIME:
                request_count[ip] = {'count': 1, 'time': now}
            elif request_count[ip]['count'] >= RATE_LIMIT:
                return 'Rate limit exceeded', 429
            else:
                request_count[ip]['count'] += 1
        else:
            request_count[ip] = {'count': 1, 'time': now}
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
@rate_limit
def home():
    response = make_response("I'm alive!")
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    return response

def run():
    port = int(os.environ.get('PORT', 8080))
    print(f"Starting web server on port {port}")
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()
