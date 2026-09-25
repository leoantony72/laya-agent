"""
Quick launcher for Laya Desktop Control Agent.
Starts the server and automatically opens the UI in your default web browser.
"""

import os
import sys
import time
import webbrowser
import threading
import uvicorn

def open_browser():
    time.sleep(1.5)
    webbrowser.open("http://127.0.0.1:8765")

if __name__ == "__main__":
    print("=" * 60)
    print("  Starting Laya Desktop Control Agent")
    print("  UI will open at: http://127.0.0.1:8765")
    print("=" * 60)
    
    # Launch browser in a background thread
    threading.Thread(target=open_browser, daemon=True).start()
    
    # Start server
    uvicorn.run("server:app", host="127.0.0.1", port=8765, reload=False)
