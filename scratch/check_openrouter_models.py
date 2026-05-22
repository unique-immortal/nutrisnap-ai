import urllib.request
import json

req = urllib.request.Request(
    'https://openrouter.ai/api/v1/models',
    headers={'User-Agent': 'Mozilla/5.0'}
)

try:
    with urllib.request.urlopen(req) as res:
        data = json.loads(res.read().decode('utf-8'))['data']
        print("Google models on OpenRouter:")
        for m in data:
            model_id = m.get('id', '')
            if 'google/' in model_id:
                pricing = m.get('pricing', {})
                prompt_price = float(pricing.get('prompt', '0.0')) * 1000000
                completion_price = float(pricing.get('completion', '0.0')) * 1000000
                print(f" - ID: {model_id} | Name: {m.get('name')} | Prompt: ${prompt_price:.4f}/M tokens | Completion: ${completion_price:.4f}/M tokens")
except Exception as e:
    print(f"Error: {e}")
