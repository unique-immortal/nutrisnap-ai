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

# Add MODIFY_AUDIO_SETTINGS permission
modify_audio_str = '<uses-permission android:name="android.permission.MODIFY_AUDIO_SETTINGS" />'
if modify_audio_str not in content:
    content = content.replace("</manifest>", f"    {modify_audio_str}\n</manifest>")

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

# Patch build.gradle version
import json
import re

package_json_path = "package.json"
build_gradle_path = "android/app/build.gradle"

if os.path.exists(package_json_path) and os.path.exists(build_gradle_path):
    with open(package_json_path, "r", encoding="utf-8") as f:
        pkg = json.load(f)
    version = pkg.get("version", "1.0.0")
    
    # Calculate versionCode: 5.5.4 -> 554
    v_parts = version.replace("v", "").split(".")
    try:
        v_code = int("".join(v_parts))
    except:
        v_code = 1

    with open(build_gradle_path, "r", encoding="utf-8") as f:
        gradle_content = f.read()

    gradle_content = re.sub(r'versionName\s+".*?"', f'versionName "{version}"', gradle_content)
    gradle_content = re.sub(r'versionCode\s+\d+', f'versionCode {v_code}', gradle_content)

    with open(build_gradle_path, "w", encoding="utf-8") as f:
        f.write(gradle_content)

    print(f"build.gradle patched with versionName {version} and versionCode {v_code}!")
