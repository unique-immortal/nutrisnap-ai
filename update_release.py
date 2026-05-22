#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import re
import json
import urllib.request
from urllib.error import HTTPError

REPO = "unique-immortal/nutrisnap-ai"

def get_version():
    # Attempt to parse version from app.py
    app_path = os.path.join(os.path.dirname(__file__), "app.py")
    if os.path.exists(app_path):
        try:
            with open(app_path, "r", encoding="utf-8") as f:
                content = f.read()
                # Find "version": "vX.Y.Z"
                match = re.search(r'"version":\s*"([^"]+)"', content)
                if match:
                    return match.group(1)
        except Exception as e:
            print(f"Warning: Failed to parse version from app.py: {e}")
    
    # Fallback to sys.argv
    if len(sys.argv) > 1:
        return sys.argv[1]
    
    print("Error: Could not determine version. Please pass it as an argument, e.g. python update_release.py v5.5.0")
    sys.exit(1)

def get_github_token():
    # 1. Check environment variable
    token = os.environ.get("GITHUB_TOKEN")
    if token and "dummy" not in token.lower():
        return token
    
    # 2. Check .env file
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("GITHUB_TOKEN="):
                        val = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                        if val and "dummy" not in val.lower():
                            return val
        except Exception as e:
            print(f"Warning: Failed to read .env file: {e}")
            
    print("Error: GITHUB_TOKEN environment variable or GITHUB_TOKEN in .env is missing or invalid.")
    print("Please set GITHUB_TOKEN with write permission for repo releases.")
    sys.exit(1)

def read_release_notes(version):
    # Try RELEASE_NOTES_vX.Y.Z.md first
    filename = f"RELEASE_NOTES_{version}.md"
    if not os.path.exists(filename):
        # Try without the 'v' prefix if present
        v_stripped = version[1:] if version.startswith("v") else version
        filename = f"RELEASE_NOTES_{v_stripped}.md"
        if not os.path.exists(filename):
            # Try RELEASE_NOTES_v5.md or similar
            major = version.split(".")[0]
            filename = f"RELEASE_NOTES_{major}.md"
            if not os.path.exists(filename):
                filename = "RELEASE_NOTES.md"
                if not os.path.exists(filename):
                    print(f"Error: Release notes file not found for version {version}.")
                    sys.exit(1)
    
    print(f"Reading release notes from {filename}...")
    with open(filename, "r", encoding="utf-8") as f:
        return f.read()

def make_github_request(url, method, token, data=None):
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "NutriSnap-Release-Manager"
    }
    
    req_data = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        # ensure_ascii=False is critical to prevent Chinese chars being converted to \uXXXX strings
        req_data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        
    req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
    
    try:
        with urllib.request.urlopen(req) as res:
            return json.loads(res.read().decode("utf-8"))
    except HTTPError as e:
        err_msg = e.read().decode("utf-8")
        print(f"HTTP Error {e.code}: {e.reason}")
        print(f"Response: {err_msg}")
        raise e

def main():
    version = get_version()
    # Normalize version to have 'v' prefix
    if not version.startswith("v"):
        version = "v" + version
        
    print(f"Targeting release version: {version}")
    token = get_github_token()
    body_content = read_release_notes(version)
    
    # 1. Fetch existing releases to see if this tag already exists
    releases_url = f"https://api.github.com/repos/{REPO}/releases"
    print(f"Fetching existing releases for {REPO}...")
    try:
        releases = make_github_request(releases_url, "GET", token)
    except Exception as e:
        print(f"Failed to fetch releases: {e}")
        sys.exit(1)
        
    existing_release = None
    for r in releases:
        if r["tag_name"] == version:
            existing_release = r
            break
            
    payload = {
        "tag_name": version,
        "name": version,
        "body": body_content,
        "draft": False,
        "prerelease": False
    }
    
    if existing_release:
        release_id = existing_release["id"]
        update_url = f"https://api.github.com/repos/{REPO}/releases/{release_id}"
        print(f"Release {version} exists (ID: {release_id}). Updating release body...")
        try:
            res = make_github_request(update_url, "PATCH", token, payload)
            print(f"Success! Release {version} updated: {res['html_url']}")
        except Exception as e:
            print(f"Failed to update release: {e}")
            sys.exit(1)
    else:
        print(f"Release {version} does not exist. Creating new release...")
        try:
            res = make_github_request(releases_url, "POST", token, payload)
            print(f"Success! Release {version} created: {res['html_url']}")
        except Exception as e:
            print(f"Failed to create release: {e}")
            sys.exit(1)

if __name__ == "__main__":
    main()
