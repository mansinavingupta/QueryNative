"""
QueryNative - NLP to SQL Web App
Run this file to start the server.
"""
import os
import sys


os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print(
    "\n"
    "QueryNative - NLP to SQL\n"
    "Database: configure any PostgreSQL database in the web UI\n"
    "Open:     http://127.0.0.1:5000\n"
)

from app import app

app.run(debug=False, port=5000)
