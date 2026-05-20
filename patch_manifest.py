import os

manifest_path = "android/app/src/main/AndroidManifest.xml"
if not os.path.exists(manifest_path):
    print(f"Error: {manifest_path} not found.")
    exit(1)

with open(manifest_path, "r", encoding="utf-8") as f:
    content = f.read()

# Add RECORD_AUDIO permission
permission_str = '<uses-permission android:name="android.permission.RECORD_AUDIO" />'
if permission_str not in content:
    content = content.replace("</manifest>", f"    {permission_str}\n</manifest>")

# Add SpeechRecognition queries
queries_str = """    <queries>
        <intent>
            <action android:name="android.speech.RecognitionService" />
        </intent>
    </queries>"""

if "<queries>" not in content:
    content = content.replace("</manifest>", f"{queries_str}\n</manifest>")

with open(manifest_path, "w", encoding="utf-8") as f:
    f.write(content)

print("AndroidManifest.xml patched successfully!")
